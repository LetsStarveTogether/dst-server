//! One in-flight stop intent and the service's shared shutdown deadlines.

use super::*;
use crate::driver::StopDeadlines;

#[derive(Clone, Copy)]
pub(crate) struct ShutdownDeadlines {
    pub handoff: Instant,
    pub notice: Instant,
    pub native: Instant,
    pub terminate: Instant,
}

pub(crate) struct StopReservation {
    resources: Option<(OwnedSemaphorePermit, OwnedSemaphorePermit)>,
    room: Arc<Room>,
    id: String,
    pub(super) interrupted: watch::Receiver<u64>,
    pub(super) revision: u64,
}

impl Drop for StopReservation {
    fn drop(&mut self) {
        if self.resources.is_some() {
            self.room.clear_stop_intent(&self.id);
        }
    }
}

impl StopReservation {
    pub(crate) async fn stop(
        self,
        notice: Option<Countdown>,
        parent: &OperationContext,
    ) -> Result<Value> {
        let room = self.room.clone();
        room.invoke_inner(
            Envelope::new(Target::Room, Request::Stop { notice })?,
            true,
            Some(parent),
            Some(self),
        )
        .await
        .map_err(|error| {
            if error.is_shutdown() {
                Error::shutdown(ErrorCode::Unknown)
            } else {
                error
            }
        })
    }

