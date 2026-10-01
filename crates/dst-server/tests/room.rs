//! Real subprocess fixtures verify room ownership without launching or editing a live game.
use dst_server::{
    driver::DriverOptions,
    files::RoomLock,
    model::{Envelope, ErrorCode, Outcome, Phase, Request, StopResult, Target},
    room::Room,
    rpc::EventHub,
};
use serde_json::json;
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
    let room = Room::open(&root, &game, DriverOptions::default(), EventHub::default()).unwrap();
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
async fn save_uses_master_snapshot_and_rejects_ahead_secondaries_before_writing() {
    let (temp, room) = room();
    fs::write(temp.path().join("cluster/Master/initial-snapshot"), "7").unwrap();
    invoke(&room, Request::Start {}).await.unwrap();
    let saved = invoke(&room, Request::Save {}).await.unwrap();
    assert!(
        saved["shards"]
            .as_array()
            .unwrap()
            .iter()
            .all(|shard| shard["result"]["value"]["snapshot_id"] == 7)
    );
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
async fn interrupted_stop_finishes_cleanup_and_retains_its_exit_and_save_reports() {
    for force in [false, true] {
        let (temp, room) = room();
        invoke(&room, Request::Start {}).await.unwrap();
        if force {
            invoke(
                &room,
                Request::ExecuteAll {
                    source: "old_shutdown_save".into(),
                },
            )
            .await
            .unwrap();
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
            room.driver_states()
                .values()
                .all(|state| !state.running && state.output_drained)
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
                assert!(error.details["stopped"]["saved_snapshot"].is_null());
            } else {
                assert!(
                    matches!(shard.result, Outcome::Success { value } if !value.forced && value.output_drained && value.saved_snapshot.is_some())
                );
            }
        }
        let stopped = interrupt.await.unwrap().unwrap();
        assert!(stopped.shards.iter().all(|shard| matches!(&shard.result, Outcome::Success { value } if value.forced == force && value.output_drained && value.saved_snapshot.is_some() != force)));
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
                    matches!(shard.result, Outcome::Success { value } if value.returncode == Some(0) && !value.forced && value.output_drained && value.saved_snapshot.is_some())
                );
                continue;
            }
            let Outcome::Failure { error } = shard.result else {
                panic!("Stop accepted {mode} exit");
            };
            assert_eq!(error.details["stopped"]["output_drained"], true);
            assert!(error.details["stopped"]["saved_snapshot"].is_object());
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
            room.driver_states()
                .values()
                .all(|state| !state.running && state.output_drained)
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
async fn migration_refusal_finishes_and_moves_wait_for_destination_entry() {
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
        let located = invoke(&room, migrate(id)).await.unwrap();
        assert_eq!(located["state"], "active");
        assert_eq!(located["shard"], destination);
        assert_eq!(
            located["identity"]["session_id"],
            format!("S_{destination}")
        );
        assert!(!root.join("migration.json").exists());
    }
    assert_eq!(
        fs::read_to_string(root.join("migration-requests")).unwrap(),
        "Master\nMaster\nCaves\n"
    );
    room.shutdown().await.unwrap();
}

#[tokio::test]
async fn native_optionals_and_permissions_use_the_master_authority() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let banned = room
        .invoke(
            Envelope::new(
                Target::Shard("Caves".into()),
                Request::Ban {
                    userid: "KU_ABC".into(),
                    seconds: None,
                },
            )
            .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(banned, true);
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
        true
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
                remove: false
            }
        )
        .await
        .unwrap(),
        json!(["KU_first"])
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
                remove: false
            }
        )
        .await
        .unwrap(),
        json!(["KU_first", "KU_second"])
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
            remove: true,
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
async fn room_owns_cancelled_mutation_and_reaps_all_shards_with_save_proof() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    assert_eq!(room.status().phase, Phase::Running);
    assert!(
        room.status()
            .shards
            .iter()
            .all(|shard| shard.readiness.ready())
    );
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
    assert!(
        matches!(room.status().operations.last.unwrap().result,Some(Outcome::Success{value}) if value=="captured")
    );
    let saved = invoke(&room, Request::Save {}).await.unwrap();
    assert!(
        saved["shards"]
            .as_array()
            .unwrap()
            .iter()
            .all(|shard| shard["result"]["value"]["snapshot_id"] == 5)
    );
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
    assert!(stopped.shards.iter().all(|shard|matches!(&shard.result,Outcome::Success{value} if !value.forced && value.output_drained && value.saved_snapshot.is_some())));
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
async fn one_failed_save_is_explicit_and_unexpected_shard_exit_stops_the_room() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    fs::write(temp.path().join("cluster/fail-save"), "").unwrap();
    let error = invoke(&room, Request::Save {}).await.unwrap_err();
    assert_eq!(error.code, ErrorCode::PartialFailure);
    assert!(
        error.details["shards"]
            .as_array()
            .unwrap()
            .iter()
            .any(|shard| shard["shard"] == "Caves" && shard["result"]["status"] == "failure")
    );
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
            && room
                .driver_states()
                .values()
                .all(|state| !state.running && state.output_drained)
    })
    .await;
    assert!(room.requires_container_restart());
    let states: Vec<_> = room.driver_states().into_values().collect();
    assert!(states.iter().all(|state| state.failure.is_none()));
    assert!(matches!(
        dst_server::preloader::classify(&states),
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
            && room
                .driver_states()
                .values()
                .all(|state| !state.running && state.output_drained)
    })
    .await;
    assert!(room.requires_container_restart());
}

#[tokio::test]
async fn reload_confirms_every_generation_session_and_selected_snapshot() {
    let (_temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    let before = room.driver_states();
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
async fn priority_shutdown_interrupts_pending_work_and_finishes_a_clean_save() {
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
    assert!(stopped.shards.iter().all(|shard|matches!(&shard.result,Outcome::Success{value} if !value.forced && value.output_drained && value.saved_snapshot.is_some())));
    assert!(!room.requires_container_restart());
    assert_eq!(room.status().phase, Phase::Stopped);
}

#[tokio::test]
async fn archive_publication_survives_kill_and_release_bypasses_other_operations() {
    let (temp, room) = room();
    invoke(&room, Request::Start {}).await.unwrap();
    room.shutdown().await.unwrap();
    let mut status = room.watch();
    let exporting = room.clone();
    let export = tokio::spawn(async move {
        invoke(
            &exporting,
            Request::ExportArchive {
                options: json!({"encode_user_path":false}),
                compression_level: 3,
            },
        )
        .await
    });
    timeout(Duration::from_secs(5), async {
        loop {
            if status
                .borrow_and_update()
                .operations
                .current
                .as_ref()
                .is_some_and(|operation| operation.method == "export_archive")
            {
                break;
            }
            status.changed().await.unwrap();
        }
    })
    .await
    .unwrap();
    room.kill().await.unwrap();
    let receipt: dst_server::archive::Artifact =
        serde_json::from_value(export.await.unwrap().unwrap()).unwrap();
    assert!(receipt.size > 0);
    let mut archive =
        dst_server::archive::read_artifact(&temp.path().join("cluster"), &receipt).unwrap();
    use std::io::Read;
    let mut signature = [0; 6];
    archive.file_mut().read_exact(&mut signature).unwrap();
    assert_eq!(&signature, b"7z\xbc\xaf\x27\x1c");
    let _guard = room.while_stopped().await.unwrap();
    assert_eq!(
        invoke(
            &room,
            Request::ReleaseArchive {
                artifact_id: receipt.artifact_id
            }
        )
        .await
        .unwrap(),
        true
    );
}
