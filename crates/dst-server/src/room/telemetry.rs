//! Room outputs live through the last shard observation and drain together on final shutdown.

use super::*;
use crate::{
    driver::{DriverEventKind, TelemetryProfile},
    events::coverage::{CaptureHealth, CoverageBody, RoomLoss, ShardLoss},
    telemetry::{
        LogExporter,
        local::{FinishOutput, LocalLog, LocalOutput},
        room_export,
    },
};

#[derive(Default)]
struct Coverage {
    checked: Option<(u64, Instant)>,
    attempted: Option<CoverageReading>,
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
struct CoverageReading {
    generation: u64,
    revision: u64,
    errors: u64,
    play_queue_dropped: u64,
    shard_loss: ShardLoss,
    room_loss: RoomLoss,
}

/// Loss before output delivery, across all shard processes in this room.
#[derive(Default, Serialize)]
struct StreamLoss {
    source_gaps: AtomicU64,
    relay_dropped: AtomicU64,
    observation_failed: AtomicU64,
}

pub(super) struct RoomTelemetry {
    losses: StreamLoss,
    pub(super) local: LocalLog,
    pub(super) exporter: Option<Arc<LogExporter>>,
    pub(super) observer: Option<Arc<crate::observability::Observability>>,
}

impl RoomTelemetry {
    pub(super) fn new(mut config: TelemetryConfig, events: EventHub) -> Self {
        let local = LocalLog {
            events,
            resource: Arc::new(config.resource.clone()),
            output: LocalOutput::new(),
        };
        let exporter = room_export(
            "LOGS",
            config
                .logs
                .take()
                .map(|settings| {
                    LogExporter::with_local(settings, &config.resource, Some(local.clone()))
                        .map(Arc::new)
                })
                .transpose(),
        );
        let observer =
            crate::observability::Observability::for_room(&config, local.clone(), exporter.clone())
                .map(Arc::new);
        Self {
            losses: StreamLoss::default(),
            local,
            exporter,
            observer,
        }
    }

    pub(super) fn publish(&self, log: &LogEvent, stream: StreamKind) {
        self.local.publish(log, stream);
        if let Some(exporter) = &self.exporter {
            let _ = exporter.submit(log);
        }
    }

    pub(super) fn observation_failed(&self) {
        self.losses
            .observation_failed
            .fetch_add(1, Ordering::Relaxed);
    }
}

struct ShardTelemetry {
    outputs: Arc<RoomTelemetry>,
    cluster: String,
    profile: TelemetryProfile,
    driver: Driver,
    relay_dropped: Arc<AtomicU64>,
    coverage: Mutex<Coverage>,
}

impl ShardTelemetry {
    fn account_drops(&self, name: &str, total: u64, previous: &mut u64) {
        if total > *previous {
            let count = total - *previous;
            self.outputs
                .losses
                .relay_dropped
                .fetch_add(count, Ordering::Relaxed);
            if let Some(observer) = &self.outputs.observer {
                observer.record_relay_drop(&self.cluster, name, count);
            }
            *previous = total;
        }
    }

    fn record_coverage(&self, state: &DriverState) {
        let (Some(generation), Some(health)) = (state.generation, &state.health) else {
            return;
        };
        let mut coverage = self.coverage.lock().unwrap();
        let now = Instant::now();
        if coverage.checked.is_some_and(|(prior, time)| {
            prior > generation
                || (state.running
                    && prior == generation
                    && now - time < crate::policy::CHECK_INTERVAL)
        }) {
            return;
        }
        coverage.checked = Some((generation, now));
        let Some(health) = CaptureHealth::from_game(health) else {
            return;
        };
        let reply = crate::process::StreamKind::Reply as usize;
        let rejected = |reason| {
            state
                .telemetry_rejections
                .get(&reason)
                .copied()
                .unwrap_or_default()
        };
        let point = CoverageReading {
            generation,
            revision: health.revision,
            errors: health.errors,
            play_queue_dropped: health.play_queue_dropped,
            shard_loss: ShardLoss {
                telemetry_gaps: state.telemetry_gaps,
                invalid_telemetry: state.invalid_telemetry,
                generation_rejected: rejected(crate::events::RejectionReason::Generation),
                sequence_rejected: rejected(crate::events::RejectionReason::Sequence),
                reply_dropped: state.exit.as_ref().map_or_else(
                    || self.driver.output_stats().dropped[reply],
                    |exit| exit.stats.dropped[reply],
                ),
                relay_dropped: self.relay_dropped.load(Ordering::Relaxed),
            },
            room_loss: RoomLoss {
                stdout_dropped: self.outputs.local.output.dropped(),
            },
        };
        if coverage.attempted == Some(point) {
            return;
        }
        let prior = coverage.attempted.unwrap_or_default();
        let prior_lua = if prior.generation == generation {
            prior
        } else {
            CoverageReading::default()
        };
        let lost = point.errors > prior_lua.errors
            || point.play_queue_dropped > prior_lua.play_queue_dropped
            || point.shard_loss != prior.shard_loss
            || point.room_loss != prior.room_loss;
        let severity = if lost || health.degraded() {
            "WARN"
        } else {
            "INFO"
        };
        let mut log = LogEvent::new(
            "dst.telemetry.coverage",
            json!(CoverageBody {
                requested_profile: self.profile,
                health,
                shard_loss: point.shard_loss,
                room_loss: point.room_loss,
            }),
            severity,
        );
        log.attributes.extend([
            ("dst.cluster.name".into(), json!(self.cluster)),
            ("dst.shard.name".into(), json!(state.shard)),
            ("dst.game.attempt.id".into(), json!(state.nonce)),
            ("dst.runtime.generation".into(), json!(generation)),
        ]);
        self.outputs.publish(&log, StreamKind::Lifecycle);
        coverage.attempted = Some(point);
    }
}

impl Room {
    #[cfg(test)]
    pub(crate) fn local_log(&self) -> &LocalLog {
        &self.telemetry.local
    }