    pub(super) fn handoff(
        &mut self,
        child: &OperationContext,
    ) -> Result<(OwnedSemaphorePermit, OwnedSemaphorePermit)> {
        let _drivers = self.room.drivers.lock().unwrap();
        self.room.ensure_open()?;
        if child.parent_id.as_deref() != Some(self.id.as_str())
            || *self.interrupted.borrow() != self.revision
        {
            return Err(Error::new(
                ErrorCode::Unknown,
                "reserved stop interrupted before admission",
            ));
        }
        if !self.room.stop_intent.send_if_modified(|intent| {
            if let Some(intent) = intent.as_mut().filter(|intent| intent.id == self.id) {
                intent.id.clone_from(&child.id);
                true
            } else {
                false
            }
        }) {
            return Err(Error::new(
                ErrorCode::Unknown,
                "reserved stop no longer owns its intent",
            ));
        }
        self.resources.take().ok_or_else(|| {
            Error::new(
                ErrorCode::Internal,
                "stop reservation was already handed off",
            )
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};

    fn fixture() -> (tempfile::TempDir, Arc<Room>) {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("cluster");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("cluster.ini"), "[SHARD]\nshard_enabled=true\nmaster_port=19000\nmaster_ip=127.0.0.1\ncluster_key=fixture\n").unwrap();
        for (name, id, port) in [("Master", 1, 19100), ("Caves", 2, 19200)] {
            fs::create_dir(root.join(name)).unwrap();
            fs::write(root.join(name).join("server.ini"), format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n", id == 1, port + 1)).unwrap();
        }
        let game = temporary.path().join("game.py");
        fs::write(&game, include_str!("../../tests/fixtures/game.py")).unwrap();
        fs::set_permissions(&game, fs::Permissions::from_mode(0o700)).unwrap();
        let room = Room::open(
            root,
            game,
            DriverOptions::default(),
            EventHub::default(),
            Default::default(),
        )
        .unwrap();
        (temporary, room)
    }

    async fn wait_for(condition: impl Fn() -> bool) {
        timeout(Duration::from_secs(4), async {
            while !condition() {
                sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
    }

    fn write_marker(path: &Path, contents: &str) {
        let temporary = path.with_extension("new");
        fs::write(&temporary, contents).unwrap();
        fs::rename(temporary, path).unwrap();
    }

    async fn start(room: &Arc<Room>) {
        room.invoke(Envelope::new(Target::Room, Request::Start {}).unwrap())
            .await
            .unwrap();
    }

    fn context() -> OperationContext {
        OperationContext::new(Source::Signal, Layer::Policy, None)
    }

    #[tokio::test]
    async fn running_save_publishes_completion_and_drop_drains_the_native_tail() {
        let (_temporary, room) = fixture();
        start(&room).await;
        assert_eq!(room.status().phase, Phase::Running);
        let local = room.local_log().clone();
        let mut records = local.events.subscribe_for_test(StreamKind::Lifecycle);
        let saved = room
            .invoke(Envelope::new(Target::Room, Request::Save {}).unwrap())
            .await
            .unwrap();
        assert_eq!(saved, true);
        assert!(records.drain().iter().any(|record| {
            let status = &record["status"];
            let last = &status["operations"]["last"];
            record["event"] == "room_status"
                && status["phase"] == "running"
                && last["method"] == "save"
                && last["completed_at_ns"].is_u64()
                && last["error"].is_null()
        }));

        let root = room.directory.clone();
        let waiting = root.join("Master/fault-waiting");
        write_marker(&root.join("Master/fault-mode"), "diagnostic-hold");
        // Keep this single-thread runtime still until both pipe writes precede the final drop.
        let deadline = std::time::Instant::now() + Duration::from_secs(4);
        while !waiting.exists() {
            assert!(
                std::time::Instant::now() < deadline,
                "fixture did not write its tail"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
        let weak = Arc::downgrade(&room);
        drop(room);
        assert!(weak.upgrade().is_none());
        wait_for(|| local.output.stats().closed).await;
        assert!(local.output.close().await);
        let stats = local.output.stats();
        assert_eq!(stats.written, stats.accepted);
        assert_eq!(stats.dropped, 0);
        let _lock = RoomLock::try_acquire(&root).unwrap();

        let tail = records.drain();
        let master = || {
            tail.iter()
                .filter(|log| log["attributes"]["dst.shard.name"] == "Master")
        };
        assert_eq!(
            master()
                .filter(|log| {
                    log["event_name"] == "dst.runtime.diagnostic"
                        && log["body"]["kind"] == "lua_error"
                })
                .count(),
            1
        );
        assert_eq!(
            master()
                .filter(|log| {
                    log["event_name"] == "dst.telemetry.coverage"
                        && log["body"]["shard_loss"]["invalid_telemetry"] == 1
                })
                .count(),
            1
        );
    }

    fn notice(delay: f64) -> Countdown {
        Countdown {
            template: "stop {remaining}".into(),
            delay,
            interval: 30.0,
            parameters: Default::default(),
        }
    }

    #[tokio::test]
    async fn stop_reservation_transfers_notice_facts_and_child_cleanup_ownership() {
        let (_temporary, room) = fixture();
        let parent = OperationContext::new(Source::Rpc, Layer::Request, None);
        let child = parent.child(Layer::Room);
        let planned = Some(notice(0.15));
        let mut reservation = room.reserve_stop(&parent, &planned).unwrap();
        let deadline = Instant::now() + Duration::from_secs(1);
        let next = deadline - Duration::from_millis(30);
        room.stop_intent.send_modify(|intent| {
            let intent = intent.as_mut().unwrap();
            intent.deadline = Some(deadline);
            intent.next = Some(next);
            intent.outcome = NoticeOutcome::Accepted;
        });
        let resources = reservation.handoff(&child).unwrap();
        drop(reservation);
        room.clear_stop_intent(&parent.id);
        let unrelated = context();
        room.countdown(&None, Some(&unrelated)).await;
        let intent = room.stop_intent.borrow().clone().unwrap();
        assert_eq!(intent.id, child.id);
        assert_eq!(intent.notice, planned);
        assert_eq!(intent.deadline, Some(deadline));
        assert_eq!(intent.next, Some(next));
        assert!(intent.outcome == NoticeOutcome::Accepted);
        assert_eq!(room.requests.available_permits(), 63);
        assert_eq!(room.mutation.available_permits(), 0);
        room.clear_stop_intent(&child.id);
        drop(resources);
        assert!(room.stop_intent.borrow().is_none());
        assert_eq!(room.requests.available_permits(), 64);
        assert_eq!(room.mutation.available_permits(), 1);
        room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn rejected_stop_handoffs_release_permits_and_preserve_the_current_owner() {
        for scenario in ["wrong_parent", "interrupted", "service", "replaced"] {
            let (_temporary, room) = fixture();
            let parent = OperationContext::new(Source::Rpc, Layer::Request, None);
            let other = context();
            let mut reservation = room.reserve_stop(&parent, &None).unwrap();
            let child = if scenario == "wrong_parent" {
                other.child(Layer::Room)
            } else {
                parent.child(Layer::Room)
            };
            match scenario {
                "interrupted" => room.interrupt(),
                "service" => room.begin_service_shutdown(ShutdownDeadlines::new(Instant::now())),
                "replaced" => room.register_stop_intent(&other.id, &Some(notice(0.15))),
                _ => {}
            }
            let error = match reservation.handoff(&child) {
                Ok(_) => panic!("{scenario} must fail before child admission"),
                Err(error) => error,
            };
            assert_eq!(
                error.code,
                if scenario == "service" {
                    ErrorCode::NotReady
                } else {
                    ErrorCode::Unknown
                }
            );
            drop(reservation);
            assert_eq!(room.requests.available_permits(), 64);
            assert_eq!(room.mutation.available_permits(), 1);
            let intent = room.stop_intent.borrow().clone();
            match scenario {
                "service" => {
                    let intent = intent.unwrap();
                    assert_eq!(intent.id, parent.id);
                    assert!(intent.notice.is_none());
                }
                "replaced" => assert_eq!(intent.unwrap().id, other.id),
                _ => assert!(intent.is_none()),
            }
            room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn late_sibling_failure_is_not_hidden_by_cleanup() {
        for mode in ["generation-exit", "clean", "forced"] {
            let (_temporary, room) = fixture();
            start(&room).await;
            let permit = room.mutation.clone().acquire_owned().await.unwrap();
            write_marker(&room.directory.join("Master/fault-mode"), "decode");
            wait_for(|| room.status().phase == Phase::Failed).await;
            let first = room
                .with_lock(|lock| Ok(recovery::load(lock)?.last_failure.unwrap()))
                .unwrap();
            assert!(!first.shards["Caves"].failed);
            match mode {
                "generation-exit" => {
                    write_marker(&room.directory.join("Caves/fault-mode"), mode);
                    wait_for(|| !room.driver_states()["Caves"].running).await;
                }
                "forced" => {
                    room.drivers()["Caves"].request_kill();
                    wait_for(|| !room.driver_states()["Caves"].running).await;
                }
                _ => {}
            }
            drop(permit);
            let _ = room.wait_fault_cleanup().await;
            let record = room
                .with_lock(|lock| Ok(recovery::load(lock)?.last_failure.unwrap()))
                .unwrap();
            assert_eq!(json!(record.trigger), json!(first.trigger));
            assert_eq!(record.cause, first.cause);
            assert_eq!(
                json!(record.shards["Caves"].cause),
                json!(first.shards["Caves"].cause)
            );
            assert_eq!(
                record.shards["Caves"].failed,
                mode == "generation-exit",
                "{mode}"
            );
            assert!(record.complete);
            assert!(
                record
                    .shards
                    .values()
                    .all(|shard| shard.exit.as_ref().unwrap().output_drained)
            );
            assert_eq!(
                matches!(record.classification, recovery::Failure::Unknown),
                mode == "generation-exit"
            );
            if mode == "generation-exit" {
                assert!(!record.shards["Caves"].evidence_current);
                room.with_lock(|lock| {
                    lock.update_state(|document| {
                        document.get_mut("recovery").unwrap()["restarts_used"] = json!(2);
                        Ok(())
                    })
                })
                .unwrap();
                let catalogs = ["Master", "Caves"]
                    .into_iter()
                    .map(|name| {
                        (
                            name.into(),
                            recovery::Catalog {
                                session_id: format!("S_{name}"),
                                latest_world_file: format!("session/S_{name}/0000000005"),
                                snapshots: (1..=5)
                                    .map(|id| recovery::Snapshot {
                                        snapshot_id: id,
                                        world_file: Some(format!("session/S_{name}/{id:010}")),
                                    })
                                    .collect(),
                            },
                        )
                    })
                    .collect();
                let decision = room
                    .with_lock(|lock| {
                        recovery::on_failure(
                            lock,
                            record.classification,
                            &BTreeSet::from(["Master".into(), "Caves".into()]),
                            &catalogs,
                            100,
                        )
                    })
                    .unwrap();
                assert!(matches!(
                    decision,
                    recovery::Decision::StayClosed {
                        reason: recovery::ClosedReason::UnknownFailure
                    }
                ));
            }
            room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn failed_evidence_write_stays_conservative_after_cancellation() {
        for cancel in [false, true] {
            let (_temporary, room) = fixture();
            start(&room).await;
            let permit = room.mutation.clone().acquire_owned().await.unwrap();
            write_marker(&room.directory.join("Master/fault-mode"), "decode");
            wait_for(|| room.status().phase == Phase::Failed).await;
            let hold = room.directory.join("Caves/fault-mode");
            write_marker(&hold, "generation-hold");
            wait_for(|| {
                room.directory.join("Caves/fault-waiting").exists()
                    && !room.driver_states()["Caves"].ready
            })
            .await;
            let blocked_write = room.directory.join(".dst-config-write.tmp");
            fs::create_dir(&blocked_write).unwrap();
            let mut stopping = Box::pin(room.stop_inner(false, None));
            assert!(futures::poll!(&mut stopping).is_pending());
            wait_for(|| room.driver_states()["Caves"].stopping).await;
            fs::remove_dir(blocked_write).unwrap();
            if cancel {
                drop(stopping);
                fs::remove_file(hold).unwrap();
                wait_for(|| !room.driver_states()["Caves"].running).await;
                let _ = room.stop_inner(false, None).await;
            } else {
                fs::remove_file(hold).unwrap();
                assert_eq!(stopping.await.unwrap_err().code, ErrorCode::Internal);
            }
            drop(permit);
            let _ = room.wait_fault_cleanup().await;
            let record = room
                .with_lock(|lock| Ok(recovery::load(lock)?.last_failure.unwrap()))
                .unwrap();
            assert!(record.complete);
            assert!(
                matches!(record.classification, recovery::Failure::Unknown),
                "cancel={cancel}"
            );
            assert!(
                record
                    .shards
                    .values()
                    .all(|shard| shard.exit.as_ref().unwrap().output_drained)
            );
            room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn initial_failure_write_error_cannot_become_agent_interruption() {
        let (_temporary, room) = fixture();
        start(&room).await;
        let permit = room.mutation.clone().acquire_owned().await.unwrap();
        let blocked_write = room.directory.join(".dst-config-write.tmp");
        fs::create_dir(&blocked_write).unwrap();
        let mut fault = Box::pin(
            room.force_fault(Error::new(ErrorCode::Transport, "observed control failure")),
        );
        assert!(futures::poll!(&mut fault).is_pending());
        drop(fault);
        fs::remove_dir(blocked_write).unwrap();
        drop(permit);
        assert!(room.wait_fault_cleanup().await.is_err());
        let (run, record) = room
            .with_lock(|lock| {
                Ok((
                    recovery::load_run(lock)?,
                    recovery::load(lock)?.last_failure,
                ))
            })
            .unwrap();
        assert!(!run.active);
        assert!(matches!(run.failure, Some(recovery::Failure::Unknown)));
        assert!(record.is_none());
        assert!(
            room.driver_states()
                .values()
                .all(|state| !state.running && state.exit.as_ref().unwrap().output_drained)
        );
        room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn service_shortens_an_inflight_or_sleeping_notice_without_restarting_it() {
        for sending in [true, false] {
            let (_temporary, room) = fixture();
            start(&room).await;
            let hold = room.directory.join("Master/hold-announce");
            if sending {
                fs::write(&hold, "").unwrap();
            }
            let copy = room.clone();
            let request = tokio::spawn(async move {
                copy.invoke(
                    Envelope::new(
                        Target::Room,
                        Request::Stop {
                            notice: Some(notice(60.0)),
                        },
                    )
                    .unwrap(),
                )
                .await
            });
            wait_for(|| {
                room.stop_intent.borrow().as_ref().is_some_and(|intent| {
                    intent.outcome
                        == if sending {
                            NoticeOutcome::Sending
                        } else {
                            NoticeOutcome::Accepted
                        }
                })
            })
            .await;
            if sending {
                wait_for(|| room.directory.join("Master/announce-waiting").exists()).await;
            }
            let at = Instant::now();
            let deadlines = ShutdownDeadlines::new(at);
            let context = context();
            room.begin_service_shutdown(deadlines);
            room.set_service_stop_plan(
                Some((notice(60.0), at + Duration::from_millis(180))),
                &context,
            );
            if sending {
                fs::remove_file(hold).unwrap();
            }
            timeout(
                Duration::from_secs(2),
                room.shutdown_service(deadlines, &context),
            )
            .await
            .unwrap()
            .unwrap();
            request.await.unwrap().unwrap();
            assert!(at.elapsed() < Duration::from_secs(1));
            assert_eq!(
                fs::read_to_string(room.directory.join("announcements"))
                    .unwrap()
                    .lines()
                    .count(),
                2
            );
            assert!(!room.requires_container_restart());
            room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn planned_and_explicit_stop_notices_keep_their_original_boundary() {
        for (delay, planned, expected_count) in [(0.12, 3600.0, 2), (60.0, 0.12, 2), (60.0, 0.0, 1)]
        {
            let (_temporary, room) = fixture();
            start(&room).await;
            let context = context();
            let at = Instant::now();
            let deadlines = ShutdownDeadlines::new(at);
            room.begin_service_shutdown(deadlines);
            room.set_service_stop_plan(
                Some((notice(delay), at + Duration::from_secs_f64(planned))),
                &context,
            );
            timeout(
                Duration::from_secs(2),
                room.shutdown_service(deadlines, &context),
            )
            .await
            .unwrap()
            .unwrap();
            assert!(at.elapsed() < Duration::from_secs(1));
            assert_eq!(
                fs::read_to_string(room.directory.join("announcements"))
                    .unwrap()
                    .lines()
                    .count(),
                expected_count
            );
        }
        for request in [
            Request::Start {},
            Request::Restart { notice: None },
            Request::UpdateMods {
                notice: None,
                restart: true,
            },
        ] {
            let (_temporary, room) = fixture();
            if !matches!(request, Request::Start {}) {
                start(&room).await;
            }
            let mut invocation =
                Box::pin(room.invoke(Envelope::new(Target::Room, request).unwrap()));
            assert!(futures::poll!(&mut invocation).is_pending());
            let deadlines = ShutdownDeadlines::new(Instant::now());
            let context = context();
            room.begin_service_shutdown(deadlines);
            assert!(
                timeout(Duration::from_secs(2), &mut invocation)
                    .await
                    .unwrap()
                    .is_err()
            );
            timeout(
                Duration::from_secs(2),
                room.shutdown_service(deadlines, &context),
            )
            .await
            .unwrap()
            .unwrap();
            assert!(!room.directory.join("announcements").exists());
            assert!(!room.requires_container_restart());
            assert!(room.driver_states().values().all(|state| !state.running));
            assert!(room.while_stopped().await.is_err());
            let run = room.with_lock(|lock| recovery::load_run(lock)).unwrap();
            assert!(run.failure.is_none());
        }
    }

    #[tokio::test]
    async fn service_skips_unavailable_worlds_and_records_notice_outcomes() {
        for outcome in [
            "accepted",
            "rejected",
            "error",
            "unknown",
            "no_running_world",
            "master_unavailable",
        ] {
            let (_temporary, room) = fixture();
            if outcome != "no_running_world" {
                start(&room).await;
            }
            if outcome == "master_unavailable" {
                let master = room.drivers.lock().unwrap().remove("Master").unwrap();
                master.stop(Duration::from_secs(2)).await.unwrap();
            }
            let response = match outcome {
                "rejected" => Some(json!({"ok":true,"data":false})),
                "error" => Some(json!({"ok":false,"error":"lua_error"})),
                "unknown" => Some(json!({"ok":false,"error":"indeterminate"})),
                _ => None,
            };
            if let Some(response) = response {
                fs::write(
                    room.directory.join("announce-response.json"),
                    response.to_string(),
                )
                .unwrap();
            }
            let mut records = room
                .telemetry
                .local
                .events
                .subscribe_for_test(StreamKind::Lifecycle);
            let context = context();
            let at = Instant::now();
            let deadlines = ShutdownDeadlines::new(at);
            room.begin_service_shutdown(deadlines);
            room.set_service_stop_plan(Some((notice(0.1), at + Duration::from_secs(2))), &context);
            timeout(
                Duration::from_secs(2),
                room.shutdown_service(deadlines, &context),
            )
            .await
            .unwrap()
            .unwrap();
            assert!(!room.requires_container_restart());
            room.close_telemetry().await.unwrap();
            let records = records.drain();
            assert!(
                records.iter().any(
                    |record| record["event_name"] == "dst.server.shutdown_notice"
                        && (record["body"]["outcome"] == outcome
                            || record["body"]["reason"] == outcome)
                ),
                "{outcome}"
            );
            assert!(
                !records
                    .iter()
                    .any(|record| record["body"]["stage"] == "failure_finalized"
                        && record["severity_text"] == "ERROR")
            );
        }
    }

    #[tokio::test]
    async fn a_fault_interrupts_the_notice_and_finishes_before_telemetry_close() {
        for persistence_failure in [false, true] {
            let (_temporary, room) = fixture();
            start(&room).await;
            let mut records = room
                .telemetry
                .local
                .events
                .subscribe_for_test(StreamKind::Lifecycle);
            let context = context();
            let deadlines = ShutdownDeadlines::new(Instant::now());
            room.begin_service_shutdown(deadlines);
            let copy = room.clone();
            let shutdown =
                tokio::spawn(async move { copy.shutdown_service(deadlines, &context).await });
            wait_for(|| {
                room.stop_intent
                    .borrow()
                    .as_ref()
                    .is_some_and(|intent| intent.outcome == NoticeOutcome::Accepted)
            })
            .await;
            let mut fault =
                Box::pin(room.force_fault(Error::new(ErrorCode::NotReady, "fixture fault")));
            assert!(futures::poll!(&mut fault).is_pending());
            drop(fault);
            assert!(room.requires_container_restart());
            let state_path = room.directory.join(crate::files::STATE_FILE);
            let before = fs::read(&state_path).unwrap();
            let mut second = None;
            if persistence_failure {
                fs::write(&state_path, "[").unwrap();
                let mut repeated =
                    Box::pin(room.force_fault(Error::new(ErrorCode::Internal, "another fault")));
                assert!(futures::poll!(&mut repeated).is_pending());
                fs::write(&state_path, &before).unwrap();
                second = Some(repeated);
            }
            timeout(Duration::from_secs(3), shutdown)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            if let Some(second) = second {
                assert!(second.await.is_err());
            }
            room.close_telemetry().await.unwrap();
            let records = records.drain();
            assert!(
                records
                    .iter()
                    .any(|record| record["body"]["method"] == "fault_cleanup"
                        && record["body"]["phase"] == "finished")
            );
            assert_eq!(
                records
                    .iter()
                    .filter(|record| record["body"]["stage"] == "persistence_failed")
                    .count(),
                usize::from(persistence_failure)
            );
            let run = room.with_lock(|lock| recovery::load_run(lock)).unwrap();
            assert!(run.failure.is_some());
            let failure = room
                .with_lock(|lock| Ok(recovery::load(lock)?.last_failure.unwrap()))
                .unwrap();
            assert_eq!(failure.trigger.code, Some(ErrorCode::NotReady));
            assert!(failure.complete);
        }
    }

    #[tokio::test]
    async fn notices_wait_from_confirmation_and_release_the_driver_after_five_seconds() {
        for stage in ["first", "confirmed", "reminder", "final"] {
            let (_temporary, room) = fixture();
            start(&room).await;
            let hold = room.directory.join("Master/hold-announce");
            if stage == "first" || stage == "confirmed" {
                fs::write(&hold, "").unwrap();
            }
            let mut records = room
                .telemetry
                .local
                .events
                .subscribe_for_test(StreamKind::Lifecycle);
            let context = context();
            let at = Instant::now();
            let deadlines = ShutdownDeadlines::new(at);
            room.begin_service_shutdown(deadlines);
            let mut planned = notice(if stage == "first" { 60.0 } else { 0.3 });
            if stage == "reminder" {
                planned.interval = 0.1;
            }
            room.set_service_stop_plan(Some((planned, at + Duration::from_secs(60))), &context);
            let copy = room.clone();
            let shutdown =
                tokio::spawn(async move { copy.shutdown_service(deadlines, &context).await });
            if stage == "first" {
                wait_for(|| room.directory.join("Master/announce-waiting").exists()).await;
                tokio::time::sleep(Duration::from_millis(5100)).await;
                fs::remove_file(&hold).unwrap();
            } else {
                if stage == "confirmed" {
                    wait_for(|| room.directory.join("Master/announce-waiting").exists()).await;
                    tokio::time::sleep(Duration::from_millis(150)).await;
                    fs::remove_file(&hold).unwrap();
                }
                wait_for(|| {
                    room.stop_intent
                        .borrow()
                        .as_ref()
                        .is_some_and(|intent| intent.outcome == NoticeOutcome::Accepted)
                })
                .await;
                if stage == "reminder" {
                    fs::write(
                        room.directory.join("announce-response.json"),
                        r#"{"ok":false,"error":"lua_error"}"#,
                    )
                    .unwrap();
                }
                if stage == "final" {
                    fs::write(&hold, "").unwrap();
                    wait_for(|| room.directory.join("Master/announce-waiting").exists()).await;
                    tokio::time::sleep(Duration::from_millis(5100)).await;
                    fs::remove_file(&hold).unwrap();
                }
            }
            timeout(Duration::from_secs(2), shutdown)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            let elapsed = at.elapsed();
            if stage == "confirmed" {
                assert!(elapsed >= Duration::from_millis(450));
            }
            if stage == "reminder" {
                assert!(elapsed < Duration::from_secs(1));
            }
            if stage == "first" || stage == "final" {
                assert!(elapsed < Duration::from_secs(7));
            }
            let announcements = fs::read_to_string(room.directory.join("announcements")).unwrap();
            if stage == "first" {
                assert_eq!(announcements.lines().count(), 1);
            }
            assert!(!room.requires_container_restart());
            room.close_telemetry().await.unwrap();
            let records = records.drain();
            let outcomes: Vec<_> = records
                .iter()
                .filter(|record| record["event_name"] == "dst.server.shutdown_notice")
                .collect();
            if stage == "first" || stage == "final" {
                assert!(
                    outcomes
                        .iter()
                        .any(|record| record["body"]["stage"] == stage
                            && record["body"]["outcome"] == "unknown"
                            && record["body"]["written"] == true)
                );
            }
            if stage == "reminder" {
                assert!(
                    outcomes
                        .iter()
                        .any(|record| record["body"]["stage"] == "reminder"
                            && record["body"]["outcome"] == "error")
                );
            }
        }
    }

    #[tokio::test]
    async fn service_deadlines_reap_every_shard_without_waiting_for_the_room_permit() {
        for expired in [false, true] {
            let (_temporary, room) = fixture();
            fs::write(room.directory.join("ignore-term"), "").unwrap();
            fs::write(room.directory.join("hang-shutdown"), "").unwrap();
            start(&room).await;
            let drivers = room.drivers();
            let master = drivers["Master"].clone();
            let mut original = Box::pin(master.stop(Duration::from_secs(30)));
            assert!(futures::poll!(&mut original).is_pending());
            wait_for(|| {
                fs::read_to_string(room.directory.join("Master/native-lifecycle"))
                    .unwrap()
                    .contains("shutdown\n")
            })
            .await;
            drop(original);
            let permit = room.mutation.clone().acquire_owned().await.unwrap();
            let at = Instant::now();
            let mut deadlines = ShutdownDeadlines::new(at);
            deadlines.native = if expired {
                at - Duration::from_secs(1)
            } else {
                at + Duration::from_millis(80)
            };
            deadlines.terminate = if expired {
                at - Duration::from_millis(500)
            } else {
                at + Duration::from_millis(160)
            };
            room.begin_service_shutdown(deadlines);
            let context = context();
            timeout(
                Duration::from_secs(2),
                room.enforce_service_deadlines(deadlines, &context),
            )
            .await
            .unwrap();
            for driver in drivers.values() {
                let exit = timeout(Duration::from_secs(2), driver.wait())
                    .await
                    .unwrap()
                    .unwrap();
                assert!(exit.forced && exit.output_drained);
                assert_eq!(unsafe { libc::kill(driver.snapshot().pid as i32, 0) }, -1);
            }
            assert!(at.elapsed() < Duration::from_secs(1));
            let report = master.stop(Duration::from_secs(30)).await.unwrap();
            assert!(report.forced && report.output_drained);
            assert_eq!(
                fs::read_to_string(room.directory.join("Master/native-lifecycle"))
                    .unwrap()
                    .matches("shutdown\n")
                    .count(),
                1
            );
            drop(permit);
            room.close_telemetry().await.unwrap();
        }
    }
}

impl ShutdownDeadlines {
    pub(crate) fn new(start: Instant) -> Self {
        Self {
            handoff: start + Duration::from_secs(180),
            notice: start + Duration::from_secs(250),
            native: start + Duration::from_secs(370),
            terminate: start + Duration::from_secs(380),
        }
    }

    fn stop(self) -> StopDeadlines {
        StopDeadlines {
            native: self.native,
            terminate: self.terminate,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum NoticeOutcome {
    Planned,
    Sending,
    Accepted,
    Rejected,
    Error,
    Unknown,
}

#[derive(Clone)]
pub(super) struct StopIntent {
    id: String,
    notice: Option<Countdown>,
    deadline: Option<Instant>,
    next: Option<Instant>,
    outcome: NoticeOutcome,
}

impl StopIntent {
    fn new(id: &str, notice: Option<Countdown>) -> Self {
        Self {
            id: id.into(),
            notice,
            deadline: None,
            next: Some(Instant::now()),
            outcome: NoticeOutcome::Planned,
        }
    }

    fn shorten_notice(&mut self, at: Instant) {
        if self.notice.is_some() {
            self.deadline = Some(self.deadline.map_or(at, |deadline| deadline.min(at)));
            self.next = self.next.map(|next| next.min(at));
        }
    }
}

impl Room {
    pub(crate) fn reserve_stop(
        self: &Arc<Self>,
        parent: &OperationContext,
        notice: &Option<Countdown>,
    ) -> Result<StopReservation> {
        let _drivers = self.drivers.lock().unwrap();
        self.ensure_open()?;
        let slot = self
            .requests
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::new(ErrorCode::Busy, "room request limit reached"))?;
        let mutation = self.mutation.clone().try_acquire_owned().map_err(|_| {
            Error::new(ErrorCode::Busy, "another room mutation is running")
                .with_details(json!({"operation":self.status().operations.current}))
        })?;
        let interrupted = self.interrupted.subscribe();
        let revision = *interrupted.borrow();
        self.register_stop_intent(&parent.id, notice);
        Ok(StopReservation {
            resources: Some((slot, mutation)),
            room: self.clone(),
            id: parent.id.clone(),
            interrupted,
            revision,
        })
    }

    pub(super) fn ensure_open(&self) -> Result<()> {
        if self.shutdown_pending.load(Ordering::Acquire) > 0 {
            Err(Error::shutdown(ErrorCode::NotReady))
        } else {
            Ok(())
        }
    }

    pub(super) fn register_stop_intent(&self, id: &str, notice: &Option<Countdown>) {
        self.stop_intent
            .send_replace(Some(StopIntent::new(id, notice.clone())));
    }

    pub(crate) fn finish_stop_notice(&self) -> bool {
        self.stop_intent.send_if_modified(|intent| {
            let Some(intent) = intent else { return false };
            intent.shorten_notice(Instant::now());
            true
        })
    }

    pub(super) fn clear_stop_intent(&self, id: &str) {
        let _drivers = self.drivers.lock().unwrap();
        if self.service_shutdown.lock().unwrap().is_none() {
            self.stop_intent.send_if_modified(|intent| {
                if intent.as_ref().is_some_and(|intent| intent.id == id) {
                    *intent = None;
                    true
                } else {
                    false
                }
            });
        }
    }

    pub(crate) fn begin_service_shutdown(&self, deadlines: ShutdownDeadlines) {
        let drivers = self.drivers.lock().unwrap();
        self.shutdown_pending.fetch_add(1, Ordering::AcqRel);
        *self.service_shutdown.lock().unwrap() = Some(deadlines);
        for driver in drivers.values() {
            driver.tighten_stop(deadlines.stop());
        }
        self.stop_intent.send_modify(|intent| {
            if let Some(intent) = intent.as_mut() {
                intent.deadline = intent
                    .deadline
                    .map(|at| at.min(deadlines.notice - NOTICE_SEND_TIMEOUT));
                intent.next = intent
                    .next
                    .map(|at| at.min(deadlines.notice - NOTICE_SEND_TIMEOUT));
            }
        });
    }

    pub(crate) fn set_service_stop_plan(
        &self,
        plan: Option<(Countdown, Instant)>,
        context: &OperationContext,
    ) {
        if let Some((notice, at)) = plan {
            self.stop_intent.send_modify(|intent| {
                let current =
                    intent.get_or_insert_with(|| StopIntent::new(&context.id, Some(notice)));
                current.shorten_notice(at);
            });
        }
    }

    pub(crate) fn expedite_service_shutdown(
        &self,
        reason: &str,
        context: &OperationContext,
    ) -> bool {
        if !self.shutdown_expedited.swap(true, Ordering::AcqRel) {
            self.record_management(
                "dst.server.shutdown",
                json!({"stage":"handoff","reason":reason}),
                "WARN",
                Some(context),
            );
            self.interrupt();
            return true;
        }
        false
    }

    pub(crate) async fn enforce_service_deadlines(
        &self,
        deadlines: ShutdownDeadlines,
        context: &OperationContext,
    ) {
        tokio::time::sleep_until(deadlines.native).await;
        for driver in self.drivers().values() {
            if !driver.snapshot().running {
                continue;
            }
            self.record_management("dst.server.shutdown", json!({"stage":"terminate","reason":"native_deadline","shard":driver.snapshot().shard}), "WARN", Some(context));
            driver.tighten_stop(deadlines.stop());
            driver.request_shutdown(deadlines.terminate);
        }
        tokio::time::sleep_until(deadlines.terminate).await;
        for driver in self.drivers().values() {
            if !driver.snapshot().running {
                continue;
            }
            self.record_management("dst.server.shutdown", json!({"stage":"kill","reason":"terminate_deadline","shard":driver.snapshot().shard}), "WARN", Some(context));
            driver.request_kill();
        }
    }

    pub(crate) async fn shutdown_service(
        self: &Arc<Self>,
        deadlines: ShutdownDeadlines,
        parent: &OperationContext,
    ) -> Result<StopResult> {
        let mut interrupted = self.interrupted.subscribe();
        let permit = self.mutation.clone().acquire_owned();
        tokio::pin!(permit);
        let permit = match tokio::time::timeout_at(deadlines.handoff, &mut permit).await {
            Ok(result) => result,
            Err(_) => {
                self.expedite_service_shutdown("handoff_timeout", parent);
                permit.await
            }
        }
        .map_err(|_| Error::new(ErrorCode::Internal, "room operation queue closed"))?;
        let mut context = parent.child(Layer::Room);
        let operation = context.status("stop", Target::Room);
        let observation = self.begin_operation(&operation, &mut context, "started");
        if self.shutdown_expedited.load(Ordering::Acquire) || Instant::now() >= deadlines.handoff {
            self.notice_skipped("handoff_ended", Some(&context));
        } else if self.requires_container_restart() {
            self.notice_skipped("fault", Some(&context));
        } else if self.status().phase != Phase::Running {
            self.notice_skipped("no_running_world", Some(&context));
        } else {
            self.stop_intent.send_if_modified(|intent| {
                if intent.is_none() {
                    *intent = Some(StopIntent::new(&context.id, shutdown_notice()));
                    true
                } else {
                    false
                }
            });
            let id = self
                .stop_intent
                .borrow()
                .as_ref()
                .expect("service stop intent")
                .id
                .clone();
            if id != context.id {
                self.record_management(
                    "dst.server.shutdown_notice",
                    json!({"stage":"continued","stop_intent_id":id}),
                    "INFO",
                    Some(&context),
                );
            }
            tokio::select! {
                biased;
                _ = interrupted.changed() => self.notice_skipped("interrupted", Some(&context)),
                _ = self.run_stop_notice(&id, Some(&context)) => {},
            }
        }
        let result = self.stop_inner(false, Some(&context)).await;
        self.finish_operation(
            &operation,
            &context,
            observation,
            result.as_ref().map(|_| "completed"),
        );
        drop(permit);
        result
    }

    pub(super) fn notice_skipped(&self, reason: &str, context: Option<&OperationContext>) {
        self.record_management(
            "dst.server.shutdown_notice",
            json!({"stage":"skipped","reason":reason}),
            "INFO",
            context,
        );
    }

    pub(crate) async fn stop_announcement(&self, message: String, context: &OperationContext) {
        let _ = self
            .send_stop_notice(message, "final", Some(context), NOTICE_SEND_TIMEOUT)
            .await;
    }

    async fn send_stop_notice(
        &self,
        message: String,
        stage: &str,
        context: Option<&OperationContext>,
        duration: Duration,
    ) -> NoticeOutcome {
        if duration.is_zero() {
            self.notice_skipped("deadline", context);
            return NoticeOutcome::Unknown;
        }
        let result = match self.driver(&self.master()) {
            Ok(driver) => driver
                .request("announce", json!({"message":message,"count":1}), duration)
                .await
                .map_err(driver_error),
            Err(error) => Err(error),
        };
        let outcome = match &result {
            Ok(value) if *value == true => NoticeOutcome::Accepted,
            Ok(_) => NoticeOutcome::Rejected,
            Err(error) if matches!(error.code, ErrorCode::Timeout | ErrorCode::Unknown) => {
                NoticeOutcome::Unknown
            }
            Err(_) => NoticeOutcome::Error,
        };
        self.record_management(
            "dst.server.shutdown_notice",
            json!({"stage":stage,"outcome":outcome,"error_code":result.as_ref().err().map(|error|error.code),"written":result.as_ref().err().and_then(|error|error.details["written"].as_bool()),"accepted":result.as_ref().err().and_then(|error|error.details["accepted"].as_bool())}),
            if outcome == NoticeOutcome::Accepted { "INFO" } else { "WARN" }, context,
        );
        outcome
    }

    pub(super) async fn countdown(
        &self,
        notice: &Option<Countdown>,
        context: Option<&OperationContext>,
    ) {
        let id = context
            .map(|context| context.id.clone())
            .unwrap_or_default();
        self.stop_intent.send_if_modified(|intent| {
            if intent.is_none() {
                *intent = Some(StopIntent::new(&id, notice.clone()));
                true
            } else {
                false
            }
        });
        self.run_stop_notice(&id, context).await;
    }

    async fn run_stop_notice(&self, id: &str, context: Option<&OperationContext>) {
        if self
            .drivers()
            .values()
            .all(|driver| !driver.snapshot().running)
        {
            self.notice_skipped("no_running_world", context);
            return;
        }
        if !self
            .driver(&self.master())
            .is_ok_and(|driver| driver.snapshot().ready)
        {
            self.notice_skipped("master_unavailable", context);
            return;
        }
        let mut changes = self.stop_intent.subscribe();
        loop {
            let Some(intent) = changes
                .borrow_and_update()
                .as_ref()
                .filter(|intent| intent.id == id)
                .cloned()
            else {
                return;
            };
            let Some(notice) = intent.notice else {
                self.notice_skipped("explicit_none", context);
                return;
            };
            if intent.outcome == NoticeOutcome::Sending && intent.next.is_none() {
                self.notice_skipped("prior_result_unknown", context);
                return;
            }
            let Some(next) = intent.next else {
                return;
            };
            tokio::select! {
                biased;
                _ = changes.changed() => continue,
                _ = tokio::time::sleep_until(next) => {},
            }
            let now = Instant::now();
            let first = intent.outcome == NoticeOutcome::Planned;
            let mut remaining = intent
                .deadline
                .map_or(Duration::from_secs_f64(notice.delay), |at| {
                    at.saturating_duration_since(now)
                });
            if first {
                remaining = remaining.min(Duration::from_secs_f64(notice.delay));
            }
            let stage = if first {
                "first"
            } else if remaining.is_zero() {
                "final"
            } else {
                "reminder"
            };
            let cap = self
                .service_shutdown
                .lock()
                .unwrap()
                .map(|deadlines| deadlines.notice);
            let mut duration = cap.map_or(NOTICE_SEND_TIMEOUT, |at| {
                NOTICE_SEND_TIMEOUT.min(at.saturating_duration_since(now))
            });
            if intent.deadline.is_some() && !remaining.is_zero() {
                duration = duration.min(remaining);
            }
            let Ok(message) = crate::policy::render_countdown(&notice, remaining.as_secs_f64())
            else {
                return;
            };
            self.stop_intent.send_modify(|current| {
                let Some(current) = current.as_mut().filter(|intent| intent.id == id) else {
                    return;
                };
                current.outcome = NoticeOutcome::Sending;
                current.next = intent
                    .deadline
                    .filter(|_| !first && !remaining.is_zero())
                    .map(|at| {
                        crate::policy::next_notice(
                            at,
                            remaining,
                            Duration::from_secs_f64(notice.interval),
                        )
                    });
            });
            let outcome = self
                .send_stop_notice(message, stage, context, duration)
                .await;
            let cap = self
                .service_shutdown
                .lock()
                .unwrap()
                .map(|deadlines| deadlines.notice);
            self.stop_intent.send_modify(|current| {
                let Some(current) = current.as_mut().filter(|intent| intent.id == id) else {
                    return;
                };
                current.outcome = outcome;
                if first {
                    if outcome != NoticeOutcome::Accepted || remaining.is_zero() {
                        current.next = None;
                        return;
                    }
                    let at = Instant::now() + Duration::from_secs_f64(notice.delay);
                    let at = current.deadline.map_or(at, |deadline| deadline.min(at));
                    current.deadline =
                        Some(cap.map_or(at, |cap| at.min(cap - NOTICE_SEND_TIMEOUT)));
                } else if remaining.is_zero() {
                    current.next = None;
                    return;
                }
                let at = current
                    .deadline
                    .expect("accepted or existing notice has a deadline");
                current.next = Some(crate::policy::next_notice(
                    at,
                    at.saturating_duration_since(Instant::now()),
                    Duration::from_secs_f64(notice.interval),
                ));
            });
        }
    }
}
