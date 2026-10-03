//! Persist recovery decisions before restarting a game or changing native snapshots.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use crate::{files::RoomLock, lua};

pub const CURRENT_RESTARTS: u8 = 2;
pub const MAX_SNAPSHOT_GAP: u64 = 3;
pub const RETRY_DELAY: Duration = Duration::from_secs(30);
pub const STABLE_RESET: Duration = Duration::from_secs(30 * 60);

/// Entries must come from the native catalog with validated world file paths.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Snapshot {
    pub snapshot_id: u64,
    pub world_file: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Catalog {
    pub session_id: String,
    pub latest_world_file: String,
    pub snapshots: Vec<Snapshot>,
}

pub type Catalogs = BTreeMap<String, Catalog>;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct SavedShard {
    pub session_id: String,
    pub snapshot_id: u64,
    pub world_file: String,
}

pub type SavedRoom = BTreeMap<String, SavedShard>;

/// Only construct LoadFailure after identifying the native loader's failed file
/// and excluding disk, permission, configuration and Mod failures.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Failure {
    Retryable,
    LoadFailure {
        shard: String,
        session_id: String,
        world_file: String,
    },
    Disk,
    Permission,
    Configuration,
    Mods,
    Unknown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ClosedReason {
    Disk,
    Permission,
    Configuration,
    Mods,
    UnknownFailure,
    #[serde(alias = "unproven_rollback_time")]
    UnprovenSnapshots,
    SessionChanged,
    #[serde(alias = "latest_snapshots_differ")]
    SnapshotGapExceeded,
    CurrentSaveChanged,
    UnprovenLoadFailure,
    CurrentRetriesExhausted,
    RollbackAlreadyUsed,
    NoPreviousCompleteSnapshot,
    RecoveryTargetFailed,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TargetPhase {
    Preparing,
    Starting,
    Ready,
    Failed,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct FixedTarget {
    pub snapshot_id: u64,
    pub shards: SavedRoom,
    pub applied: BTreeSet<String>,
    pub phase: TargetPhase,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CurrentRetry {
    pub not_before_ms: u64,
    pub started: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryState {
    pub version: u8,
    pub restarts_used: u8,
    pub last_open_window_ms: Option<u64>,
    pub closed: Option<ClosedReason>,
    pub latest: Option<SavedRoom>,
    pub retry: Option<CurrentRetry>,
    pub target: Option<FixedTarget>,
}

impl Default for RecoveryState {
    fn default() -> Self {
        Self {
            version: 1,
            restarts_used: 0,
            last_open_window_ms: None,
            closed: None,
            latest: None,
            retry: None,
            target: None,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum Decision {
    RetryCurrent {
        attempt: u8,
        not_before_ms: u64,
        shards: SavedRoom,
    },
    Restore {
        target: FixedTarget,
        resumed: bool,
    },
    StayClosed {
        reason: ClosedReason,
    },
}

fn valid_text(value: &str, limit: usize) -> bool {
    !value.is_empty() && value.len() <= limit && !value.chars().any(char::is_control)
}

fn verify_saved(saved: &SavedShard) -> Result<()> {
    ensure!(
        valid_text(&saved.session_id, 128),
        "invalid session identity"
    );
    ensure!(
        saved.snapshot_id > 0 && saved.snapshot_id <= lua::MAX_SAFE_INTEGER as u64,
        "invalid snapshot identity"
    );
    ensure!(
        valid_text(&saved.world_file, 4096),
        "invalid native snapshot path"
    );
    Ok(())
}

impl RecoveryState {
    fn validate(&self) -> Result<()> {
        ensure!(
            self.version == 1 && self.restarts_used <= CURRENT_RESTARTS,
            "invalid recovery state"
        );
        if let Some(latest) = &self.latest {
            ensure!(!latest.is_empty(), "empty saved room");
            for saved in latest.values() {
                verify_saved(saved)?;
            }
        }
        if self.retry.is_some() {
            ensure!(
                self.restarts_used > 0 && self.latest.is_some() && self.closed.is_none(),
                "invalid pending retry"
            );
        }
        if let Some(target) = &self.target {
            ensure!(!target.shards.is_empty(), "empty recovery target");
            for saved in target.shards.values() {
                verify_saved(saved)?;
                ensure!(
                    saved.snapshot_id == target.snapshot_id,
                    "inconsistent recovery target"
                );
            }
            ensure!(
                target
                    .applied
                    .iter()
                    .all(|shard| target.shards.contains_key(shard)),
                "unknown applied shard"
            );
            if matches!(target.phase, TargetPhase::Starting | TargetPhase::Ready) {
                ensure!(
                    target.applied.len() == target.shards.len(),
                    "recovery target is not applied everywhere"
                );
            }
        }
        Ok(())
    }

    fn close(&mut self, reason: ClosedReason) -> Decision {
        self.closed = Some(reason);
        self.retry = None;
        if let Some(target) = &mut self.target
            && matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
        {
            target.phase = TargetPhase::Failed;
        }
        Decision::StayClosed { reason }
    }

    fn reset(&mut self) {
        self.restarts_used = 0;
        self.closed = None;
        self.latest = None;
        self.retry = None;
        if self
            .target
            .as_ref()
            .is_some_and(|target| target.phase == TargetPhase::Ready)
        {
            self.target = None;
        } else if let Some(target) = &mut self.target {
            // An interrupted truncate may have happened before its receipt was persisted.
            // Even a fresh budget must finish this same target instead of selecting an older one.
            target.phase = TargetPhase::Preparing;
        }
    }
}

pub fn load(room: &RoomLock) -> Result<RecoveryState> {
    decode(&room.read_control()?)
}

fn decode(document: &serde_json::Map<String, serde_json::Value>) -> Result<RecoveryState> {
    let state: RecoveryState = match document.get("recovery") {
        Some(value) => serde_json::from_value(value.clone()).context("read recovery state")?,
        None => RecoveryState::default(),
    };
    state.validate()?;
    Ok(state)
}

fn update<T>(
    room: &mut RoomLock,
    change: impl FnOnce(&mut RecoveryState) -> Result<T>,
) -> Result<T> {
    room.update_control(|document| {
        let mut state = decode(document)?;
        let result = change(&mut state)?;
        state.validate()?;
        document.insert("recovery".into(), serde_json::to_value(state)?);
        // update_control synchronizes the file and directory before returning this decision.
        Ok(result)
    })
}

fn saved(catalog: &Catalog, snapshot: &Snapshot) -> Result<SavedShard> {
    let saved = SavedShard {
        session_id: catalog.session_id.clone(),
        snapshot_id: snapshot.snapshot_id,
        world_file: snapshot
            .world_file
            .clone()
            .context("snapshot world file is missing")?,
    };
    verify_saved(&saved)?;
    Ok(saved)
}

fn latest_room(required: &BTreeSet<String>, catalogs: &Catalogs) -> Result<SavedRoom> {
    ensure!(
        !required.is_empty() && catalogs.keys().eq(required.iter()),
        "required shard catalogs differ"
    );
    required
        .iter()
        .map(|name| {
            let catalog = &catalogs[name];
            let mut ids = BTreeSet::new();
            ensure!(
                catalog
                    .snapshots
                    .iter()
                    .all(|snapshot| ids.insert(snapshot.snapshot_id)),
                "duplicate snapshot identity"
            );
            let latest = catalog
                .snapshots
                .iter()
                .find(|snapshot| snapshot.world_file.as_deref() == Some(&catalog.latest_world_file))
                .context("latest snapshot is absent from its catalog")?;
            Ok((name.clone(), saved(catalog, latest)?))
        })
        .collect()
}

fn previous_target(
    latest: &SavedRoom,
    catalogs: &Catalogs,
    failed_snapshot_id: u64,
) -> std::result::Result<FixedTarget, ClosedReason> {
    let newest = latest
        .values()
        .map(|saved| saved.snapshot_id)
        .max()
        .unwrap();
    let oldest = latest
        .values()
        .map(|saved| saved.snapshot_id)
        .min()
        .unwrap();
    let first = catalogs.values().next().unwrap();
    let selected = first
        .snapshots
        .iter()
        .filter(|snapshot| {
            snapshot.snapshot_id > 0
                && snapshot.snapshot_id < failed_snapshot_id
                && snapshot.snapshot_id <= oldest
        })
        .filter(|snapshot| {
            catalogs.values().all(|catalog| {
                catalog.snapshots.iter().any(|other| {
                    other.snapshot_id == snapshot.snapshot_id && other.world_file.is_some()
                })
            })
        })
        .map(|snapshot| snapshot.snapshot_id)
        .max()
        .ok_or(ClosedReason::NoPreviousCompleteSnapshot)?;
    if newest - selected > MAX_SNAPSHOT_GAP {
        return Err(ClosedReason::SnapshotGapExceeded);
    }
    let mut shards = BTreeMap::new();
    for (name, catalog) in catalogs {
        let snapshot = catalog
            .snapshots
            .iter()
            .find(|snapshot| snapshot.snapshot_id == selected)
            .unwrap();
        let target = saved(catalog, snapshot).map_err(|_| ClosedReason::UnprovenSnapshots)?;
        shards.insert(name.clone(), target);
    }
    Ok(FixedTarget {
        snapshot_id: selected,
        shards,
        applied: BTreeSet::new(),
        phase: TargetPhase::Preparing,
    })
}

fn verify_replay(
    target: &FixedTarget,
    required: &BTreeSet<String>,
    catalogs: &Catalogs,
) -> std::result::Result<(), ClosedReason> {
    let latest = latest_room(required, catalogs).map_err(|_| ClosedReason::UnprovenSnapshots)?;
    if target.shards.keys().ne(latest.keys()) {
        return Err(ClosedReason::SessionChanged);
    }
    for (name, expected) in &target.shards {
        let current = &latest[name];
        if current.session_id != expected.session_id {
            return Err(ClosedReason::SessionChanged);
        }
        let snapshot = catalogs[name]
            .snapshots
            .iter()
            .find(|snapshot| snapshot.snapshot_id == target.snapshot_id)
            .ok_or(ClosedReason::NoPreviousCompleteSnapshot)?;
        let found =
            saved(&catalogs[name], snapshot).map_err(|_| ClosedReason::UnprovenSnapshots)?;
        if &found != expected || current.snapshot_id < target.snapshot_id {
            return Err(ClosedReason::CurrentSaveChanged);
        }
        if current.snapshot_id - target.snapshot_id > MAX_SNAPSHOT_GAP {
            return Err(ClosedReason::SnapshotGapExceeded);
        }
    }
    Ok(())
}

fn decide(
    state: &mut RecoveryState,
    failure: &Failure,
    required: &BTreeSet<String>,
    catalogs: &Catalogs,
    now_ms: u64,
) -> Result<Decision> {
    if let Some(reason) = state.closed {
        return Ok(Decision::StayClosed { reason });
    }
    let fatal = match failure {
        Failure::Disk => Some(ClosedReason::Disk),
        Failure::Permission => Some(ClosedReason::Permission),
        Failure::Configuration => Some(ClosedReason::Configuration),
        Failure::Mods => Some(ClosedReason::Mods),
        Failure::Unknown => Some(ClosedReason::UnknownFailure),
        Failure::Retryable | Failure::LoadFailure { .. } => None,
    };
    if let Some(reason) = fatal {
        return Ok(state.close(reason));
    }
    if state.target.as_ref().is_some_and(|target| {
        matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
    }) {
        return Ok(state.close(ClosedReason::RecoveryTargetFailed));
    }
    let Ok(latest) = latest_room(required, catalogs) else {
        return Ok(state.close(ClosedReason::UnprovenSnapshots));
    };
    if let Some(previous) = &state.latest
        && (previous.keys().ne(latest.keys())
            || previous
                .iter()
                .any(|(name, saved)| saved.session_id != latest[name].session_id))
    {
        return Ok(state.close(ClosedReason::SessionChanged));
    }
    let failed_snapshot_id = if let Failure::LoadFailure {
        shard,
        session_id,
        world_file,
    } = failure
    {
        let Some(failed) = latest
            .get(shard)
            .filter(|saved| &saved.session_id == session_id && &saved.world_file == world_file)
        else {
            return Ok(state.close(ClosedReason::UnprovenLoadFailure));
        };
        Some(failed.snapshot_id)
    } else {
        None
    };
    let newest = latest
        .values()
        .map(|saved| saved.snapshot_id)
        .max()
        .unwrap();
    let oldest = latest
        .values()
        .map(|saved| saved.snapshot_id)
        .min()
        .unwrap();
    if newest - oldest > MAX_SNAPSHOT_GAP {
        return Ok(state.close(ClosedReason::SnapshotGapExceeded));
    }
    state.latest = Some(latest.clone());
    if state.restarts_used < CURRENT_RESTARTS {
        state.restarts_used += 1;
        let not_before_ms = now_ms
            .checked_add(RETRY_DELAY.as_millis() as u64)
            .context("retry deadline overflow")?;
        state.retry = Some(CurrentRetry {
            not_before_ms,
            started: false,
        });
        return Ok(Decision::RetryCurrent {
            attempt: state.restarts_used,
            not_before_ms,
            shards: latest,
        });
    }
    let Some(failed_snapshot_id) = failed_snapshot_id else {
        return Ok(state.close(ClosedReason::CurrentRetriesExhausted));
    };
    if state.target.is_some() {
        return Ok(state.close(ClosedReason::RollbackAlreadyUsed));
    }
    let target = match previous_target(&latest, catalogs, failed_snapshot_id) {
        Ok(target) => target,
        Err(reason) => return Ok(state.close(reason)),
    };
    state.retry = None;
    state.target = Some(target.clone());
    Ok(Decision::Restore {
        target,
        resumed: false,
    })
}

/// Call only after all game processes have stopped and catalogs are read afresh.
/// Returning a retry/restore proves the decision was persisted before native mutation.
pub fn on_failure(
    room: &mut RoomLock,
    failure: Failure,
    required: &BTreeSet<String>,
    catalogs: &Catalogs,
    now_ms: u64,
) -> Result<Decision> {
    update(room, |state| {
        decide(state, &failure, required, catalogs, now_ms)
    })
}

/// Resume the saved target; an interrupted current-save start consumes its existing slot.
/// If that start had already begun, reserve the next available retry rather than repeating it indefinitely.
pub fn resume_pending(
    room: &mut RoomLock,
    required: &BTreeSet<String>,
    catalogs: &Catalogs,
    now_ms: u64,
) -> Result<Option<Decision>> {
    update(room, |state| {
        if let Some(reason) = state.closed {
            return Ok(Some(Decision::StayClosed { reason }));
        }
        if let Some(target) = &state.target
            && matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
        {
            if let Err(reason) = verify_replay(target, required, catalogs) {
                return Ok(Some(state.close(reason)));
            }
            let target = state.target.as_mut().unwrap();
            target.phase = TargetPhase::Preparing;
            return Ok(Some(Decision::Restore {
                target: target.clone(),
                resumed: true,
            }));
        }
        if let Some(retry) = &state.retry {
            if retry.started {
                return decide(state, &Failure::Retryable, required, catalogs, now_ms).map(Some);
            }
            if latest_room(required, catalogs).ok().as_ref() != state.latest.as_ref() {
                return Ok(Some(state.close(ClosedReason::CurrentSaveChanged)));
            }
            return Ok(Some(Decision::RetryCurrent {
                attempt: state.restarts_used,
                not_before_ms: retry.not_before_ms,
                shards: state.latest.clone().context("retry lost its saved room")?,
            }));
        }
        Ok(None)
    })
}

/// Reserve process launch durably; the caller must recheck the returned snapshot identities before loading.
pub fn begin_current_retry(room: &mut RoomLock, now_ms: u64) -> Result<()> {
    update(room, |state| {
        let retry = state
            .retry
            .as_mut()
            .context("no current-save retry is pending")?;
        ensure!(
            !retry.started && now_ms >= retry.not_before_ms && state.closed.is_none(),
            "retry is not ready to start"
        );
        retry.started = true;
        Ok(())
    })
}

pub fn mark_applied(
    room: &mut RoomLock,
    shard: &str,
    session_id: &str,
    snapshot_id: u64,
    world_file: &str,
) -> Result<()> {
    update(room, |state| {
        ensure!(state.closed.is_none(), "room is closed after failure");
        let target = state.target.as_mut().context("no recovery target")?;
        ensure!(
            target.phase == TargetPhase::Preparing,
            "recovery is not preparing"
        );
        let expected = target.shards.get(shard).context("unknown recovery shard")?;
        ensure!(
            expected.session_id == session_id
                && expected.snapshot_id == snapshot_id
                && expected.world_file == world_file,
            "native recovery receipt differs from fixed target"
        );
        target.applied.insert(shard.to_owned());
        Ok(())
    })
}

pub fn begin_recovered_room(room: &mut RoomLock) -> Result<()> {
    update(room, |state| {
        ensure!(state.closed.is_none(), "room is closed after failure");
        let target = state.target.as_mut().context("no recovery target")?;
        ensure!(
            target.phase == TargetPhase::Preparing && target.applied.len() == target.shards.len(),
            "recovery is not applied on every shard"
        );
        target.phase = TargetPhase::Starting;
        Ok(())
    })
}

/// Full-room readiness ends this incident, while its budget remains spent until a permitted reset.
pub fn mark_ready(room: &mut RoomLock) -> Result<()> {
    update(room, |state| {
        ensure!(state.closed.is_none(), "closed room cannot become ready");
        ensure!(
            state.retry.as_ref().is_none_or(|retry| retry.started),
            "reserved retry has not started"
        );
        if let Some(target) = &mut state.target {
            ensure!(
                matches!(target.phase, TargetPhase::Starting | TargetPhase::Ready),
                "fixed target has not started"
            );
            target.phase = TargetPhase::Ready;
        }
        state.latest = None;
        state.retry = None;
        Ok(())
    })
}

/// The runner measures continuous full-room readiness with a monotonic clock.
/// It must restart that measurement whenever the Agent process or any shard restarts.
pub fn reset_after_stable(room: &mut RoomLock, continuous_ready: Duration) -> Result<bool> {
    if continuous_ready < STABLE_RESET {
        return Ok(false);
    }
    update(room, |state| {
        ensure!(
            state.closed.is_none() && state.latest.is_none() && state.retry.is_none(),
            "room is not continuously ready"
        );
        ensure!(
            state
                .target
                .as_ref()
                .is_none_or(|target| target.phase == TargetPhase::Ready),
            "recovery remains unfinished"
        );
        state.reset();
        Ok(true)
    })
}

/// Use the resolved UTC opening timestamp, never a repeating name such as "morning".
pub fn reset_for_open_window(room: &mut RoomLock, window_start_ms: u64) -> Result<bool> {
    update(room, |state| {
        if state
            .last_open_window_ms
            .is_some_and(|previous| window_start_ms <= previous)
        {
            return Ok(false);
        }
        state.last_open_window_ms = Some(window_start_ms);
        state.reset();
        Ok(true)
    })
}

pub fn explicit_start(room: &mut RoomLock, always_open: bool) -> Result<bool> {
    update(room, |state| {
        if !always_open || state.closed.is_none() {
            return Ok(false);
        }
        state.reset();
        Ok(true)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn path(session: &str, id: u64) -> String {
        format!("save/session/{session}/{id:010}")
    }

    fn catalogs() -> (BTreeSet<String>, Catalogs) {
        let required = BTreeSet::from(["Caves".into(), "Master".into()]);
        let catalogs = ["Caves", "Master"]
            .into_iter()
            .map(|name| {
                let session = format!("SESSION_{name}");
                let snapshots = [12, 11, 10, 9, 8]
                    .into_iter()
                    .map(|id| Snapshot {
                        snapshot_id: id,
                        world_file: Some(path(&session, id)),
                    })
                    .collect();
                (
                    name.to_owned(),
                    Catalog {
                        latest_world_file: path(&session, 12),
                        session_id: session,
                        snapshots,
                    },
                )
            })
            .collect();
        (required, catalogs)
    }

    fn load_failure(catalogs: &Catalogs) -> Failure {
        Failure::LoadFailure {
            shard: "Master".into(),
            session_id: catalogs["Master"].session_id.clone(),
            world_file: catalogs["Master"].latest_world_file.clone(),
        }
    }

    #[test]
    fn latest_snapshot_gap_allows_three_and_rejects_four_for_either_shard() {
        for lagging in ["Master", "Caves"] {
            for gap in 0..=4 {
                let (required, mut catalogs) = catalogs();
                let catalog = catalogs.get_mut(lagging).unwrap();
                catalog.latest_world_file = path(&catalog.session_id, 12 - gap);
                for failure in [Failure::Retryable, load_failure(&catalogs)] {
                    let directory = tempfile::tempdir().unwrap();
                    let mut room = RoomLock::try_acquire(directory.path()).unwrap();
                    let decision = on_failure(&mut room, failure, &required, &catalogs, 0).unwrap();
                    if gap <= 3 {
                        assert!(matches!(
                            decision,
                            Decision::RetryCurrent { attempt: 1, .. }
                        ));
                    } else {
                        assert!(matches!(
                            decision,
                            Decision::StayClosed {
                                reason: ClosedReason::SnapshotGapExceeded
                            }
                        ));
                        assert_eq!(load(&room).unwrap().restarts_used, 0);
                    }
                    assert!(load(&room).unwrap().target.is_none());
                }
            }
        }
    }

    #[test]
    fn snapshot_gap_uses_the_whole_room_range() {
        let (mut required, mut catalogs) = catalogs();
        let caves = catalogs.get_mut("Caves").unwrap();
        caves.latest_world_file = path(&caves.session_id, 8);
        let mut extra = catalogs["Master"].clone();
        extra.latest_world_file = path(&extra.session_id, 10);
        catalogs.insert("Extra".into(), extra);
        required.insert("Extra".into());
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        assert!(matches!(
            on_failure(&mut room, Failure::Retryable, &required, &catalogs, 0).unwrap(),
            Decision::StayClosed {
                reason: ClosedReason::SnapshotGapExceeded
            }
        ));
    }

    #[test]
    fn interrupted_current_attempts_keep_the_budget_and_delay() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let (required, catalogs) = catalogs();
        let result = on_failure(&mut room, Failure::Retryable, &required, &catalogs, 100).unwrap();
        assert!(matches!(
            result,
            Decision::RetryCurrent {
                attempt: 1,
                not_before_ms: 30_100,
                ..
            }
        ));
        assert!(begin_current_retry(&mut room, 30_099).is_err());
        begin_current_retry(&mut room, 30_100).unwrap();
        drop(room);
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let resumed = resume_pending(&mut room, &required, &catalogs, 31_000)
            .unwrap()
            .unwrap();
        assert!(matches!(
            resumed,
            Decision::RetryCurrent {
                attempt: 2,
                not_before_ms: 61_000,
                ..
            }
        ));
        let waiting = resume_pending(&mut room, &required, &catalogs, 40_000)
            .unwrap()
            .unwrap();
        assert!(matches!(
            waiting,
            Decision::RetryCurrent {
                attempt: 2,
                not_before_ms: 61_000,
                ..
            }
        ));
        begin_current_retry(&mut room, 61_000).unwrap();
        drop(room);
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let exhausted = resume_pending(&mut room, &required, &catalogs, 62_000)
            .unwrap()
            .unwrap();
        assert!(matches!(
            exhausted,
            Decision::StayClosed {
                reason: ClosedReason::CurrentRetriesExhausted
            }
        ));
        assert_eq!(load(&room).unwrap().restarts_used, 2);
        assert!(!explicit_start(&mut room, false).unwrap());
        assert!(explicit_start(&mut room, true).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 0);
    }

    #[test]
    fn fixed_target_survives_partial_application_and_never_falls_back() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        room.update_control(|control| {
            control.insert("policy".into(), json!({"paused": false}));
            Ok(())
        })
        .unwrap();
        let (required, mut catalogs) = catalogs();
        for now in [0, 30_000] {
            on_failure(
                &mut room,
                load_failure(&catalogs),
                &required,
                &catalogs,
                now,
            )
            .unwrap();
            begin_current_retry(&mut room, now + 30_000).unwrap();
        }
        let Decision::Restore {
            target,
            resumed: false,
        } = on_failure(
            &mut room,
            load_failure(&catalogs),
            &required,
            &catalogs,
            60_001,
        )
        .unwrap()
        else {
            panic!("expected one fixed rollback")
        };
        assert_eq!(target.snapshot_id, 11);
        let stored = room.read_control().unwrap();
        assert_eq!(stored["recovery"]["target"]["snapshot_id"], 11);
        assert_eq!(stored["policy"], json!({"paused": false}));
        assert!(begin_recovered_room(&mut room).is_err());
        let master = &target.shards["Master"];
        mark_applied(
            &mut room,
            "Master",
            &master.session_id,
            11,
            &master.world_file,
        )
        .unwrap();
        for catalog in catalogs.values_mut() {
            catalog.latest_world_file = path(&catalog.session_id, 11);
        }
        // Caves was also truncated, but the Agent died before persisting its receipt.
        drop(room);
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let Decision::Restore {
            target: resumed,
            resumed: true,
        } = resume_pending(&mut room, &required, &catalogs, 70_000)
            .unwrap()
            .unwrap()
        else {
            panic!("expected exact target replay")
        };
        assert_eq!(resumed.snapshot_id, 11);
        assert_eq!(resumed.applied, BTreeSet::from(["Master".into()]));
        let caves = &resumed.shards["Caves"];
        assert!(
            mark_applied(&mut room, "Caves", &caves.session_id, 10, &caves.world_file).is_err()
        );
        mark_applied(&mut room, "Caves", &caves.session_id, 11, &caves.world_file).unwrap();
        begin_recovered_room(&mut room).unwrap();
        let failure = on_failure(
            &mut room,
            load_failure(&catalogs),
            &required,
            &catalogs,
            80_000,
        )
        .unwrap();
        assert!(matches!(
            failure,
            Decision::StayClosed {
                reason: ClosedReason::RecoveryTargetFailed
            }
        ));
        assert!(reset_for_open_window(&mut room, 100_000).unwrap());
        let Decision::Restore { target, .. } =
            resume_pending(&mut room, &required, &catalogs, 100_000)
                .unwrap()
                .unwrap()
        else {
            panic!("new window must retain partially applied target")
        };
        assert_eq!(target.snapshot_id, 11);
    }

    #[test]
    fn started_retries_accept_native_sync_but_pending_retries_keep_their_saved_room() {
        let (required, mut catalogs) = catalogs();
        let caves = catalogs.get_mut("Caves").unwrap();
        caves.latest_world_file = path(&caves.session_id, 9);
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        on_failure(&mut room, Failure::Retryable, &required, &catalogs, 0).unwrap();
        begin_current_retry(&mut room, 30_000).unwrap();
        let caves = catalogs.get_mut("Caves").unwrap();
        caves.latest_world_file = path(&caves.session_id, 12);
        let resumed = resume_pending(&mut room, &required, &catalogs, 31_000)
            .unwrap()
            .unwrap();
        assert!(matches!(
            resumed,
            Decision::RetryCurrent {
                attempt: 2,
                not_before_ms: 61_000,
                ..
            }
        ));
        let master = catalogs.get_mut("Master").unwrap();
        master.latest_world_file = path(&master.session_id, 11);
        assert!(matches!(
            resume_pending(&mut room, &required, &catalogs, 40_000)
                .unwrap()
                .unwrap(),
            Decision::StayClosed {
                reason: ClosedReason::CurrentSaveChanged
            }
        ));
    }

    #[test]
    fn rollback_precedes_the_failed_snapshot_and_never_loses_more_than_three() {
        let (required, mut catalogs) = catalogs();
        let master = catalogs.get_mut("Master").unwrap();
        master.latest_world_file = path(&master.session_id, 11);
        let latest = latest_room(&required, &catalogs).unwrap();
        assert_eq!(
            previous_target(&latest, &catalogs, 11).unwrap().snapshot_id,
            10
        );

        let master = catalogs.get_mut("Master").unwrap();
        master.latest_world_file = path(&master.session_id, 9);
        let latest = latest_room(&required, &catalogs).unwrap();
        assert!(matches!(
            previous_target(&latest, &catalogs, 9),
            Err(ClosedReason::SnapshotGapExceeded)
        ));

        for catalog in catalogs.values_mut() {
            catalog.latest_world_file = path(&catalog.session_id, 12);
            catalog
                .snapshots
                .retain(|snapshot| ![10, 11].contains(&snapshot.snapshot_id));
        }
        let latest = latest_room(&required, &catalogs).unwrap();
        let target = previous_target(&latest, &catalogs, 12).unwrap();
        assert_eq!(target.snapshot_id, 9);
        verify_replay(&target, &required, &catalogs).unwrap();
        let master = catalogs.get_mut("Master").unwrap();
        master.latest_world_file = path(&master.session_id, 13);
        master.snapshots.push(Snapshot {
            snapshot_id: 13,
            world_file: Some(master.latest_world_file.clone()),
        });
        assert!(matches!(
            verify_replay(&target, &required, &catalogs),
            Err(ClosedReason::SnapshotGapExceeded)
        ));

        for catalog in catalogs.values_mut() {
            catalog.latest_world_file = path(&catalog.session_id, 12);
            catalog
                .snapshots
                .retain(|snapshot| snapshot.snapshot_id != 9);
        }
        assert!(matches!(
            previous_target(&latest, &catalogs, 12),
            Err(ClosedReason::SnapshotGapExceeded)
        ));
    }

    #[test]
    fn missing_world_files_and_known_failures_never_use_retries() {
        let (required, catalogs) = catalogs();
        for failure in [
            Failure::Disk,
            Failure::Permission,
            Failure::Configuration,
            Failure::Mods,
            Failure::Unknown,
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            assert!(matches!(
                on_failure(&mut room, failure, &required, &catalogs, 0).unwrap(),
                Decision::StayClosed { .. }
            ));
            assert_eq!(load(&room).unwrap().restarts_used, 0);
        }
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let mut invalid = catalogs.clone();
        invalid.get_mut("Master").unwrap().snapshots[0].world_file = None;
        assert!(matches!(
            on_failure(&mut room, load_failure(&invalid), &required, &invalid, 0).unwrap(),
            Decision::StayClosed {
                reason: ClosedReason::UnprovenSnapshots
            }
        ));
        assert!(load(&room).unwrap().target.is_none());

        for catalog in invalid.values_mut() {
            catalog
                .snapshots
                .retain(|snapshot| snapshot.snapshot_id == 12);
        }
        let latest = latest_room(&required, &catalogs).unwrap();
        assert!(matches!(
            previous_target(&latest, &invalid, 12),
            Err(ClosedReason::NoPreviousCompleteSnapshot)
        ));
    }

    #[test]
    fn only_new_openings_or_continuous_readiness_reset_spent_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let (required, catalogs) = catalogs();
        assert!(reset_for_open_window(&mut room, 1_000).unwrap());
        on_failure(&mut room, Failure::Retryable, &required, &catalogs, 2_000).unwrap();
        assert!(!reset_for_open_window(&mut room, 1_000).unwrap());
        assert!(!reset_for_open_window(&mut room, 999).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 1);
        begin_current_retry(&mut room, 32_000).unwrap();
        mark_ready(&mut room).unwrap();
        assert!(!reset_after_stable(&mut room, Duration::from_secs(1799)).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 1);
        assert!(reset_after_stable(&mut room, STABLE_RESET).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 0);
        assert!(!reset_for_open_window(&mut room, 1_000).unwrap());
        on_failure(&mut room, Failure::Retryable, &required, &catalogs, 40_000).unwrap();
        assert!(reset_for_open_window(&mut room, 2_000).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 0);
    }
}