    pub fn telemetry_stats(&self) -> Value {
        let mut receivers = BTreeMap::new();
        let mut processes = BTreeMap::new();
        for (name, driver) in self.drivers() {
            let state = driver.watch();
            let state = state.borrow();
            receivers.insert(name.clone(), json!({
                "rejections":state.telemetry_rejections, "last_rejection":state.last_telemetry_rejection,
            }));
            processes.insert(name, driver.output_stats());
        }
        json!({
            "streams":self.telemetry.losses, "rpc":self.telemetry.local.events.stats(),
            "local_output":self.telemetry.local.output.stats(), "processes":processes, "receivers":receivers,
            "exporter":self.telemetry.exporter.as_ref().map(|exporter| exporter.stats()),
        })
    }
    /// Drain the final records after game processes and room mutations have stopped.
    pub async fn close_telemetry(&self) -> Result<Value> {
        let _ = self.wait_fault_cleanup().await;
        let mut completion = {
            let mut close = self.telemetry_close.lock().unwrap();
            if close.is_none() {
                let drivers = self.drivers.lock().unwrap();
                if drivers.values().any(|driver| driver.snapshot().running) {
                    return Err(Error::new(
                        ErrorCode::Busy,
                        "stop the game before closing room telemetry",
                    ));
                }
                let _operation = self.mutation.try_acquire().map_err(|_| {
                    Error::new(ErrorCode::Busy, "a room operation is still running")
                })?;
                self.requests.close();
                self.shutdown_pending.fetch_add(1, Ordering::AcqRel);
                *close = Some(start_telemetry_close(
                    std::mem::take(&mut *self.relays.lock().unwrap()),
                    self.telemetry.clone(),
                ));
            }
            close.as_ref().unwrap().clone()
        };
        let result = completion
            .wait_for(Option::is_some)
            .await
            .map_err(|_| Error::new(ErrorCode::Internal, "telemetry shutdown worker stopped"))?
            .clone()
            .unwrap();
        match result {
            Ok(mut report) => {
                report["stats"] = self.telemetry_stats();
                Ok(report)
            }
            Err(mut error) => {
                error.details["stats"] = self.telemetry_stats();
                Err(error)
            }
        }
    }

