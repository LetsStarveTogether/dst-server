use std::{collections::BTreeMap, time::Duration};

use chrono::{DateTime, TimeDelta, Utc};
use dst_server::{
    files::RoomLock,
    model::Countdown,
    policy::{
        Action, Inputs, ModPhase, Policy, PolicyState, Presence, StopReason, countdown, retention,
    },
};
use serde_json::json;

fn at(value: &str) -> DateTime<Utc> {
    value.parse().unwrap()
}

fn make_policy(timezone: &str, windows: &[(&str, &str)]) -> Policy {
    serde_json::from_value(json!({
        "timezone": timezone,
        "schedule": windows.iter().map(|(start, end)| json!({"start": start, "end": end})).collect::<Vec<_>>()
    })).unwrap()
}

fn running() -> Inputs {
    Inputs {
        game_running: true,
        all_ready: true,
        expected_shards: vec!["Master".into(), "Caves".into()],
        presence: ["Master", "Caves"]
            .into_iter()
            .map(|name| {
                (
                    name.into(),
                    Presence {
                        session_id: format!("{name}-world"),
                        observation: format!("{name}-observation"),
                        reliable: true,
                        client_count: 0,
                        player_count: 0,
                        idle_seconds: 0.0,
                        observed_seconds: 0.0,
                    },
                )
            })
            .collect(),
        master_day: Some(1),
        ..Inputs::default()
    }
}

fn elapsed(input: &mut Inputs, seconds: f64) {
    for presence in input.presence.values_mut() {
        presence.idle_seconds = seconds;
        presence.observed_seconds = seconds;
    }
}

#[test]
fn schedules_merge_across_midnight_and_include_start_only() {
    let policy = make_policy("Asia/Shanghai", &[("22:00", "02:00"), ("01:00", "03:00")]);
    let before = policy.schedule_at(at("2026-10-01T13:59:59Z")).unwrap();
    assert!(!before.open);
    assert_eq!(before.next_change, Some(at("2026-10-01T14:00:00Z")));
    for now in ["2026-10-01T14:00:00Z", "2026-10-01T18:30:00Z"] {
        let current = policy.schedule_at(at(now)).unwrap();
        assert!(current.open);
        assert_eq!(current.opening_id, Some(at("2026-10-01T14:00:00Z")));
        assert_eq!(current.next_change, Some(at("2026-10-01T19:00:00Z")));
    }
    let closed = policy.schedule_at(at("2026-10-01T19:00:00Z")).unwrap();
    assert!(!closed.open);
    assert_eq!(closed.next_change, Some(at("2026-10-02T14:00:00Z")));
    let always = make_policy("UTC", &[("00:00", "12:00"), ("12:00", "00:00")]);
    assert_eq!(
        always
            .schedule_at(at("2026-10-01T12:00:00Z"))
            .unwrap()
            .next_change,
        None
    );
}

#[test]
fn schedule_dst_has_one_opening_and_skipped_times_advance() {
    let fall = make_policy("America/New_York", &[("01:15", "01:45")]);
    for now in ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"] {
        let current = fall.schedule_at(at(now)).unwrap();
        assert!(current.open);
        assert_eq!(current.opening_id, Some(at("2026-11-01T05:15:00Z")));
        assert_eq!(current.next_change, Some(at("2026-11-01T06:45:00Z")));
    }
    let spring = make_policy("America/New_York", &[("02:15", "04:00")]);
    let current = spring.schedule_at(at("2026-03-08T07:00:00Z")).unwrap();
    assert_eq!(current.opening_id, Some(at("2026-03-08T07:00:00Z")));
    assert_eq!(current.next_change, Some(at("2026-03-08T08:00:00Z")));
    let skipped_date = make_policy("Pacific/Apia", &[("12:00", "13:00")]);
    let current = skipped_date
        .schedule_at(at("2011-12-30T09:00:00Z"))
        .unwrap();
    assert!(!current.open);
    assert_eq!(current.next_change, Some(at("2011-12-30T22:00:00Z")));
}

