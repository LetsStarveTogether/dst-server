//! Deterministic room policy decisions; the Agent owns execution and persistence.

use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    time::Duration,
};

use anyhow::{Context, Result, bail, ensure};
use chrono::{
    DateTime, Days, LocalResult, NaiveDateTime, NaiveTime, TimeDelta, TimeZone, Timelike, Utc,
};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::time::Instant;

use crate::{files::RoomLock, model::Countdown};

pub const CHECK_INTERVAL: Duration = Duration::from_secs(30);
pub const MOD_NOTICE_DELAY: Duration = Duration::from_secs(60);
pub const MOD_NOTICE_INTERVAL: Duration = Duration::from_secs(30);
pub const MOD_RETRY_DELAY: Duration = Duration::from_secs(300);
pub const MOD_ATTEMPTS: u8 = 3;
const CLOSING_NOTICE_MINUTES: i64 = 8;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DailyWindow {
    #[serde(with = "minute_time")]
    pub start: NaiveTime,
    #[serde(with = "minute_time")]
    pub end: NaiveTime,
}

impl DailyWindow {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.start != self.end,
            "daily window endpoints must differ; use an empty schedule for all day"
        );
        ensure!(
            [self.start, self.end]
                .iter()
                .all(|time| time.second() == 0 && time.nanosecond() == 0),
            "daily windows require minute precision"
        );
        Ok(())
    }
}

mod minute_time {
    use super::*;

    pub fn serialize<S: serde::Serializer>(
        time: &NaiveTime,
        serializer: S,
    ) -> std::result::Result<S::Ok, S::Error> {
        if time.second() != 0 || time.nanosecond() != 0 {
            return Err(serde::ser::Error::custom(
                "daily windows require minute precision",
            ));
        }
        serializer.serialize_str(&time.format("%H:%M").to_string())
    }