    pub(super) fn monitor(
        self: &Arc<Self>,
        name: String,
        driver: &Driver,
        mut events: DriverEvents,
    ) {
        let weak = Arc::downgrade(self);
        let mut states = driver.watch();
        let shard = Arc::new(ShardTelemetry {
            outputs: self.telemetry.clone(),
            cluster: self.cluster_name.clone(),
            profile: self.options.lock().unwrap().profile,
            driver: driver.clone(),
            relay_dropped: events.drop_counter(),
            coverage: Mutex::new(Coverage::default()),
        });
        if let Some(observer) = &shard.outputs.observer {
            observer.observe_state(&states.borrow(), &shard.cluster);
        }
        let relay_shard = shard.clone();
        let relay = tokio::spawn(async move {
            let shard = relay_shard;
            let mut dropped = 0;
            while let Some(event) = events.recv().await {
                shard.account_drops(&name, events.dropped(), &mut dropped);
                let stream = match &event.kind {
                    DriverEventKind::Telemetry(game) => {
                        if let Some(observer) = &shard.outputs.observer {
                            observer.observe_gameplay(
                                &shard.cluster,
                                &event.shard,
                                game.body.name(),
                            );
                        }
                        StreamKind::Events
                    }
                    DriverEventKind::Log(_) => StreamKind::Logs,
                    DriverEventKind::Lifecycle(_) | DriverEventKind::Control(_) => {
                        StreamKind::Lifecycle
                    }
                    DriverEventKind::Diagnostic(_) => StreamKind::Events,
                };
                match LogEvent::from_driver(event, &shard.cluster) {
                    Ok(log) => shard.outputs.publish(&log, stream),
                    Err(_) => shard.outputs.observation_failed(),
                }
            }
            shard.account_drops(&name, events.dropped(), &mut dropped);
            shard.record_coverage(&shard.driver.snapshot());
        });
        let monitor = tokio::spawn(async move {
            let mut diagnostics = 0;
            let mut gaps = 0;
            loop {
                let state = states.borrow_and_update().clone();
                if let Some(log) = crate::telemetry::management::lua_diagnostic(
                    state.native_diagnostics.lua_error.as_ref(),
                    &mut diagnostics,
                    &shard.cluster,
                    &state.shard,
                    &state.process_id,
                    &state.nonce,
                    state.generation,
                ) {
                    shard.outputs.publish(&log, StreamKind::Lifecycle);
                }
                if state.telemetry_gaps > gaps {
                    shard
                        .outputs
                        .losses
                        .source_gaps
                        .fetch_add(state.telemetry_gaps - gaps, Ordering::Relaxed);
                    gaps = state.telemetry_gaps;
                }
                if state.running {
                    shard.record_coverage(&state);
                }
                if let Some(room) = weak.upgrade() {
                    room.observe_monitored_shard(&shard.driver, &state);
                } else if !state.running
                    && let Some(observer) = &shard.outputs.observer
                {
                    observer.observe_state(&state, &shard.cluster);
                }
                if !state.running || states.changed().await.is_err() {
                    break;
                }
            }
        });
        let mut relays = self.relays.lock().unwrap();
        relays.retain(|relay| !relay.is_finished());
        relays.extend([relay, monitor]);
    }
}

impl Drop for Room {
    fn drop(&mut self) {
        let drivers = self
            .drivers
            .get_mut()
            .unwrap()
            .values()
            .cloned()
            .collect::<Vec<_>>();
        for driver in &drivers {
            driver.request_kill();
        }
        let lock = self.lock.get_mut().unwrap().take();
        let telemetry = self.telemetry.clone();
        let relays = std::mem::take(self.relays.get_mut().unwrap());
        let closing = self.telemetry_close.get_mut().unwrap().take();
        if let Some(runtime) = self
            .runtime
            .get_mut()
            .unwrap()
            .take()
            .or_else(|| tokio::runtime::Handle::try_current().ok())
        {
            let finish = FinishOutput(telemetry.local.output.clone());
            runtime.spawn(async move {
                let _finish = finish;
                join_all(drivers.iter().map(Driver::wait)).await;
                drop(lock);
                let mut completion =
                    closing.unwrap_or_else(|| start_telemetry_close(relays, telemetry));
                let _ = completion.wait_for(Option::is_some).await;
            });
        } else {
            telemetry.local.output.finish();
        }
    }
}

fn start_telemetry_close(
    relays: Vec<tokio::task::JoinHandle<()>>,
    telemetry: Arc<RoomTelemetry>,
) -> watch::Receiver<Option<Result<Value>>> {
    let (sender, receiver) = watch::channel(None);
    let finish = FinishOutput(telemetry.local.output.clone());
    tokio::spawn(async move {
        let _finish = finish;
        let aborts: Vec<_> = relays.iter().map(|relay| relay.abort_handle()).collect();
        let mut completion = Box::pin(join_all(relays));
        let mut relay_errors = Vec::new();
        let results = match timeout(Duration::from_secs(5), &mut completion).await {
            Ok(results) => results,
            Err(_) => {
                relay_errors.push("telemetry relay drain timed out".to_owned());
                for relay in aborts {
                    relay.abort();
                }
                completion.await
            }
        };
        relay_errors.extend(
            results
                .into_iter()
                .filter_map(|result| result.err().map(|error| error.to_string())),
        );
        // Settle log delivery before the SDK's final metric collection.
        let export_error = if let Some(exporter) = &telemetry.exporter {
            exporter.close().await.err().map(|error| error.to_string())
        } else {
            None
        };
        let observability_error = if let Some(observer) = &telemetry.observer {
            observer.close().await.err().map(|error| error.to_string())
        } else {
            None
        };
        let output_drained = telemetry.local.output.close().await;
        let failed = !relay_errors.is_empty()
            || !output_drained
            || export_error.is_some()
            || observability_error.is_some();
        let report = json!({"relays_drained":relay_errors.is_empty(),"relay_errors":relay_errors,"output_drained":output_drained,"export_error":export_error,"observability_error":observability_error});
        sender.send_replace(Some(if failed {
            Err(
                Error::new(ErrorCode::Internal, "telemetry shutdown did not complete")
                    .with_details(report),
            )
        } else {
            Ok(report)
        }));
    });
    receiver
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stopped_room() -> (tempfile::TempDir, Arc<Room>) {
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("cluster.ini"), "").unwrap();
        std::fs::create_dir(directory.path().join("Master")).unwrap();
        std::fs::write(
            directory.path().join("Master/server.ini"),
            "[SHARD]\nis_master=true\n",
        )
        .unwrap();
        let room = Room::open(
            directory.path(),
            "/unused-game",
            DriverOptions::default(),
            EventHub::default(),
            TelemetryConfig {
                resource: serde_json::Map::from_iter([
                    ("service.instance.id".into(), json!("fixed")),
                    ("dst.cluster.name".into(), json!("shared-room")),
                ]),
                ..Default::default()
            },
        )
        .unwrap();
        (directory, room)
    }

