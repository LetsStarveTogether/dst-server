//! Real subprocess fixtures verify room ownership without launching or editing a live game.
use dst_server::{
    driver::DriverOptions,
    files::RoomLock,
    model::{Countdown, Envelope, ErrorCode, Outcome, Phase, Request, StopResult, Target},
    room::Room,
    rpc::EventHub,
};
use serde_json::{Value, json};
use std::{fs, os::unix::fs::PermissionsExt, sync::Arc, time::Duration};
use tokio::time::{sleep, timeout};

const GAME: &str = include_str!("fixtures/game.py");

fn room() -> (tempfile::TempDir, Arc<Room>) {
    room_with_shards(&[("Master", 1, 19100), ("Caves", 2, 19200)])
}

fn room_with_shards(shards: &[(&str, u16, u16)]) -> (tempfile::TempDir, Arc<Room>) {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("cluster");
    fs::create_dir(&root).unwrap();
    fs::write(root.join("cluster_token.txt"), "").unwrap();
    fs::write(
        root.join("cluster.ini"),
        "[SHARD]\nshard_enabled=true\nmaster_port=19000\nmaster_ip=127.0.0.1\ncluster_key=test-room-key\n",
    )
    .unwrap();
    for &(name, id, port) in shards {
        fs::create_dir(root.join(name)).unwrap();
        let save = root.join(name).join(format!("save/session/S_{name}"));
        fs::create_dir_all(&save).unwrap();
        let index = json!({
            "session_id": format!("S_{name}"),
            "server": {},
            "world": {"options": {
                "id": "CUSTOM", "name": name, "desc": "",
                "location": if id == 1 { "forest" } else { "cave" },
                "overrides": {"task_set": if id == 1 { "default" } else { "cave_default" }},
            }},
        });
        fs::write(
            root.join(name).join("save/shardindex"),
            format!(
                "KLEI     1 return {}",
                dst_server::lua::render_literal(&index).unwrap()
            ),
        )
        .unwrap();
        for id in 1..=4 {
            fs::write(save.join(format!("{id:010}")), "native-save").unwrap();
            fs::write(
                save.join(format!("{id:010}.meta")),
                format!(
                    "KLEI 1 return {{ clock={{ cycles={} }} }}\0",
                    if id >= 3 { 2 } else { 1 }
                ),
            )
            .unwrap();
        }
        fs::write(root.join(name).join("server.ini"),format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n",id==1,port+1)).unwrap();
    }
    let game = temp.path().join("game.py");
    fs::write(&game, GAME).unwrap();
    fs::set_permissions(&game, fs::Permissions::from_mode(0o700)).unwrap();
    let room = Room::open(
        &root,
        &game,
        DriverOptions::default(),
        EventHub::default(),
        Default::default(),
    )
    .unwrap();
    (temp, room)
}
async fn invoke(
    room: &Arc<Room>,
    request: Request,
) -> dst_server::model::Result<serde_json::Value> {
    room.invoke(Envelope::new(Target::Room, request).unwrap())
        .await
}
async fn until(room: &Arc<Room>, condition: impl Fn() -> bool) {
    timeout(Duration::from_secs(5), async {
        while !condition() {
            sleep(Duration::from_millis(10)).await
        }
    })
    .await
    .expect("room condition");
    assert!(
        room.status().operations.current.is_none()
            || matches!(
                room.status().phase,
                Phase::Running | Phase::Starting | Phase::Stopping
            )
    );
}

#[tokio::test]
async fn repeated_start_and_restart_keep_status_history_bounded() {
    let (_temp, room) = room();
    for _ in 0..32 {
        assert_eq!(
            invoke(&room, Request::Start {}).await.unwrap()["phase"],
            "running"
        );
    }
    assert_eq!(
        invoke(&room, Request::Restart { notice: None })
            .await
            .unwrap()["phase"],
        "running"
    );
    let status = invoke(&room, Request::Status {}).await.unwrap();
    assert_eq!(status["operations"]["last"]["method"], "restart");
    assert!(status["operations"]["last"]["completed_at_ns"].is_u64());
    assert!(status["operations"]["last"]["error"].is_null());
    assert!(serde_json::to_vec(&status).unwrap().len() < 16 * 1024);
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn start_waits_for_native_sync_during_runtime_and_connection_checks() {
    for method in ["runtime", "connected_shards"] {
        let (temp, room) = room();
        let caves = temp.path().join("cluster/Caves");
        fs::write(caves.join(format!("reload-during-{method}")), "").unwrap();
        timeout(Duration::from_secs(5), invoke(&room, Request::Start {}))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(room.status().phase, Phase::Running);
        assert!(!room.requires_container_restart());
        let states = room.driver_states();
        assert!(states.values().all(|state| state.ready));
        assert_eq!(states["Caves"].generation, Some(2));
        assert_eq!(
            fs::read_to_string(caves.join("native-lifecycle")).unwrap(),
            "start\nreload\n"
        );
        room.shutdown().await.unwrap();
    }
}

#[tokio::test]
async fn invalid_launch_options_fail_before_starting_games() {
    let (temp, room) = room();
    room.configure_driver_control(json!({"observe_loads":false}))
        .unwrap();
    assert!(invoke(&room, Request::Start {}).await.is_err());
    assert_eq!(room.status().phase, Phase::Failed);
    assert!(!temp.path().join("cluster/Master/native-lifecycle").exists());
    assert!(!temp.path().join("cluster/Caves/native-lifecycle").exists());
}

#[tokio::test]
async fn preparation_failure_does_not_reuse_a_previous_driver() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let previous_attempt = room
        .with_lock(|lock| Ok(lock.read_state()?["agent_run"]["attempt_id"].clone()))
        .unwrap();
    invoke(&room, Request::Stop { notice: None }).await.unwrap();
    let stopped = room.with_lock(|lock| lock.read_state()).unwrap();
    invoke(&room, Request::Stop { notice: None }).await.unwrap();
    let repeated = room.with_lock(|lock| lock.read_state()).unwrap();
    assert_eq!(repeated["agent_run"], stopped["agent_run"]);
    assert_eq!(repeated["recovery"], stopped["recovery"]);
    room.with_lock(|lock| {
        assert_eq!(lock.read_state()?["agent_run"]["active"], false);
        Ok(())
    })
    .unwrap();
    fs::write(
        temp.path()
            .join("cluster/mods/dedicated_server_mods_setup.lua"),
        "ServerModSetup(",
    )
    .unwrap();
    assert!(invoke(&room, Request::Start {}).await.is_err());
    assert!(room.driver_states().is_empty());
    room.with_lock(|lock| {
        let record = dst_server::recovery::load(lock)?.last_failure.unwrap();
        assert_ne!(json!(record.attempt_id), previous_attempt);
        assert_eq!(lock.read_state()?["agent_run"]["active"], false);
        assert_eq!(
            record.trigger.stage,
            dst_server::recovery::FailureStage::Prepare
        );
        assert!(
            record
                .shards
                .values()
                .all(|shard| shard.process_id.is_none() && shard.exit.is_none())
        );
        assert!(matches!(
            record.classification,
            dst_server::recovery::Failure::Unknown
        ));
        assert!(record.complete);
        Ok(())
    })
    .unwrap();
}

#[tokio::test]
async fn save_uses_master_snapshot_and_rejects_ahead_secondaries_before_writing() {
    let (temp, room) = room();
    fs::write(temp.path().join("cluster/Master/initial-snapshot"), "7").unwrap();
    invoke(&room, Request::Start {}).await.unwrap();
    let saved = invoke(&room, Request::Save {}).await.unwrap();
    assert_eq!(saved, true);
    let request: Value =
        serde_json::from_slice(&fs::read(temp.path().join("cluster/save-request")).unwrap())
            .unwrap();
    assert_eq!(request["snapshot_id"], 7);
    room.shutdown().await.unwrap();

    let (temp, room) = self::room();
    fs::write(temp.path().join("cluster/Caves/initial-snapshot"), "7").unwrap();
    invoke(&room, Request::Start {}).await.unwrap();
    let error = invoke(&room, Request::Save {}).await.unwrap_err();
    assert_eq!(error.code, ErrorCode::Conflict);
    assert_eq!(error.details["shard"], "Caves");
    assert!(!temp.path().join("cluster/save-request").exists());
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn interrupted_stop_finishes_cleanup_and_retains_its_exit_reports() {
    for force in [false, true] {
        let (temp, room) = room();
        invoke(&room, Request::Start {}).await.unwrap();
        if force {
            fs::write(temp.path().join("cluster/hang-shutdown"), "").unwrap();
        }
        let copy = room.clone();
        let stopping =
            tokio::spawn(async move { invoke(&copy, Request::Stop { notice: None }).await });
        until(&room, || room.status().phase == Phase::Stopping).await;
        let copy = room.clone();
        let interrupt = tokio::spawn(async move {
            if force {
                copy.kill().await
            } else {
                copy.shutdown_priority().await
            }
        });
        let error = stopping.await.unwrap().unwrap_err();
        assert_eq!(error.code, ErrorCode::Unknown);
        assert!(
            room.driver_states().values().all(|state| !state.running
                && state.exit.as_ref().is_some_and(|exit| exit.output_drained))
        );
        let cleanup: StopResult = if force {
            assert_eq!(error.details["cleanup"]["status"], "failure");
            assert_eq!(error.details["cleanup"]["error"]["code"], "partial_failure");
            serde_json::from_value(error.details["cleanup"]["error"]["details"].clone()).unwrap()
        } else {
            assert_eq!(error.details["cleanup"]["status"], "success");
            serde_json::from_value(error.details["cleanup"]["value"].clone()).unwrap()
        };
        for shard in cleanup.shards {
            if force {
                let Outcome::Failure { error } = shard.result else {
                    panic!("interrupted Stop accepted forced termination");
                };
                assert_eq!(error.details["stopped"]["returncode"], -libc::SIGKILL);
                assert_eq!(error.details["stopped"]["forced"], true);
                assert_eq!(error.details["stopped"]["output_drained"], true);
            } else {
                assert!(
                    matches!(shard.result, Outcome::Success { value } if !value.forced && value.output_drained)
                );
            }
        }
        let stopped = interrupt.await.unwrap().unwrap();
        assert!(stopped.shards.iter().all(|shard| matches!(&shard.result, Outcome::Success { value } if value.forced == force && value.output_drained)));
        if force {
            assert!(stopped.shards.iter().all(|shard| matches!(&shard.result, Outcome::Success { value } if value.returncode == Some(-libc::SIGKILL))));
        }
        assert_eq!(room.status().phase, Phase::Stopped);
        assert_eq!(room.requires_container_restart(), force);
    }
}

#[tokio::test]
async fn stop_rejects_abnormal_exits_and_retains_every_shard_report() {
    for mode in ["exit", "signal", "protocol"] {
        let (temp, room) = room();
        invoke(&room, Request::Start {}).await.unwrap();
        let pids: Vec<_> = room
            .driver_states()
            .values()
            .map(|state| state.pid)
            .collect();
        fs::write(temp.path().join("cluster/Caves/shutdown-mode"), mode).unwrap();
        let error = invoke(&room, Request::Stop { notice: None })
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::PartialFailure, "{mode}");
        let stopped: StopResult = serde_json::from_value(error.details.clone()).unwrap();
        assert_eq!(stopped.shards.len(), 2);
        for shard in stopped.shards {
            if shard.shard == "Master" {
                assert!(
                    matches!(shard.result, Outcome::Success { value } if value.returncode == Some(0) && !value.forced && value.output_drained)
                );
                continue;
            }
            let Outcome::Failure { error } = shard.result else {
                panic!("Stop accepted {mode} exit");
            };
            assert_eq!(error.details["stopped"]["output_drained"], true);
            match mode {
                "exit" | "signal" => {
                    assert_eq!(error.code, ErrorCode::Transport);
                    assert_eq!(
                        error.details["stopped"]["returncode"],
                        if mode == "exit" { 7 } else { -libc::SIGABRT },
                    );
                    assert_eq!(error.details["stopped"]["forced"], false);
                }
                "protocol" => assert_eq!(error.code, ErrorCode::Protocol),
                _ => unreachable!(),
            }
        }
        assert_eq!(room.status().phase, Phase::Failed);
        assert_eq!(room.status().error, Some(error));
        assert!(room.requires_container_restart());
        assert!(
            room.driver_states().values().all(|state| !state.running
                && state.exit.as_ref().is_some_and(|exit| exit.output_drained))
        );
        for pid in pids {
            assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
        }
    }
}

#[tokio::test]
async fn repeated_announcements_complete_and_shutdown_cancels_future_notices() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let notices = temp.path().join("cluster/announcements");
    let announce = |count| Request::Announce {
        message: "notice".into(),
        count,
        interval: 0.05,
    };
    assert_eq!(invoke(&room, announce(3)).await.unwrap(), true);
    let delivered = fs::read_to_string(&notices).unwrap();
    assert_eq!(delivered.lines().count(), 3);
    for line in delivered.lines() {
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(line).unwrap()["count"],
            1
        );
    }
    let copy = room.clone();
    let pending = tokio::spawn(async move { invoke(&copy, announce(100)).await });
    until(&room, || {
        fs::read_to_string(&notices).unwrap().lines().count() > 3
    })
    .await;
    room.shutdown_priority().await.unwrap();
    assert_eq!(pending.await.unwrap().unwrap_err().code, ErrorCode::Unknown);
    let delivered = fs::read_to_string(&notices).unwrap();
    sleep(Duration::from_millis(100)).await;
    assert_eq!(fs::read_to_string(&notices).unwrap(), delivered);
    assert!(delivered.lines().count() < 103);
}