#[test]
fn schedule_opening_budget_survives_restart_and_clock_rewind() {
    let policy = make_policy("UTC", &[("10:00", "12:00")]);
    let mut state = PolicyState::default();
    let mut input = running();
    let first = at("2026-10-02T10:30:00Z");
    assert_eq!(
        policy.plan(&mut state, first, &input).unwrap().new_opening,
        Some(at("2026-10-02T10:00:00Z"))
    );
    state = serde_json::from_value(serde_json::to_value(state).unwrap()).unwrap();
    state.resume_after_restart(first).unwrap();
    assert_eq!(
        policy.plan(&mut state, first, &input).unwrap().new_opening,
        None
    );
    assert_eq!(
        policy
            .plan(&mut state, first - TimeDelta::days(1), &input)
            .unwrap()
            .new_opening,
        None
    );
    assert_eq!(
        policy.plan(&mut state, first, &input).unwrap().new_opening,
        None
    );
    input.monotonic = Some(Duration::from_secs(0));
    policy.plan(&mut state, first, &input).unwrap();
    state = serde_json::from_value(serde_json::to_value(state).unwrap()).unwrap();
    input.monotonic = Some(Duration::from_secs(30));
    assert_eq!(
        policy
            .plan(&mut state, first + TimeDelta::days(1), &input)
            .unwrap()
            .new_opening,
        None
    );
    input.monotonic = Some(Duration::from_secs(60));
    assert_eq!(
        policy
            .plan(
                &mut state,
                first + TimeDelta::days(1) + TimeDelta::seconds(30),
                &input
            )
            .unwrap()
            .new_opening,
        None
    );
    // A subsequent ordinary opening may receive a fresh recovery budget.
    input.monotonic = None;
    assert!(
        policy
            .plan(&mut state, first + TimeDelta::days(2), &input)
            .unwrap()
            .new_opening
            .is_some()
    );
}

#[test]
fn mod_countdown_stops_then_retries_three_total_attempts() {
    let policy = Policy::default();
    let mut state = PolicyState::default();
    let now = at("2026-10-02T10:00:00Z");
    let mut input = running();
    input.mods_outdated = true;
    assert!(matches!(
        policy.plan(&mut state, now, &input).unwrap().action,
        Some(Action::Announce { .. })
    ));
    assert!(
        policy
            .plan(&mut state, now + TimeDelta::seconds(29), &input)
            .unwrap()
            .action
            .is_none()
    );
    assert!(matches!(
        policy
            .plan(&mut state, now + TimeDelta::seconds(30), &input)
            .unwrap()
            .action,
        Some(Action::Announce { .. })
    ));
    assert!(matches!(
        policy
            .plan(&mut state, now + TimeDelta::seconds(60), &input)
            .unwrap()
            .action,
        Some(Action::Stop {
            reason: StopReason::ModUpdate,
            message: Some(_)
        })
    ));
    input = Inputs::default();
    let mut update_at = now + TimeDelta::seconds(61);
    for attempt in 1..=3 {
        assert_eq!(
            policy.plan(&mut state, update_at, &input).unwrap().action,
            Some(Action::UpdateMods { attempt })
        );
        assert!(
            policy
                .plan(&mut state, update_at, &input)
                .unwrap()
                .action
                .is_none()
        );
        state
            .finish_mod_update(update_at, Err("download failed".into()))
            .unwrap();
        assert!(
            policy
                .plan(&mut state, update_at + TimeDelta::seconds(299), &input)
                .unwrap()
                .action
                .is_none()
        );
        update_at += TimeDelta::seconds(300);
    }
    let failure = policy.plan(&mut state, update_at, &input).unwrap();
    assert!(failure.fault.is_some());
    assert!(failure.action.is_none());
    state.request_mod_update().unwrap();
    assert_eq!(
        policy.plan(&mut state, update_at, &input).unwrap().action,
        Some(Action::UpdateMods { attempt: 1 })
    );
}

#[test]
fn late_countdown_tick_remains_valid_until_its_deadline() {
    let policy = Policy::default();
    let mut state = PolicyState::default();
    let now = at("2026-10-02T10:00:00Z");
    let input = Inputs {
        mods_outdated: true,
        ..running()
    };
    policy.plan(&mut state, now, &input).unwrap();
    let plan = policy
        .plan(
            &mut state,
            now + TimeDelta::microseconds(59_999_500),
            &input,
        )
        .unwrap();
    assert!(
        matches!(plan.action, Some(Action::Announce { ref message }) if message.contains("1秒"))
    );
    assert!(matches!(
        policy
            .plan(&mut state, now + TimeDelta::seconds(60), &input)
            .unwrap()
            .action,
        Some(Action::Stop {
            reason: StopReason::ModUpdate,
            ..
        })
    ));
}