    #[tokio::test]
    async fn broken_stdout_counts_one_output_loss_and_preserves_the_rpc_record() {
        let (_directory, mut room) = stopped_room();
        let telemetry = &mut Arc::get_mut(&mut room).unwrap().telemetry;
        Arc::get_mut(telemetry).unwrap().local.output =
            LocalOutput::with_writer(1, 4096, |_| Err(std::io::ErrorKind::BrokenPipe.into()));
        let mut events = room
            .local_log()
            .events
            .subscribe_for_test(StreamKind::Lifecycle);
        let log = LogEvent::new("dst.server.ready", json!({}), "INFO");
        room.telemetry.publish(&log, StreamKind::Lifecycle);

        let close = room.close_telemetry().await.unwrap();
        let stats = &close["stats"];
        let output = &stats["local_output"];
        assert_eq!(output["accepted"], 1);
        assert_eq!(output["written"], 0);
        assert_eq!(output["write_failed"], 1);
        assert_eq!(output["dropped"], 1);
        assert_eq!(stats["streams"]["observation_failed"], 0);
        let records = events.drain();
        assert_eq!(records.len(), 1);
        assert_eq!(
            records[0]["attributes"]["log.record.uid"],
            log.attributes["log.record.uid"]
        );
    }

    #[tokio::test]
    async fn coverage_delivers_once_per_observation_and_keeps_shard_and_room_loss_separate() {
        let (directory, room) = stopped_room();
        let driver = Driver::attach(
            crate::process::ProcessSpec {
                program: "/bin/true".into(),
                args: vec![],
                cwd: directory.path().into(),
            },
            ulid::Ulid::generate().to_string(),
            "Master".into(),
            None,
        )
        .unwrap();
        let exit = driver.wait().await.unwrap();
        let mut state = driver.snapshot();
        state.running = true;
        state.exit = None;
        state.generation = Some(1);
        state.health = Some(json!({
            "profile":"off", "telemetry_status":"disabled", "revision":0, "errors":0,
            "play_queue_dropped":0, "capabilities":{}, "faults":{}, "last_error":null,
        }));
        let shard = ShardTelemetry {
            outputs: room.telemetry.clone(),
            cluster: room.cluster_name.clone(),
            profile: TelemetryProfile::Off,
            driver,
            relay_dropped: Arc::new(AtomicU64::new(0)),
            coverage: Mutex::new(Coverage::default()),
        };
        let mut records = room
            .telemetry
            .local
            .events
            .subscribe_for_test(StreamKind::Lifecycle);
        shard.record_coverage(&state);
        assert_eq!(records.drain().len(), 1);

        room.telemetry.local.output.finish();
        state.generation = Some(2);
        shard.record_coverage(&state);
        shard.record_coverage(&state);
        assert_eq!(records.drain().len(), 1);
        assert_eq!(
            shard.coverage.lock().unwrap().attempted.unwrap().generation,
            2
        );

        // A sibling on the same room contributes to the aggregate but not this shard's counter.
        room.telemetry
            .losses
            .relay_dropped
            .fetch_add(42, Ordering::Relaxed);
        shard.relay_dropped.store(2, Ordering::Relaxed);
        state.invalid_telemetry = 1;
        state
            .telemetry_rejections
            .insert(crate::events::RejectionReason::Generation, 3);
        state
            .telemetry_rejections
            .insert(crate::events::RejectionReason::Sequence, 4);
        state.health.as_mut().unwrap()["play_queue_dropped"] = json!(3);
        state.running = false;
        let mut exit = crate::process::ExitSummary::from(&exit);
        exit.stats.dropped[crate::process::StreamKind::Reply as usize] = 1;
        state.exit = Some(exit);
        shard.record_coverage(&state);
        let logs = records.drain();
        let [log] = logs.as_slice() else {
            panic!("expected one final coverage record")
        };
        assert_eq!(log["attributes"]["dst.cluster.name"], "shared-room");
        assert_eq!(room.local_log().resource["dst.cluster.name"], "shared-room");
        assert_eq!(log["severity_text"], "WARN");
        assert_eq!(log["body"]["play_queue_dropped"], 3);
        assert_eq!(
            log["body"]["shard_loss"],
            json!({"telemetry_gaps":0,"invalid_telemetry":1,"generation_rejected":3,"sequence_rejected":4,"reply_dropped":1,"relay_dropped":2})
        );
        assert_eq!(log["body"]["room_loss"], json!({"stdout_dropped":1}));
    }