    pub fn deserialize<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<NaiveTime, D::Error> {
        let value = String::deserialize(deserializer)?;
        let time = NaiveTime::parse_from_str(&value, "%H:%M")
            .or_else(|_| NaiveTime::parse_from_str(&value, "%H:%M:%S"))
            .map_err(|_| serde::de::Error::custom("daily window time must be HH:MM"))?;
        if time.second() != 0 || time.nanosecond() != 0 {
            return Err(serde::de::Error::custom(
                "daily windows require minute precision",
            ));
        }
        Ok(time)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    pub timezone: String,
    pub schedule: Vec<DailyWindow>,
    pub mod_auto_update: bool,
    pub idle_regeneration: bool,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            timezone: "Asia/Shanghai".into(),
            schedule: Vec::new(),
            mod_auto_update: true,
            idle_regeneration: false,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleState {
    pub open: bool,
    /// The UTC start of the merged opening; None means continuously open.
    pub opening_id: Option<DateTime<Utc>>,
    pub next_change: Option<DateTime<Utc>>,
}

impl Policy {
    pub fn validate(&self) -> Result<()> {
        self.timezone
            .parse::<Tz>()
            .context("unknown IANA schedule timezone")?;
        ensure!(self.schedule.len() <= 1440, "too many daily windows");
        for window in &self.schedule {
            window.validate()?;
        }
        Ok(())
    }

    pub fn load(room: &RoomLock) -> Result<Self> {
        let policy = room
            .read_control()?
            .remove("policy")
            .map(serde_json::from_value)
            .transpose()
            .context("invalid room policy")?
            .unwrap_or_default();
        Self::validate(&policy)?;
        Ok(policy)
    }

    pub fn save(&self, room: &mut RoomLock) -> Result<()> {
        self.validate()?;
        room.update_control(|control| {
            control.insert("policy".into(), serde_json::to_value(self)?);
            Ok(())
        })
    }

    /// Ambiguous starts use their first occurrence, ambiguous ends their last.
    /// Missing local times advance to the first valid minute, including skipped dates.
    /// Thus a repeated DST hour belongs to one opening and one recovery opportunity.
    pub fn schedule_at(&self, now: DateTime<Utc>) -> Result<ScheduleState> {
        self.validate()?;
        let windows = merged_minutes(&self.schedule);
        if windows.is_empty() || windows == [(0, 1440)] {
            return Ok(ScheduleState {
                open: true,
                opening_id: None,
                next_change: None,
            });
        }
        let zone: Tz = self.timezone.parse()?;
        let today = now.with_timezone(&zone).date_naive();
        let mut intervals = Vec::new();
        for offset in -2_i64..=3 {
            let date = today
                .checked_add_signed(TimeDelta::days(offset))
                .context("schedule date is out of range")?;
            for &(start, end) in &windows {
                let start_local = date
                    .and_hms_opt(start / 60, start % 60, 0)
                    .context("invalid window start")?;
                let end_date = if end <= start || end == 1440 {
                    date.checked_add_days(Days::new(1))
                        .context("schedule date is out of range")?
                } else {
                    date
                };
                let end_minute = end % 1440;
                let end_local = end_date
                    .and_hms_opt(end_minute / 60, end_minute % 60, 0)
                    .context("invalid window end")?;
                let start = resolve_local(zone, start_local, false)?;
                let end = resolve_local(zone, end_local, true)?;
                if start < end {
                    intervals.push((start, end));
                }
            }
        }
        intervals.sort_unstable();
        let mut merged: Vec<(DateTime<Utc>, DateTime<Utc>)> = Vec::new();
        for (start, end) in intervals {
            if let Some(previous) = merged.last_mut()
                && start <= previous.1
            {
                previous.1 = previous.1.max(end);
            } else {
                merged.push((start, end));
            }
        }
        for (start, end) in merged {
            if start <= now && now < end {
                return Ok(ScheduleState {
                    open: true,
                    opening_id: Some(start),
                    next_change: Some(end),
                });
            }
            if start > now {
                return Ok(ScheduleState {
                    open: false,
                    opening_id: None,
                    next_change: Some(start),
                });
            }
        }
        bail!("cannot determine the next schedule boundary")
    }

    /// Persist the returned state before executing its action. Repeated ticks do
    /// not consume a download attempt; only UpdateMods reserves an attempt.
    pub fn plan(
        &self,
        state: &mut PolicyState,
        now: DateTime<Utc>,
        inputs: &Inputs,
    ) -> Result<Plan> {
        state.validate()?;
        inputs.validate()?;
        let schedule = self.schedule_at(now)?;
        let clock_changed = state.clock_changed(now, inputs.monotonic)?;
        let new_opening = schedule.opening_id.filter(|opening| {
            !clock_changed
                && state.current_opening != Some(*opening)
                && state
                    .last_opening
                    .is_none_or(|previous| *opening > previous)
        });
        state.current_opening = schedule.opening_id;
        if let Some(opening) = new_opening {
            state.last_opening = Some(opening);
            if matches!(state.mods.phase, ModPhase::Failed) {
                state.mods.request()?;
            }
        }
        if self.mod_auto_update
            && inputs.mods_outdated
            && !state.mods.observed_outdated
            && matches!(state.mods.phase, ModPhase::Idle)
        {
            state.mods.phase = ModPhase::Pending;
        }
        state.mods.observed_outdated = inputs.mods_outdated;
        if !self.mod_auto_update && !state.mods.manual && state.mods.attempts == 0 {
            state.mods = ModMaintenance {
                observed_outdated: inputs.mods_outdated,
                ..ModMaintenance::default()
            };
        }
        state.observe_activity(now, inputs, clock_changed)?;
        let mut plan = Plan {
            schedule,
            new_opening,
            action: None,
            fault: matches!(state.mods.phase, ModPhase::Failed)
                .then(|| "mod update failed after three attempts".into()),
        };

        if !plan.schedule.open {
            if matches!(
                state.mods.phase,
                ModPhase::Announcing { .. } | ModPhase::Stopping
            ) {
                state.mods.phase = ModPhase::Pending;
            }
            if inputs.game_running && !inputs.writes_in_progress {
                plan.action = Some(Action::Stop {
                    reason: StopReason::Schedule,
                    message: None,
                });
            }
            return Ok(plan);
        }
        if inputs.writes_in_progress || inputs.faulted {
            return Ok(plan);
        }
        if matches!(state.mods.phase, ModPhase::Failed) {
            if inputs.game_running {
                plan.action = Some(Action::Stop {
                    reason: StopReason::ModFailure,
                    message: None,
                });
            }
            return Ok(plan);
        }

        if !matches!(state.mods.phase, ModPhase::Idle) {
            let closing_soon = plan.schedule.next_change.is_some_and(|close| {
                close - now <= TimeDelta::seconds(MOD_NOTICE_DELAY.as_secs() as i64)
            });
            if closing_soon {
                if matches!(state.mods.phase, ModPhase::Announcing { .. }) {
                    state.mods.phase = ModPhase::Pending;
                }
                if !inputs.busy {
                    plan.action = self.closing_notice(state, now, inputs, &plan.schedule)?;
                }
            } else if !inputs.busy {
                plan.action = state.mods.action(now, inputs)?;
            }
            return Ok(plan);
        }
        if inputs.busy {
            return Ok(plan);
        }
        if !inputs.game_running {
            plan.action = Some(Action::Start);
            return Ok(plan);
        }

        if self.idle_regeneration
            && inputs.all_ready
            && let Some(day) = inputs.master_day
            && let Some(activity) = &mut state.activity
            && !activity.interrupted
            && inputs.reliable_empty()
            && now.signed_duration_since(activity.last_active_at) > retention(day)
        {
            // Renew before dispatch, including a later unknown regeneration result.
            activity.last_active_at = now;
            plan.action = Some(Action::Regenerate {
                expected_sessions: activity.sessions.clone(),
            });
            return Ok(plan);
        }
        plan.action = self.closing_notice(state, now, inputs, &plan.schedule)?;
        Ok(plan)
    }

    fn closing_notice(
        &self,
        state: &mut PolicyState,
        now: DateTime<Utc>,
        inputs: &Inputs,
        schedule: &ScheduleState,
    ) -> Result<Option<Action>> {
        let Some(close) = schedule.next_change else {
            return Ok(None);
        };
        let remaining = close.signed_duration_since(now);
        let minutes = (remaining.num_seconds() + 59) / 60;
        if !inputs.game_running
            || !inputs.all_ready
            || !(1..=CLOSING_NOTICE_MINUTES).contains(&minutes)
        {
            return Ok(None);
        }
        let notice = ClosingNotice { close, minutes };
        if state.last_closing_notice.as_ref() == Some(&notice) {
            return Ok(None);
        }
        let next = self.schedule_at(close)?.next_change;
        let zone: Tz = self.timezone.parse()?;
        let opening = next
            .map(|time| {
                format!(
                    "下次开放时间：{}。",
                    time.with_timezone(&zone).format("%H:%M")
                )
            })
            .unwrap_or_default();
        state.last_closing_notice = Some(notice);
        Ok(Some(Action::Announce {
            message: format!(
                "本房间将在约 {minutes} 分钟后定时关闭，请提前安排游戏进度。{opening}"
            ),
        }))
    }
}

fn merged_minutes(windows: &[DailyWindow]) -> Vec<(u32, u32)> {
    let mut ranges = Vec::new();
    for window in windows {
        let start = window.start.hour() * 60 + window.start.minute();
        let end = window.end.hour() * 60 + window.end.minute();
        if start < end {
            ranges.push((start, end));
        } else {
            ranges.extend([(start, 1440), (0, end)]);
        }
    }
    ranges.sort_unstable();
    let mut merged: Vec<(u32, u32)> = Vec::new();
    for (start, end) in ranges {
        if start == end {
            continue;
        }
        if let Some(previous) = merged.last_mut()
            && start <= previous.1
        {
            previous.1 = previous.1.max(end);
        } else {
            merged.push((start, end));
        }
    }
    // Join the two sides of midnight into the same named opening.
    if merged.len() > 1 && merged[0].0 == 0 && merged.last().is_some_and(|range| range.1 == 1440) {
        let end = merged.remove(0).1;
        merged.last_mut().unwrap().1 = end;
    }
    merged
}

fn resolve_local(zone: Tz, local: NaiveDateTime, end: bool) -> Result<DateTime<Utc>> {
    // IANA includes a whole skipped civil date (for example Pacific/Apia).
    for minute in 0..=1440 {
        let candidate = local
            .checked_add_signed(TimeDelta::minutes(minute))
            .context("schedule date is out of range")?;
        match zone.from_local_datetime(&candidate) {
            LocalResult::Single(value) => return Ok(value.with_timezone(&Utc)),
            LocalResult::Ambiguous(first, second) => {
                return Ok(if end {
                    first.max(second)
                } else {
                    first.min(second)
                }
                .with_timezone(&Utc));
            }
            LocalResult::None => {}
        }
    }
    bail!("cannot resolve local schedule boundary")
}

#[derive(Clone, Debug, Default)]
pub struct Inputs {
    pub game_running: bool,
    pub all_ready: bool,
    pub clean_stop_in_progress: bool,
    pub busy: bool,
    pub writes_in_progress: bool,
    pub faulted: bool,
    pub mods_outdated: bool,
    pub expected_shards: Vec<String>,
    pub presence: BTreeMap<String, Presence>,
    pub master_day: Option<u64>,
    /// Agent uptime from a monotonic clock; supply it to detect wall-clock jumps.
    pub monotonic: Option<Duration>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Presence {
    pub session_id: String,
    pub observation: String,
    pub reliable: bool,
    pub client_count: u64,
    pub player_count: u64,
    pub idle_seconds: f64,
    pub observed_seconds: f64,
}

impl Inputs {
    fn validate(&self) -> Result<()> {
        ensure!(
            !self.all_ready || self.game_running,
            "a stopped room cannot be ready"
        );
        ensure!(
            self.master_day.is_none_or(|day| day > 0),
            "world day must be positive"
        );
        let names: BTreeSet<_> = self.expected_shards.iter().collect();
        ensure!(
            names.len() == self.expected_shards.len() && names.iter().all(|name| !name.is_empty()),
            "expected shards must be nonempty and unique"
        );
        for presence in self.presence.values() {
            ensure!(
                !presence.session_id.is_empty() && !presence.observation.is_empty(),
                "presence identity is missing"
            );
            ensure!(
                [presence.idle_seconds, presence.observed_seconds]
                    .iter()
                    .all(|value| value.is_finite()
                        && *value >= 0.0
                        && Duration::try_from_secs_f64(*value).is_ok()),
                "presence times must be finite nonnegative durations"
            );
        }
        Ok(())
    }

    fn reliable(&self) -> bool {
        self.all_ready
            && !self.expected_shards.is_empty()
            && self.presence.len() == self.expected_shards.len()
            && self.expected_shards.iter().all(|name| {
                self.presence
                    .get(name)
                    .is_some_and(|presence| presence.reliable)
            })
    }

    fn reliable_empty(&self) -> bool {
        self.reliable()
            && self
                .presence
                .values()
                .all(|presence| presence.client_count == 0 && presence.player_count == 0)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Activity {
    pub sessions: BTreeMap<String, String>,
    pub observations: BTreeMap<String, String>,
    pub observed_seconds: BTreeMap<String, f64>,
    pub last_active_at: DateTime<Utc>,
    pub clean_shutdown: bool,
    pub interrupted: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ClosingNotice {
    close: DateTime<Utc>,
    minutes: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PolicyState {
    pub version: u8,
    pub current_opening: Option<DateTime<Utc>>,
    pub last_opening: Option<DateTime<Utc>>,
    pub activity: Option<Activity>,
    pub mods: ModMaintenance,
    last_closing_notice: Option<ClosingNotice>,
    // Each Agent tick reloads this state; initialization resets clocks after a restart.
    last_wall: Option<DateTime<Utc>>,
    last_monotonic: Option<Duration>,
}

impl Default for PolicyState {
    fn default() -> Self {
        Self {
            version: 1,
            current_opening: None,
            last_opening: None,
            activity: None,
            mods: ModMaintenance::default(),
            last_closing_notice: None,
            last_wall: None,
            last_monotonic: None,
        }
    }
}

impl PolicyState {
    pub fn validate(&self) -> Result<()> {
        ensure!(self.version == 1, "unsupported policy state version");
        self.mods.validate()?;
        if let Some(activity) = &self.activity {
            ensure!(
                !activity.sessions.is_empty()
                    && activity.sessions.keys().eq(activity.observations.keys())
                    && activity
                        .sessions
                        .keys()
                        .eq(activity.observed_seconds.keys()),
                "activity shard identities differ"
            );
            ensure!(
                activity
                    .sessions
                    .values()
                    .chain(activity.observations.values())
                    .all(|value| !value.is_empty()),
                "activity identities cannot be empty"
            );
            ensure!(
                activity
                    .observed_seconds
                    .values()
                    .all(|value| value.is_finite() && *value >= 0.0),
                "invalid activity observation duration"
            );
        }
        Ok(())
    }

    pub fn load(room: &RoomLock) -> Result<Self> {
        let state = room
            .read_control()?
            .remove("policy_state")
            .map(serde_json::from_value)
            .transpose()
            .context("invalid room policy state")?
            .unwrap_or_default();
        Self::validate(&state)?;
        Ok(state)
    }

    pub fn save(&self, room: &mut RoomLock) -> Result<()> {
        self.validate()?;
        room.update_control(|control| {
            control.insert("policy_state".into(), serde_json::to_value(self)?);
            Ok(())
        })
    }

    /// Call only after native normal shutdown, process reaping and output draining.
    pub fn record_clean_shutdown(&mut self, _now: DateTime<Utc>) {
        if let Some(activity) = &mut self.activity
            && !activity.interrupted
        {
            activity.clean_shutdown = true;
        }
    }

    /// Invoke after startup cleanup confirms no previous updater is still writing.
    pub fn resume_after_restart(&mut self, now: DateTime<Utc>) -> Result<()> {
        self.last_wall = None;
        self.last_monotonic = None;
        if matches!(self.mods.phase, ModPhase::Updating) {
            self.finish_mod_update(now, Err("previous mod update was interrupted".into()))?;
        }
        if let Some(activity) = &mut self.activity
            && !activity.clean_shutdown
        {
            activity.interrupted = true;
        }
        Ok(())
    }

    pub fn request_mod_update(&mut self) -> Result<()> {
        self.mods.request()?;
        self.mods.manual = true;
        Ok(())
    }

    pub fn finish_mod_update(
        &mut self,
        now: DateTime<Utc>,
        outcome: std::result::Result<(), String>,
    ) -> Result<()> {
        ensure!(
            matches!(self.mods.phase, ModPhase::Updating),
            "no mod update is running"
        );
        match outcome {
            Ok(()) => {
                self.mods = ModMaintenance {
                    observed_outdated: self.mods.observed_outdated,
                    ..ModMaintenance::default()
                }
            }
            Err(error) => {
                self.mods.error = Some(error);
                self.mods.phase = if self.mods.attempts >= MOD_ATTEMPTS {
                    ModPhase::Failed
                } else {
                    ModPhase::Retry {
                        not_before: now + TimeDelta::seconds(MOD_RETRY_DELAY.as_secs() as i64),
                    }
                };
            }
        }
        Ok(())
    }

    fn clock_changed(&mut self, now: DateTime<Utc>, monotonic: Option<Duration>) -> Result<bool> {
        let mut skew = TimeDelta::zero();
        let mut backwards = false;
        if let Some(previous) = self.last_wall {
            let elapsed = now.signed_duration_since(previous);
            backwards = elapsed < TimeDelta::zero();
            if let (Some(before), Some(after)) = (self.last_monotonic, monotonic) {
                let actual = TimeDelta::from_std(after.saturating_sub(before))
                    .context("monotonic interval is out of range")?;
                skew = elapsed - actual;
            } else if elapsed < TimeDelta::zero() {
                skew = elapsed;
            }
        }
        let tolerance = TimeDelta::seconds(CHECK_INTERVAL.as_secs() as i64);
        let changed = backwards || skew < -tolerance || skew > tolerance;
        if changed {
            match &mut self.mods.phase {
                ModPhase::Announcing {
                    deadline,
                    next_notice,
                } => {
                    *deadline = deadline
                        .checked_add_signed(skew)
                        .context("mod countdown is out of range")?;
                    *next_notice = next_notice
                        .checked_add_signed(skew)
                        .context("mod countdown is out of range")?;
                }
                ModPhase::Retry { not_before } => {
                    *not_before = not_before
                        .checked_add_signed(skew)
                        .context("mod retry is out of range")?
                }
                _ => {}
            }
        }
        self.last_wall = Some(now);
        self.last_monotonic = monotonic;
        Ok(changed)
    }

    fn observe_activity(
        &mut self,
        now: DateTime<Utc>,
        inputs: &Inputs,
        clock_changed: bool,
    ) -> Result<()> {
        if clock_changed && let Some(activity) = &mut self.activity {
            activity.interrupted = true;
            activity.clean_shutdown = false;
        }
        if !inputs.reliable() {
            if let Some(activity) = &mut self.activity
                && !activity.clean_shutdown
                && !inputs.clean_stop_in_progress
            {
                activity.interrupted = true;
            }
            return Ok(());
        }
        let sessions = inputs
            .presence
            .iter()
            .map(|(name, value)| (name.clone(), value.session_id.clone()))
            .collect();
        let observations = inputs
            .presence
            .iter()
            .map(|(name, value)| (name.clone(), value.observation.clone()))
            .collect();
        let observed_seconds = inputs
            .presence
            .iter()
            .map(|(name, value)| (name.clone(), value.observed_seconds))
            .collect();
        let continuous = self.activity.as_ref().is_some_and(|previous| {
            !previous.interrupted
                && previous.sessions == sessions
                && (previous.clean_shutdown
                    || previous.observations == observations
                        && inputs.presence.iter().all(|(name, current)| {
                            previous
                                .observed_seconds
                                .get(name)
                                .is_some_and(|old| current.observed_seconds >= *old)
                        }))
        });
        let mut last_active_at = if continuous {
            self.activity.as_ref().unwrap().last_active_at.min(now)
        } else {
            now
        };
        for presence in inputs.presence.values() {
            if presence.client_count != 0 || presence.player_count != 0 {
                last_active_at = now;
            } else if presence.idle_seconds < presence.observed_seconds {
                let idle = TimeDelta::from_std(Duration::try_from_secs_f64(presence.idle_seconds)?)
                    .context("presence idle duration is too large")?;
                let observed_activity = now
                    .checked_sub_signed(idle)
                    .context("presence activity timestamp is out of range")?;
                last_active_at = last_active_at.max(observed_activity);
            }
        }
        self.activity = Some(Activity {
            sessions,
            observations,
            observed_seconds,
            last_active_at,
            clean_shutdown: false,
            interrupted: false,
        });
        Ok(())
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ModMaintenance {
    pub phase: ModPhase,
    pub attempts: u8,
    pub manual: bool,
    pub observed_outdated: bool,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub enum ModPhase {
    #[default]
    Idle,
    Pending,
    Announcing {
        deadline: DateTime<Utc>,
        next_notice: DateTime<Utc>,
    },
    Stopping,
    Updating,
    Retry {
        not_before: DateTime<Utc>,
    },
    Failed,
}

impl ModMaintenance {
    fn validate(&self) -> Result<()> {
        ensure!(self.attempts <= MOD_ATTEMPTS, "mod retry budget is invalid");
        match self.phase {
            ModPhase::Idle => ensure!(self.attempts == 0, "idle mod state retains attempts"),
            ModPhase::Updating => {
                ensure!(self.attempts > 0, "running update has no reserved attempt")
            }
            ModPhase::Retry { .. } => ensure!(
                (1..MOD_ATTEMPTS).contains(&self.attempts),
                "retry state has no remaining attempts"
            ),
            ModPhase::Failed => ensure!(
                self.attempts == MOD_ATTEMPTS,
                "failed mod state has an incomplete budget"
            ),
            ModPhase::Announcing {
                deadline,
                next_notice,
            } => ensure!(next_notice <= deadline, "mod countdown is invalid"),
            _ => {}
        }
        Ok(())
    }

    fn request(&mut self) -> Result<()> {
        ensure!(
            !matches!(self.phase, ModPhase::Updating),
            "a mod update is already writing"
        );
        self.phase = ModPhase::Pending;
        self.attempts = 0;
        self.error = None;
        Ok(())
    }

    fn action(&mut self, now: DateTime<Utc>, inputs: &Inputs) -> Result<Option<Action>> {
        match self.phase {
            ModPhase::Idle | ModPhase::Updating | ModPhase::Failed => return Ok(None),
            ModPhase::Retry { not_before } if now < not_before => return Ok(None),
            ModPhase::Announcing {
                deadline,
                next_notice,
            } if inputs.game_running => {
                if now >= deadline {
                    self.phase = ModPhase::Stopping;
                    return Ok(Some(Action::Stop {
                        reason: StopReason::ModUpdate,
                        message: Some(mod_message(0)),
                    }));
                }
                if now >= next_notice {
                    let seconds = (((deadline - now).num_milliseconds() + 999) / 1000).max(1);
                    self.phase = ModPhase::Announcing {
                        deadline,
                        next_notice: deadline - TimeDelta::seconds(((seconds + 29) / 30 - 1) * 30),
                    };
                    return Ok(Some(Action::Announce {
                        message: mod_message(seconds),
                    }));
                }
                return Ok(None);
            }
            ModPhase::Stopping if inputs.game_running => {
                return Ok(Some(Action::Stop {
                    reason: StopReason::ModUpdate,
                    message: None,
                }));
            }
            _ => {}
        }
        if inputs.game_running {
            if !inputs.all_ready {
                return Ok(None);
            }
            self.phase = ModPhase::Announcing {
                deadline: now + TimeDelta::seconds(60),
                next_notice: now + TimeDelta::seconds(30),
            };
            return Ok(Some(Action::Announce {
                message: mod_message(60),
            }));
        }
        ensure!(
            self.attempts < MOD_ATTEMPTS,
            "mod update retry budget is exhausted"
        );
        self.attempts += 1;
        self.phase = ModPhase::Updating;
        Ok(Some(Action::UpdateMods {
            attempt: self.attempts,
        }))
    }
}

fn mod_message(seconds: i64) -> String {
    let when = if seconds == 0 {
        "即将".to_owned()
    } else if seconds >= 60 {
        format!("将在约 {} 分钟后", (seconds + 59) / 60)
    } else {
        format!("将在{seconds}秒后")
    };
    format!("本房间{when}重启更新 MOD，预计耗时约 5 分钟，请提前安排游戏进度。")
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub schedule: ScheduleState,
    pub new_opening: Option<DateTime<Utc>>,
    pub action: Option<Action>,
    pub fault: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Action {
    Start,
    Stop {
        reason: StopReason,
        message: Option<String>,
    },
    Announce {
        message: String,
    },
    UpdateMods {
        attempt: u8,
    },
    Regenerate {
        expected_sessions: BTreeMap<String, String>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    Schedule,
    ModUpdate,
    ModFailure,
}

pub fn retention(day: u64) -> TimeDelta {
    TimeDelta::hours(match day {
        0..=8 => 6,
        9..=30 => 24,
        31..=70 => 36,
        71..=280 => 72,
        _ => 168,
    })
}

pub fn render_countdown(notice: &Countdown, remaining: f64) -> Result<String> {
    notice.validate()?;
    ensure!(
        remaining.is_finite() && remaining >= 0.0,
        "countdown time must be finite and nonnegative"
    );
    let seconds = remaining.ceil();
    let when = if seconds == 0.0 {
        "即将".to_owned()
    } else if seconds >= 60.0 {
        format!("将在约 {} 分钟后", (seconds / 60.0).ceil())
    } else {
        format!("将在{seconds}秒后")
    };
    let mut output = String::new();
    let mut characters = notice.template.chars().peekable();
    while let Some(character) = characters.next() {
        match character {
            '{' if characters.peek() == Some(&'{') => {
                characters.next();
                output.push('{');
            }
            '}' if characters.peek() == Some(&'}') => {
                characters.next();
                output.push('}');
            }
            '{' => {
                let name: String = characters
                    .by_ref()
                    .take_while(|character| *character != '}')
                    .collect();
                let value = match name.as_str() {
                    "remaining" => seconds.to_string(),
                    "minutes" => (seconds / 60.0).ceil().to_string(),
                    "when" => when.clone(),
                    _ => match &notice.parameters[&name] {
                        Value::String(value) => value.clone(),
                        value => value.to_string(),
                    },
                };
                output.push_str(&value);
            }
            character => output.push(character),
        }
    }
    Ok(output)
}

/// Slow sends never extend the deadline or replay missed notices. Returning false
/// from send stops the countdown; dropping this future performs no extra send.
pub async fn countdown<F, Fut>(notice: &Countdown, mut send: F) -> Result<bool>
where
    F: FnMut(String) -> Fut,
    Fut: Future<Output = Result<bool>>,
{
    notice.validate()?;
    let delay = Duration::try_from_secs_f64(notice.delay)?;
    let interval = Duration::try_from_secs_f64(notice.interval)?;
    let deadline = Instant::now()
        .checked_add(delay)
        .context("countdown deadline is out of range")?;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if !send(render_countdown(notice, remaining.as_secs_f64())?).await? {
            return Ok(false);
        }
        if remaining.is_zero() {
            return Ok(true);
        }
        tokio::time::sleep_until(next_notice(deadline, remaining, interval)).await;
    }
}

fn next_notice(deadline: Instant, remaining: Duration, interval: Duration) -> Instant {
    let offset = remaining.as_nanos().saturating_sub(1) / interval.as_nanos() * interval.as_nanos();
    deadline
        - Duration::new(
            (offset / 1_000_000_000) as u64,
            (offset % 1_000_000_000) as u32,
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn notices_stay_aligned_to_the_monotonic_deadline() {
        let begin = Instant::now();
        let deadline = begin + Duration::from_secs(90);
        assert_eq!(
            next_notice(deadline, Duration::from_secs(90), Duration::from_secs(30)),
            begin + Duration::from_secs(30)
        );
        // A send taking 75 seconds reaches the next loop immediately; its next
        // notice is due at the deadline, not 30 seconds after that slow send.
        assert_eq!(
            next_notice(deadline, Duration::from_secs(15), Duration::from_secs(30)),
            deadline
        );
    }
}