#[tokio::test]
async fn failed_stop_notice_still_saves_and_reaps_every_shard() {
    for response in [
        json!({"ok":false,"error":"lua_error"}),
        json!({"ok":true,"data":false}),
    ] {
        let (temp, room) = room();
        invoke(&room, Request::Start {}).await.unwrap();
        let root = temp.path().join("cluster");
        fs::write(root.join("announce-response.json"), response.to_string()).unwrap();
        let notice =
            serde_json::from_value(json!({"template":"stop {remaining}","delay":0.05})).unwrap();
        invoke(
            &room,
            Request::Stop {
                notice: Some(notice),
            },
        )
        .await
        .unwrap();
        assert!(!room.requires_container_restart());
        for name in ["Master", "Caves"] {
            assert_eq!(
                fs::read_to_string(root.join(name).join("native-lifecycle")).unwrap(),
                "start\nshutdown\nshutdown_save\n"
            );
        }
        assert_eq!(
            fs::read_to_string(root.join("announcements"))
                .unwrap()
                .lines()
                .count(),
            1
        );
    }
}

#[tokio::test]
async fn stop_countdown_renders_parameters_and_shutdown_cancels_later_notices() {
    for delay in [0.0, 60.0] {
        let (temp, room) = room();
        invoke(&room, Request::Start {}).await.unwrap();
        let notices = temp.path().join("cluster/announcements");
        let notice = Countdown {
            template: "{{room}} {when} {remaining} {minutes} {why}".into(),
            delay,
            interval: 30.0,
            parameters: std::collections::BTreeMap::from([("why".into(), json!("更新"))]),
        };
        let copy = room.clone();
        let stopping = tokio::spawn(async move {
            invoke(
                &copy,
                Request::Stop {
                    notice: Some(notice),
                },
            )
            .await
        });
        until(&room, || notices.exists()).await;
        if delay > 0.0 {
            room.shutdown_priority().await.unwrap();
            assert_eq!(
                stopping.await.unwrap().unwrap_err().code,
                ErrorCode::Unknown
            );
        } else {
            stopping.await.unwrap().unwrap();
        }
        let delivered = fs::read_to_string(notices).unwrap();
        let messages: Vec<serde_json::Value> = delivered
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0]["count"], 1);
        assert_eq!(
            messages[0]["message"],
            if delay == 0.0 {
                "{room} 即将 0 0 更新"
            } else {
                "{room} 将在约 1 分钟后 60 1 更新"
            }
        );
        assert!(
            room.driver_states().values().all(|state| !state.running
                && state.exit.as_ref().is_some_and(|exit| exit.output_drained))
        );
    }
}