#[test]
fn closing_preempts_mod_notice_and_started_writes_finish_closed() {
    let policy = make_policy("UTC", &[("10:00", "12:00")]);
    let mut state = PolicyState::default();
    let mut input = running();
    input.mods_outdated = true;
    let now = at("2026-10-02T11:59:30Z");
    let warning = policy.plan(&mut state, now, &input).unwrap();
    assert!(
        matches!(warning.action, Some(Action::Announce { ref message }) if message.contains("定时关闭"))
    );
    assert_eq!(state.mods.phase, ModPhase::Pending);
    assert!(matches!(
        policy
            .plan(&mut state, now + TimeDelta::seconds(30), &input)
            .unwrap()
            .action,
        Some(Action::Stop {
            reason: StopReason::Schedule,
            ..
        })
    ));
    input = Inputs::default();
    assert!(
        policy
            .plan(&mut state, now + TimeDelta::minutes(1), &input)
            .unwrap()
            .action
            .is_none()
    );
    let next = at("2026-10-03T10:00:00Z");
    assert_eq!(
        policy.plan(&mut state, next, &input).unwrap().action,
        Some(Action::UpdateMods { attempt: 1 })
    );
    input.writes_in_progress = true;
    assert!(
        policy
            .plan(&mut state, at("2026-10-03T12:00:00Z"), &input)
            .unwrap()
            .action
            .is_none()
    );
    state
        .finish_mod_update(at("2026-10-03T12:01:00Z"), Ok(()))
        .unwrap();
    input.writes_in_progress = false;
    assert!(
        policy
            .plan(&mut state, at("2026-10-03T12:01:00Z"), &input)
            .unwrap()
            .action
            .is_none()
    );
    assert_eq!(
        policy
            .plan(&mut state, at("2026-10-04T10:00:00Z"), &input)
            .unwrap()
            .action,
        Some(Action::Start)
    );
}

#[test]
fn failed_mod_budget_resets_only_on_a_later_opening() {
    let policy = make_policy("UTC", &[("10:00", "12:00")]);
    let now = at("2026-10-02T10:00:00Z");
    let mut state = PolicyState::default();
    policy.plan(&mut state, now, &Inputs::default()).unwrap();
    state.mods.phase = ModPhase::Failed;
    state.mods.attempts = 3;
    assert!(
        policy
            .plan(&mut state, now, &Inputs::default())
            .unwrap()
            .fault
            .is_some()
    );
    let next = policy
        .plan(&mut state, now + TimeDelta::days(1), &Inputs::default())
        .unwrap();
    assert!(next.fault.is_none());
    assert_eq!(next.action, Some(Action::UpdateMods { attempt: 1 }));
}

#[test]
fn idle_requires_continuous_observation_and_strict_threshold() {
    for (day, hours) in [
        (8, 6),
        (9, 24),
        (30, 24),
        (31, 36),
        (70, 36),
        (71, 72),
        (280, 72),
        (281, 168),
    ] {
        assert_eq!(retention(day), TimeDelta::hours(hours));
    }
    let policy = Policy {
        idle_regeneration: true,
        ..Policy::default()
    };
    let mut state = PolicyState::default();
    let mut input = running();
    let now = at("2026-10-02T10:00:00Z");
    policy.plan(&mut state, now, &input).unwrap();
    elapsed(&mut input, 21600.0);
    assert!(
        policy
            .plan(&mut state, now + TimeDelta::hours(6), &input)
            .unwrap()
            .action
            .is_none()
    );
    elapsed(&mut input, 21601.0);
    assert!(matches!(
        policy
            .plan(&mut state, now + TimeDelta::seconds(21601), &input)
            .unwrap()
            .action,
        Some(Action::Regenerate { .. })
    ));
    input.presence.get_mut("Caves").unwrap().client_count = 1;
    policy
        .plan(&mut state, now + TimeDelta::seconds(21602), &input)
        .unwrap();
    input.presence.get_mut("Caves").unwrap().client_count = 0;
    input.presence.get_mut("Caves").unwrap().reliable = false;
    policy
        .plan(&mut state, now + TimeDelta::hours(7), &input)
        .unwrap();
    input.presence.get_mut("Caves").unwrap().reliable = true;
    elapsed(&mut input, 86400.0);
    assert!(
        policy
            .plan(&mut state, now + TimeDelta::days(1), &input)
            .unwrap()
            .action
            .is_none()
    );
    assert_eq!(
        state.activity.as_ref().unwrap().last_active_at,
        now + TimeDelta::days(1)
    );
}