    #[tokio::test]
    async fn telemetry_close_retains_relays_across_cancellation_and_drop_releases_local_output() {
        let mut instances = BTreeSet::new();
        for drop_room in [false, true] {
            let (_directory, room) = stopped_room();
            let weak = Arc::downgrade(&room);
            let local = room.local_log().clone();
            let instance = local.resource["service.instance.id"]
                .as_str()
                .unwrap()
                .parse::<ulid::Ulid>()
                .unwrap();
            assert!(instances.insert(instance));
            let mut records = local.events.subscribe_for_test(StreamKind::Lifecycle);
            let (release, wait) = oneshot::channel();
            let relay_local = local.clone();
            room.relays.lock().unwrap().push(tokio::spawn(async move {
                wait.await.unwrap();
                relay_local.publish(
                    &LogEvent::new("dst.test.last_health", json!({"finished":true}), "INFO"),
                    StreamKind::Lifecycle,
                );
            }));
            if drop_room {
                drop(room);
                assert!(weak.upgrade().is_none());
                release.send(()).unwrap();
                timeout(Duration::from_secs(2), async {
                    while !local.output.stats().closed {
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
            } else {
                let stopped = room.while_stopped().await.unwrap();
                assert_eq!(
                    room.close_telemetry().await.unwrap_err().code,
                    ErrorCode::Busy
                );
                drop(stopped);
                let mut closing = Box::pin(room.close_telemetry());
                assert!(futures::poll!(&mut closing).is_pending());
                drop(closing);
                assert!(
                    timeout(Duration::from_millis(20), room.close_telemetry())
                        .await
                        .is_err()
                );
                assert!(!local.output.stats().closed);
                release.send(()).unwrap();
                let report = room.close_telemetry().await.unwrap();
                assert_eq!(report["relays_drained"], true);
                assert_eq!(report["output_drained"], true);
                assert_eq!(report["stats"]["local_output"]["written"], 1);
            }
            assert_eq!(records.drain().len(), 1);
            assert!(local.output.close().await);
        }
    }

    #[tokio::test]
    async fn telemetry_shutdown_reports_failed_relays_and_allows_counted_drops() {
        for failed_relay in [true, false] {
            let (_directory, room) = stopped_room();
            room.telemetry
                .losses
                .relay_dropped
                .fetch_add(3, Ordering::Relaxed);
            if failed_relay {
                let relay = tokio::spawn(std::future::pending::<()>());
                relay.abort();
                room.relays.lock().unwrap().push(relay);
            }

            let result = room.close_telemetry().await;
            let report = if failed_relay {
                let error = result.unwrap_err();
                assert_eq!(error.code, ErrorCode::Internal);
                assert_eq!(error.details["relays_drained"], false);
                assert_eq!(error.details["relay_errors"].as_array().unwrap().len(), 1);
                error.details
            } else {
                let report = result.unwrap();
                assert_eq!(report["relays_drained"], true);
                report
            };
            assert_eq!(report["output_drained"], true);
            assert_eq!(report["stats"]["local_output"]["closed"], true);
            assert_eq!(report["stats"]["streams"]["relay_dropped"], 3);
            assert!(report["export_error"].is_null());
            assert!(report["observability_error"].is_null());
        }
    }
}