#[tokio::test]
async fn migration_returns_native_acceptance_before_destination_entry() {
    let (temp, room) = room_with_shards(&[
        ("Master", 1, 19100),
        ("Caves", 2, 19200),
        ("Caves2", 3, 19300),
    ]);
    let root = temp.path().join("cluster");
    invoke(&room, Request::Start {}).await.unwrap();
    let migrate = |shard_id: &str| Request::Migrate {
        userid: "KU_ABC".into(),
        shard_id: shard_id.into(),
        portal_id: 1,
    };
    assert_eq!(
        invoke(&room, migrate("absent")).await.unwrap_err().code,
        ErrorCode::Invalid
    );
    assert!(!root.join("migration-requests").exists());
    fs::write(root.join("refuse-migration"), "").unwrap();
    let rejected = timeout(Duration::from_secs(1), invoke(&room, migrate("2")))
        .await
        .expect("native refusal must complete without waiting for migration")
        .unwrap();
    assert_eq!(rejected, false);
    assert!(room.status().operations.current.is_none());
    fs::remove_file(root.join("refuse-migration")).unwrap();
    for (id, destination) in [("2", "Caves"), ("3", "Caves2")] {
        fs::write(root.join("hold-migration"), "").unwrap();
        assert_eq!(
            timeout(Duration::from_secs(1), invoke(&room, migrate(id)))
                .await
                .expect("migration returns before arrival")
                .unwrap(),
            true
        );
        assert!(root.join("migration.json").exists());
        fs::remove_file(root.join("hold-migration")).unwrap();
        timeout(Duration::from_secs(3), async {
            loop {
                let located = invoke(
                    &room,
                    Request::GetPlayer {
                        userid: "KU_ABC".into(),
                    },
                )
                .await
                .unwrap();
                if located["state"] == "active" && located["shard"] == destination {
                    assert_eq!(
                        located["identity"]["session_id"],
                        format!("S_{destination}")
                    );
                    break;
                }
                sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert!(!root.join("migration.json").exists());
    }
    assert_eq!(
        fs::read_to_string(root.join("migration-requests")).unwrap(),
        "Master\nMaster\nCaves\n"
    );
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn stopped_permission_edits_preserve_native_records_without_starting_games() {
    let (temp, room) = room();
    let root = temp.path().join("cluster");
    let existing = b"KU_existing\xba\xba1790989763\xba\xbaRoom \xff\xba\n";
    fs::write(root.join("blocklist.txt"), existing).unwrap();
    assert_eq!(
        invoke(
            &room,
            Request::Ban {
                userid: "KU_new".into(),
                seconds: None
            }
        )
        .await
        .unwrap(),
        serde_json::Value::Null
    );
    assert_eq!(
        fs::read(root.join("blocklist.txt")).unwrap(),
        [existing.as_slice(), b"KU_new\n"].concat()
    );
    assert_eq!(
        invoke(&room, Request::Blocklist {}).await.unwrap(),
        json!(["KU_existing", "KU_new"])
    );
    for (userid, blocked) in [
        ("KU_existing", true),
        ("KU_new", true),
        ("KU_absent", false),
    ] {
        assert_eq!(
            invoke(
                &room,
                Request::IsBlocked {
                    userid: userid.into()
                }
            )
            .await
            .unwrap(),
            blocked
        );
    }
    for expected in [serde_json::Value::Null, serde_json::Value::Null] {
        assert_eq!(
            invoke(
                &room,
                Request::Unban {
                    userid: "KU_new".into()
                }
            )
            .await
            .unwrap(),
            expected
        );
    }
    assert_eq!(fs::read(root.join("blocklist.txt")).unwrap(), existing);
    assert_eq!(
        invoke(
            &room,
            Request::IsBlocked {
                userid: "KU_new".into()
            }
        )
        .await
        .unwrap(),
        false
    );
    let timed = invoke(
        &room,
        Request::Ban {
            userid: "KU_timed".into(),
            seconds: Some(60),
        },
    )
    .await
    .unwrap_err();
    assert_eq!(timed.code, ErrorCode::NotReady);
    assert_eq!(fs::read(root.join("blocklist.txt")).unwrap(), existing);
    for request in [
        Request::Whitelist {
            userid: "KU_guest".into(),
        },
        Request::Unwhitelist {
            userid: "KU_guest".into(),
        },
    ] {
        assert_eq!(
            invoke(&room, request).await.unwrap(),
            serde_json::Value::Null
        );
    }
    assert!(fs::read(root.join("whitelist.txt")).unwrap().is_empty());
    assert_eq!(room.status().phase, Phase::Stopped);
}

#[tokio::test]
async fn native_optionals_and_permissions_use_the_master_authority() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    assert!(
        Envelope::new(
            Target::Shard("Caves".into()),
            Request::Ban {
                userid: "KU_ABC".into(),
                seconds: None
            }
        )
        .is_err()
    );
    let banned = invoke(
        &room,
        Request::Ban {
            userid: "KU_ABC".into(),
            seconds: None,
        },
    )
    .await
    .unwrap();
    assert_eq!(banned, serde_json::Value::Null);
    assert_eq!(
        fs::read_to_string(temp.path().join("cluster/banned")).unwrap(),
        "Master:KU_ABC"
    );
    assert_eq!(
        invoke(&room, Request::Blocklist {}).await.unwrap(),
        json!(["KU_ABC"])
    );
    assert_eq!(
        invoke(
            &room,
            Request::Unban {
                userid: "KU_ABC".into()
            }
        )
        .await
        .unwrap(),
        serde_json::Value::Null
    );
    let changed = invoke(
        &room,
        Request::SetVitals {
            userid: "KU_ABC".into(),
            health: Some(0.5),
            hunger: None,
            sanity: None,
            temperature: None,
            moisture: None,
        },
    )
    .await
    .unwrap();
    assert_eq!(changed, true);
    let vitals: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(temp.path().join("cluster/vitals")).unwrap())
            .unwrap();
    assert_eq!(vitals["health"], 0.5);
    assert!(!vitals.as_object().unwrap().contains_key("hunger"));
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn kick_handles_loading_and_migrating_connections_and_reports_partial_failures() {
    for (scenario, state, shard) in [
        ("lobby", "loading", None),
        ("handshake", "loading", Some("Caves")),
        ("migrating", "departing", Some("Master")),
        ("leaving", "departing", Some("Master")),
        ("partial", "active", Some("Master")),
    ] {
        let (temp, room) = room();
        let root = temp.path().join("cluster");
        invoke(&room, Request::Start {}).await.unwrap();
        if scenario == "lobby" {
            // Both shards list the lobby client before either has a character.
            fs::write(root.join("player-loading"), "").unwrap();
            fs::write(root.join("player-handshake"), "").unwrap();
        } else if scenario == "handshake" {
            fs::write(root.join("player-location"), "Caves").unwrap();
            fs::write(root.join("player-handshake"), "").unwrap();
        } else if scenario == "migrating" {
            fs::write(root.join("hold-migration"), "").unwrap();
            invoke(
                &room,
                Request::Migrate {
                    userid: "KU_ABC".into(),
                    shard_id: "2".into(),
                    portal_id: 1,
                },
            )
            .await
            .unwrap();
        } else if scenario == "leaving" {
            fs::write(root.join("player-departing"), "").unwrap();
        } else {
            fs::write(root.join("invalid-kick-Caves"), "").unwrap();
        }
        let player = invoke(
            &room,
            Request::GetPlayer {
                userid: "KU_ABC".into(),
            },
        )
        .await
        .unwrap();
        assert_eq!(player["state"], state);
        assert_eq!(player["shard"].as_str(), shard);
        assert_eq!(player["identity"].is_null(), shard.is_none());
        assert_eq!(player["player"]["userid"], "KU_ABC");
        if state != "active" {
            let error = invoke(
                &room,
                Request::Inventory {
                    userid: "KU_ABC".into(),
                },
            )
            .await
            .unwrap_err();
            assert_eq!(error.code, ErrorCode::NotReady);
            assert_eq!(error.details["state"], state);
        }
        let result = invoke(
            &room,
            Request::Kick {
                userid: "KU_ABC".into(),
            },
        )
        .await;
        if scenario == "partial" {
            assert_eq!(result.unwrap_err().code, ErrorCode::PartialFailure);
        } else {
            assert_eq!(result.unwrap(), true);
            assert_eq!(
                invoke(
                    &room,
                    Request::Kick {
                        userid: "KU_ABC".into()
                    }
                )
                .await
                .unwrap(),
                false
            );
        }
        let requests = fs::read_to_string(root.join("kick-requests")).unwrap();
        assert!(requests.contains("Master") && requests.contains("Caves"));
        room.shutdown().await.unwrap();
    }
}

#[tokio::test]
async fn vote_control_routes_to_master_and_preserves_native_rejections() {
    let (temp, room) = room();
    let root = temp.path().join("cluster");
    invoke(&room, Request::Start {}).await.unwrap();
    let response = json!({"enabled": true, "current": null});
    fs::write(root.join("vote-response.json"), response.to_string()).unwrap();
    assert_eq!(invoke(&room, Request::GetVote {}).await.unwrap(), response);
    assert!(root.join("Master.vote-call").exists() && !root.join("Caves.vote-call").exists());
    fs::write(
        root.join("vote-response.json"),
        r#"{"error":{"code":"busy","message":"active vote"}}"#,
    )
    .unwrap();
    let start = || Request::StartVote {
        command: "kick".into(),
        starter_userid: "KU_A".into(),
        target_userid: Some("KU_B".into()),
    };
    let error = invoke(&room, start()).await.unwrap_err();
    assert_eq!(error.code, ErrorCode::Busy);
    let forwarded: serde_json::Value =
        serde_json::from_slice(&fs::read(root.join("Master.vote-call")).unwrap()).unwrap();
    assert_eq!(
        forwarded["arguments"],
        json!({"command":"kick", "starter_userid":"KU_A", "target_userid":"KU_B"})
    );
    fs::write(root.join("player-location"), "Caves").unwrap();
    fs::write(
        root.join("vote-access.json"),
        r#"{"error":{"code":"invalid","message":"starter cannot access this command"}}"#,
    )
    .unwrap();
    assert_eq!(
        invoke(&room, start()).await.unwrap_err().code,
        ErrorCode::Invalid
    );
    let access: Value =
        serde_json::from_slice(&fs::read(root.join("Caves.vote-access")).unwrap()).unwrap();
    assert_eq!(access["userid"], "KU_A");
    assert_eq!(access["_expected_session_id"], "S_Caves");
    assert_eq!(access["_expected_generation"], 1);
    assert!(access["_expected_guid"].as_u64().is_some());
    fs::write(root.join("vote-access.json"), "42").unwrap();
    let vote = json!({"enabled":true,"current":{"command":"kick","starter_userid":"KU_A","target_userid":"KU_B"}});
    fs::write(root.join("vote-response.json"), vote.to_string()).unwrap();
    assert_eq!(invoke(&room, start()).await.unwrap(), vote);
    let forwarded: Value =
        serde_json::from_slice(&fs::read(root.join("Master.vote-call")).unwrap()).unwrap();
    assert_eq!(forwarded["arguments"]["_checked_command_hash"], 42);
    assert!(!root.join("Caves.vote-call").exists());
    fs::write(root.join("vote-response.json"), "true").unwrap();
    assert_eq!(invoke(&room, Request::CancelVote {}).await.unwrap(), true);
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn configuration_field_edits_validate_all_changes_before_writing() {
    let (temp, room) = room();
    let before = invoke(&room, Request::ReadConfiguration {}).await.unwrap();
    let invalid = invoke(
        &room,
        Request::EditConfiguration {
            changes: vec![
                (
                    "/cluster/settings/cluster_name".into(),
                    json!("must not be written"),
                ),
                ("/policy/timezone".into(), json!("invalid")),
            ],
            unset: vec![],
        },
    )
    .await;
    assert!(invalid.is_err());
    assert_eq!(
        invoke(&room, Request::ReadConfiguration {}).await.unwrap(),
        before
    );
    let permission = temp.path().join("cluster/blocklist.txt");
    fs::write(&permission, b"KU_banned\xba\xff\n").unwrap();
    invoke(
        &room,
        Request::EditConfiguration {
            changes: vec![
                ("/cluster/settings/cluster_name".into(), json!("renamed")),
                ("/policy/mod_auto_update".into(), json!(false)),
            ],
            unset: vec![],
        },
    )
    .await
    .unwrap();
    assert_eq!(fs::read(&permission).unwrap(), b"KU_banned\xba\xff\n");
    invoke(&room, Request::Start {}).await.unwrap();
    invoke(
        &room,
        Request::EditConfiguration {
            changes: vec![("/policy/timezone".into(), json!("UTC"))],
            unset: vec![],
        },
    )
    .await
    .unwrap();
    assert!(
        invoke(
            &room,
            Request::EditConfiguration {
                changes: vec![("/cluster/settings/max_players".into(), json!(12))],
                unset: vec![],
            }
        )
        .await
        .is_err()
    );
    let saved = invoke(&room, Request::ReadConfiguration {}).await.unwrap();
    assert_eq!(saved["cluster"]["settings"]["cluster_name"], "renamed");
    assert_eq!(saved["policy"]["timezone"], "UTC");
    assert_eq!(saved["policy"]["mod_auto_update"], false);
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn configuration_cannot_change_published_query_ports() {
    let (temp, room) = room();
    room.set_external_ports([("Master".into(), 29100), ("Caves".into(), 29200)].into())
        .unwrap();
    let path = temp.path().join("cluster/Master/server.ini");
    let before = fs::read(&path).unwrap();
    let mut configuration = invoke(&room, Request::ReadConfiguration {}).await.unwrap();
    configuration["cluster"]["shards"]["Master"]["settings"]["master_server_port"] = json!(19102);
    let error = invoke(
        &room,
        Request::Configure {
            configuration: configuration["cluster"].clone(),
            replace_permissions: false,
        },
    )
    .await
    .unwrap_err();
    assert!(error.message.contains("published"), "{error}");
    assert_eq!(fs::read(path).unwrap(), before);
}

#[tokio::test]
async fn admin_changes_preserve_native_lists_and_reload_every_shard() {
    let (temp, room) = room();
    let root = temp.path().join("cluster");
    let banned = b"KU_banned\xba\xba1790989763\xba\xbaRoom \xff\xba\n";
    fs::write(root.join("blocklist.txt"), banned).unwrap();
    fs::write(root.join("whitelist.txt"), b"KU_guest\n").unwrap();
    let configuration = invoke(&room, Request::ReadConfiguration {}).await.unwrap();
    assert_eq!(configuration["cluster"]["blocklist"], "KU_banned\n");
    assert_eq!(
        invoke(
            &room,
            Request::SetAdmin {
                userid: "KU_first".into(),
                enabled: true
            }
        )
        .await
        .unwrap(),
        serde_json::Value::Null
    );
    assert!(!root.join("Master.permissions").exists());
    invoke(&room, Request::Start {}).await.unwrap();
    fs::create_dir_all(root.join("mods")).unwrap();
    fs::write(
        root.join("mods/dedicated_server_mods_setup.lua"),
        "local id = '123'; ServerModSetup(id)",
    )
    .unwrap();
    assert_eq!(
        invoke(
            &room,
            Request::SetAdmin {
                userid: "KU_second".into(),
                enabled: true
            }
        )
        .await
        .unwrap(),
        serde_json::Value::Null
    );
    for shard in ["Master", "Caves"] {
        assert_eq!(
            fs::read(root.join(format!("{shard}.permissions"))).unwrap(),
            b"KU_first\nKU_second\n"
        );
    }
    assert_eq!(
        invoke(&room, Request::ReadPermissions {}).await.unwrap(),
        json!({"adminlist":["KU_first","KU_second"],"blocklist":["KU_banned"],"whitelist":["KU_guest"]})
    );
    assert_eq!(
        invoke(
            &room,
            Request::IsAdmin {
                userid: "KU_second".into()
            }
        )
        .await
        .unwrap(),
        true
    );
    fs::write(root.join("refuse-permissions-Caves"), "").unwrap();
    let failed = invoke(
        &room,
        Request::SetAdmin {
            userid: "KU_second".into(),
            enabled: false,
        },
    )
    .await
    .unwrap_err();
    assert_eq!(failed.code, ErrorCode::PartialFailure);
    assert_eq!(failed.details["adminlist"], json!(["KU_first"]));
    assert_eq!(
        invoke(
            &room,
            Request::IsAdmin {
                userid: "KU_second".into()
            }
        )
        .await
        .unwrap(),
        false
    );
    assert_eq!(fs::read(root.join("blocklist.txt")).unwrap(), banned);
    assert_eq!(fs::read(root.join("whitelist.txt")).unwrap(), b"KU_guest\n");
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn room_owns_cancelled_mutation_and_reaps_all_shards() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    assert_eq!(room.status().phase, Phase::Running);
    assert!(
        room.status()
            .shards
            .iter()
            .all(|shard| shard.readiness.ready())
    );
    let identities: std::collections::BTreeSet<_> = room
        .status()
        .shards
        .iter()
        .map(|shard| shard.identity.as_ref().unwrap().process_id.clone())
        .collect();
    assert_eq!(
        identities.len(),
        2,
        "shared launch options must retain distinct process identities"
    );
    for id in &identities {
        assert_eq!(id.parse::<ulid::Ulid>().unwrap().to_string(), *id);
    }
    let pids: Vec<_> = room
        .driver_states()
        .values()
        .map(|state| state.pid)
        .collect();
    assert!(RoomLock::try_acquire(temp.path().join("cluster")).is_err());
    let mut slow = Envelope::new(
        Target::Shard("Master".into()),
        Request::Execute {
            source: "slow".into(),
        },
    )
    .unwrap();
    slow.timeout = Some(0.03);
    assert_eq!(
        room.invoke(slow).await.unwrap_err().code,
        ErrorCode::Timeout
    );
    assert_eq!(
        invoke(&room, Request::Save {}).await.unwrap_err().code,
        ErrorCode::Busy
    );
    assert_eq!(
        invoke(&room, Request::Status {}).await.unwrap()["phase"],
        "running"
    );
    until(&room, || room.status().operations.current.is_none()).await;
    assert_eq!(
        fs::read_to_string(temp.path().join("cluster/executions")).unwrap(),
        "1"
    );
    let completed = room.status().operations.last.unwrap();
    assert!(completed.completed_at_ns.is_some());
    assert_eq!(completed.error, None);
    let saved = invoke(&room, Request::Save {}).await.unwrap();
    assert_eq!(saved, true);
    let moved = invoke(
        &room,
        Request::Teleport {
            userid: "KU_ABC".into(),
            x: 1.0,
            y: 0.0,
            z: 2.0,
        },
    )
    .await
    .unwrap();
    assert_eq!(moved, json!(true));
    assert_eq!(
        invoke(
            &room,
            Request::Kick {
                userid: "KU_ABC".into()
            }
        )
        .await
        .unwrap(),
        true
    );
    assert_eq!(
        invoke(
            &room,
            Request::GetPlayer {
                userid: "KU_ABC".into()
            }
        )
        .await
        .unwrap()["state"],
        "disconnected"
    );
    let stopped: StopResult =
        serde_json::from_value(invoke(&room, Request::Stop { notice: None }).await.unwrap())
            .unwrap();
    assert!(stopped.shards.iter().all(|shard|matches!(&shard.result,Outcome::Success{value} if !value.forced && value.output_drained)));
    for pid in pids {
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
    }
    assert_eq!(room.status().phase, Phase::Stopped);
    assert!(!room.requires_container_restart());
    drop(room);
    timeout(Duration::from_secs(2), async {
        loop {
            if RoomLock::try_acquire(temp.path().join("cluster")).is_ok() {
                break;
            }
            sleep(Duration::from_millis(10)).await
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn save_returns_on_acceptance_and_unexpected_shard_exit_stops_the_room() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    fs::write(temp.path().join("cluster/hold-save"), "").unwrap();
    assert_eq!(
        timeout(Duration::from_secs(1), invoke(&room, Request::Save {}))
            .await
            .expect("save returns before persistence")
            .unwrap(),
        true
    );
    for name in ["Master", "Caves"] {
        let runtime = room
            .invoke(Envelope::new(Target::Shard(name.into()), Request::Runtime {}).unwrap())
            .await
            .unwrap();
        assert_eq!(runtime["snapshot"], 5);
    }
    let crash = Envelope::new(
        Target::Shard("Master".into()),
        Request::Execute {
            source: "crash".into(),
        },
    )
    .unwrap();
    assert!(room.invoke(crash).await.is_err());
    until(&room, || {
        room.status().phase == Phase::Failed
            && room.driver_states().values().all(|state| {
                !state.running && state.exit.as_ref().is_some_and(|exit| exit.output_drained)
            })
    })
    .await;
    assert!(room.requires_container_restart());
    let states: Vec<_> = room.driver_states().into_values().collect();
    assert!(states.iter().all(|state| state.failure.is_none()));
    assert!(matches!(
        room.with_lock(|lock| Ok(dst_server::recovery::load(lock)?
            .last_failure
            .unwrap()
            .classification))
            .unwrap(),
        dst_server::recovery::Failure::Retryable
    ));
    assert_eq!(
        invoke(&room, Request::Start {}).await.unwrap_err().code,
        ErrorCode::NotReady
    );
}

#[tokio::test]
async fn stopped_guard_excludes_start_and_cancelled_kill_keeps_cleanup_owned() {
    let (_temp, room) = room();
    let guard = room.while_stopped().await.unwrap();
    assert_eq!(
        invoke(&room, Request::Start {}).await.unwrap_err().code,
        ErrorCode::Busy
    );
    drop(guard);
    invoke(&room, Request::Start {}).await.unwrap();
    let slow = Envelope::new(
        Target::Shard("Master".into()),
        Request::Execute {
            source: "slow".into(),
        },
    )
    .unwrap();
    let copy = room.clone();
    let request = tokio::spawn(async move { copy.invoke(slow).await });
    until(&room, || room.status().operations.current.is_some()).await;
    let mut kill = Envelope::new(Target::Room, Request::Kill {}).unwrap();
    kill.timeout = Some(0.000_001);
    let _ = room.invoke(kill).await;
    let _ = request.await;
    until(&room, || {
        room.status().operations.current.is_none()
            && room.driver_states().values().all(|state| {
                !state.running && state.exit.as_ref().is_some_and(|exit| exit.output_drained)
            })
    })
    .await;
    assert!(room.requires_container_restart());
}

#[tokio::test]
async fn rollback_uses_clock_metadata_only_to_match_days() {
    let (temp, room) = room();
    let meta = |shard: &str, id: u64| {
        temp.path().join(format!(
            "cluster/{shard}/save/session/S_{shard}/{id:010}.meta"
        ))
    };
    fs::write(meta("Master", 1), "broken").unwrap();
    fs::copy(meta("Master", 3), meta("Master", 2)).unwrap();
    fs::write(meta("Caves", 2), "broken").unwrap();
    fs::remove_file(meta("Master", 4)).unwrap();
    fs::write(meta("Caves", 4), "broken").unwrap();
    invoke(&room, Request::Start {}).await.unwrap();

    let catalog = invoke(
        &room,
        Request::Snapshots {
            limit: 4,
            before: None,
        },
    )
    .await
    .unwrap();
    assert_eq!(catalog["snapshots"][0]["snapshot_id"], 4);
    assert_eq!(catalog["snapshots"][0]["metadata"], Value::Null);
    assert_eq!(catalog["snapshots"][1]["metadata"]["day"], 3);
    assert_eq!(catalog["snapshots"][3]["metadata"], Value::Null);
    let reset = invoke(&room, Request::Reset {}).await.unwrap();
    assert_eq!(reset["snapshot_id"], 4);
    assert_eq!(reset["metadata"], Value::Null);
    invoke(
        &room,
        Request::RollbackToSnapshot {
            session_id: "S_Master".into(),
            snapshot_id: 4,
        },
    )
    .await
    .unwrap();
    let selected = invoke(&room, Request::Rollback { count: 1 }).await.unwrap();
    assert_eq!(selected["snapshot_id"], 3);
    assert_eq!(selected["metadata"]["day"], 3);

    let day = invoke(&room, Request::RollbackToDay { day: 3 })
        .await
        .unwrap();
    assert_eq!(day["snapshot_id"], 3);
    fs::copy(meta("Caves", 3), meta("Caves", 2)).unwrap();
    let day = invoke(&room, Request::RollbackToDay { day: 3 })
        .await
        .unwrap();
    assert_eq!(day["snapshot_id"], 2);
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn reload_confirms_every_generation_session_and_selected_snapshot() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let before = room.driver_states();
    let cave_world = temp
        .path()
        .join("cluster/Caves/save/session/S_Caves/0000000004");
    fs::write(&cave_world, []).unwrap();
    fs::remove_file(cave_world.with_extension("meta")).unwrap();
    let error = invoke(
        &room,
        Request::RollbackToSnapshot {
            session_id: "S_Master".into(),
            snapshot_id: 4,
        },
    )
    .await
    .unwrap_err();
    assert_eq!(error.code, ErrorCode::NotFound);
    fs::write(
        temp.path()
            .join("cluster/Master/save/session/S_Master/0000000004"),
        [],
    )
    .unwrap();
    assert_eq!(
        invoke(&room, Request::Reset {}).await.unwrap_err().code,
        ErrorCode::NotFound
    );
    assert!(!temp.path().join("cluster/reload-request").exists());
    let selected = invoke(&room, Request::RollbackToDay { day: 3 })
        .await
        .unwrap();
    assert_eq!(selected["snapshot_id"], 3);
    for (name, state) in room.driver_states() {
        assert_eq!(state.session_id, before[&name].session_id);
        assert_eq!(state.generation, Some(2));
        assert_eq!(state.runtime.unwrap()["snapshot"], 4);
    }
    invoke(
        &room,
        Request::Regenerate {
            expected_session_id: before["Master"].session_id.clone(),
            require_empty: Some(true),
        },
    )
    .await
    .unwrap();
    for (name, state) in room.driver_states() {
        assert_ne!(state.session_id, before[&name].session_id);
        assert_eq!(state.generation, Some(3));
    }
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn health_checks_each_native_self_id_and_waits_for_its_ready_peers() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let caves = temp.path().join("cluster/Caves");
    let runtime = caves.join("reload-during-runtime");
    fs::write(&runtime, "").unwrap();
    let response = caves.join("connected-shards.json");
    for (peers, code) in [
        (
            json!([{"id":"2","name":"Caves","ready":true}]),
            ErrorCode::Protocol,
        ),
        (
            json!([{"id":"1","name":"Caves","is_current":true,"ready":true}]),
            ErrorCode::Conflict,
        ),
    ] {
        fs::write(&response, serde_json::to_vec(&peers).unwrap()).unwrap();
        assert_eq!(room.refresh_health().await.unwrap_err().code, code);
    }
    let peers = json!([
        {"id":"1","name":"Master","is_current":false,"ready":false},
        {"id":"2","name":"Caves","is_current":true,"ready":true},
    ]);
    fs::write(&response, serde_json::to_vec(&peers).unwrap()).unwrap();
    let check = {
        let room = room.clone();
        tokio::spawn(async move { room.refresh_health().await })
    };
    until(&room, || {
        room.status()
            .shards
            .iter()
            .any(|shard| shard.name == "Caves" && shard.readiness.missing_shards == ["Master"])
    })
    .await;
    fs::remove_file(response).unwrap();
    check.await.unwrap().unwrap();
    assert!(
        runtime.exists(),
        "health requested runtime again in the same generation"
    );
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn required_health_failure_is_an_error_and_keeps_status_available() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    room.refresh_health().await.unwrap();
    fs::write(temp.path().join("cluster/unhealthy"), "").unwrap();
    assert_eq!(
        room.refresh_health().await.unwrap_err().code,
        ErrorCode::NotReady
    );
    let status = invoke(&room, Request::Status {}).await.unwrap();
    assert_eq!(status["phase"], "running");
    until(&room, || {
        room.status()
            .shards
            .iter()
            .any(|shard| shard.name == "Caves" && !shard.readiness.control_ready)
    })
    .await;
    room.shutdown().await.unwrap();
    let closed = room.close_telemetry().await.unwrap();
    assert_eq!(closed["output_drained"], true);
    assert_eq!(closed["relays_drained"], true);
}

#[tokio::test]
async fn dropping_from_a_foreign_thread_holds_the_room_lock_until_native_reap() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let pids: Vec<_> = room
        .driver_states()
        .values()
        .map(|state| state.pid)
        .collect();
    let directory = temp.path().join("cluster");
    let dropped_directory = directory.clone();
    std::thread::spawn(move || {
        drop(room);
        assert!(RoomLock::try_acquire(dropped_directory).is_err());
    })
    .join()
    .unwrap();
    timeout(Duration::from_secs(5), async {
        loop {
            if RoomLock::try_acquire(&directory).is_ok() {
                break;
            }
            sleep(Duration::from_millis(10)).await
        }
    })
    .await
    .unwrap();
    for pid in pids {
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
    }
}

#[tokio::test]
async fn priority_shutdown_interrupts_pending_work_and_reaps_cleanly() {
    let (_temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let request = Envelope::new(
        Target::Shard("Master".into()),
        Request::Execute {
            source: "slow".into(),
        },
    )
    .unwrap();
    let operation_room = room.clone();
    let operation = tokio::spawn(async move { operation_room.invoke(request).await });
    until(&room, || room.status().operations.current.is_some()).await;
    let stopped = room.shutdown_priority().await.unwrap();
    assert!(operation.await.unwrap().is_err());
    assert!(stopped.shards.iter().all(|shard|matches!(&shard.result,Outcome::Success{value} if !value.forced && value.output_drained)));
    assert!(!room.requires_container_restart());
    assert_eq!(room.status().phase, Phase::Stopped);
}

#[tokio::test]
async fn archive_export_does_not_stop_or_mutate_a_running_room() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let before = room.status();
    let prepared = dst_server::archive::prepare(
        &dst_server::files::RoomReader::open(temp.path().join("cluster")).unwrap(),
        1,
        &dst_server::archive::ExportOptions {
            room_id: Some("fixture".into()),
            ..Default::default()
        },
    )
    .unwrap();
    let mut archive = prepared
        .compress(dst_server::archive::DEFAULT_COMPRESSION_LEVEL)
        .await
        .unwrap();
    use std::io::Read;
    let mut signature = [0; 6];
    archive.file_mut().read_exact(&mut signature).unwrap();
    assert_eq!(&signature, b"7z\xbc\xaf\x27\x1c");
    let after = room.status();
    assert_eq!(after.phase, Phase::Running);
    assert_eq!(
        after
            .shards
            .iter()
            .map(|shard| shard.pid)
            .collect::<Vec<_>>(),
        before
            .shards
            .iter()
            .map(|shard| shard.pid)
            .collect::<Vec<_>>()
    );
    assert_eq!(
        serde_json::to_value(&after.operations).unwrap(),
        serde_json::to_value(&before.operations).unwrap()
    );
    room.shutdown().await.unwrap();
}