#[test]
fn clean_shutdown_preserves_idle_but_world_changes_and_clock_jumps_reset_it() {
    let policy = Policy {
        idle_regeneration: true,
        ..Policy::default()
    };
    let now = at("2026-10-02T10:00:00Z");
    let mut state = PolicyState::default();
    let mut input = running();
    policy.plan(&mut state, now, &input).unwrap();
    let stopping = Inputs {
        clean_stop_in_progress: true,
        ..Inputs::default()
    };
    policy
        .plan(&mut state, now + TimeDelta::hours(1), &stopping)
        .unwrap();
    state.record_clean_shutdown(now + TimeDelta::hours(1));
    state = serde_json::from_value(serde_json::to_value(state).unwrap()).unwrap();
    state
        .resume_after_restart(now + TimeDelta::hours(2))
        .unwrap();
    for presence in input.presence.values_mut() {
        presence.observation.push_str("-new");
    }
    assert!(matches!(
        policy
            .plan(&mut state, now + TimeDelta::hours(7), &input)
            .unwrap()
            .action,
        Some(Action::Regenerate { .. })
    ));
    input.presence.get_mut("Caves").unwrap().session_id = "different-world".into();
    assert!(
        policy
            .plan(&mut state, now + TimeDelta::days(1), &input)
            .unwrap()
            .action
            .is_none()
    );
    input.monotonic = Some(Duration::from_secs(0));
    policy
        .plan(&mut state, now + TimeDelta::days(1), &input)
        .unwrap();
    state = serde_json::from_value(serde_json::to_value(state).unwrap()).unwrap();
    input.monotonic = Some(Duration::from_secs(30));
    assert!(
        policy
            .plan(&mut state, now + TimeDelta::days(2), &input)
            .unwrap()
            .action
            .is_none()
    );
    assert_eq!(
        state.activity.as_ref().unwrap().last_active_at,
        now + TimeDelta::days(2)
    );
    input.monotonic = Some(Duration::from_secs(60));
    policy
        .plan(
            &mut state,
            now + TimeDelta::days(2) + TimeDelta::seconds(29),
            &input,
        )
        .unwrap();
    assert_eq!(
        state.activity.as_ref().unwrap().last_active_at,
        now + TimeDelta::days(2)
    );
}

#[test]
fn saving_policy_preserves_recovery_and_other_control_keys() {
    let directory = tempfile::tempdir().unwrap();
    let mut room = RoomLock::try_acquire(directory.path()).unwrap();
    room.update_control(|control| {
        control.insert("recovery".into(), json!({"remaining": 2}));
        control.insert("future".into(), json!([1, 2, 3]));
        Ok(())
    })
    .unwrap();
    let expected = make_policy("Asia/Shanghai", &[("09:00", "23:00")]);
    expected.save(&mut room).unwrap();
    PolicyState::default().save(&mut room).unwrap();
    assert_eq!(Policy::load(&room).unwrap(), expected);
    assert_eq!(PolicyState::load(&room).unwrap(), PolicyState::default());
    let control = room.read_control().unwrap();
    assert_eq!(control["recovery"], json!({"remaining": 2}));
    assert_eq!(control["future"], json!([1, 2, 3]));
}

#[tokio::test]
async fn zero_notice_and_cancellation_send_no_extra_messages() {
    let notice = Countdown {
        template: "{{room}} {when} {remaining} {minutes} {why}".into(),
        delay: 0.0,
        interval: 30.0,
        parameters: BTreeMap::from([("why".into(), json!("更新"))]),
    };
    let mut sent = Vec::new();
    assert!(
        countdown(&notice, |message| {
            sent.push(message);
            async { Ok(true) }
        })
        .await
        .unwrap()
    );
    assert_eq!(sent, ["{room} 即将 0 0 更新"]);
    let notice = Countdown {
        delay: 60.0,
        ..notice
    };
    sent.clear();
    assert!(
        !countdown(&notice, |message| {
            sent.push(message);
            async { Ok(false) }
        })
        .await
        .unwrap()
    );
    assert_eq!(sent.len(), 1);
}
