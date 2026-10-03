//! One room owns every configured shard and each accepted operation until completion.

use crate::{
    configuration::{self, Cluster},
    driver::{Driver, DriverError, DriverErrorCode, DriverEventKind, DriverOptions, DriverState},
    files::RoomLock,
    model::*,
    rpc::EventHub,
    telemetry::{LogEvent, LogExporter, StreamLossCounters},
};
use futures::future::join_all;
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore, oneshot, watch},
    time::{Instant, sleep, timeout},
};

const COMMAND: Duration = Duration::from_secs(120);

pub struct StoppedOperation {
    room: Arc<Room>,
    _permit: OwnedSemaphorePermit,
}

impl StoppedOperation {
    pub fn directory(&self) -> &Path {
        &self.room.directory
    }
    pub fn executable(&self) -> &Path {
        &self.room.executable
    }
    pub fn with_lock<T>(
        &self,
        callback: impl FnOnce(&mut RoomLock) -> anyhow::Result<T>,
    ) -> Result<T> {
        self.room.with_lock(callback)
    }
}

pub struct Room {
    directory: PathBuf,
    cluster_name: String,
    executable: PathBuf,
    runtime: Mutex<Option<tokio::runtime::Handle>>,
    lock: Mutex<Option<RoomLock>>,
    cluster: Mutex<Cluster>,
    options: Mutex<DriverOptions>,
    ports: Mutex<BTreeMap<String, u16>>,
    drivers: Mutex<BTreeMap<String, Driver>>,
    status: watch::Sender<RoomStatus>,
    mutation: Arc<Semaphore>,
    requests: Arc<Semaphore>,
    interrupted: watch::Sender<u64>,
    restart_required: AtomicBool,
    shutdown_pending: AtomicU64,
    resource: Mutex<serde_json::Map<String, Value>>,
    events: EventHub,
    exporter: Mutex<Option<Arc<LogExporter>>>,
    observability: Mutex<Option<Arc<crate::observability::Observability>>>,
    output: LocalOutput,
    losses: Arc<StreamLossCounters>,
    relays: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}

impl Room {
    pub fn open(
        directory: impl AsRef<Path>,
        executable: impl AsRef<Path>,
        options: DriverOptions,
        events: EventHub,
    ) -> Result<Arc<Self>> {
        let mut lock = RoomLock::try_acquire(directory.as_ref()).map_err(internal)?;
        lock.while_stopped().recover().map_err(internal)?;
        crate::archive::clear_artifacts(directory.as_ref(), true).map_err(internal)?;
        let cluster = configuration::discover(directory.as_ref()).map_err(internal)?;
        let initial = RoomStatus {
            phase: Phase::Stopped,
            master: cluster.master().name.clone(),
            shards: cluster
                .shards
                .iter()
                .map(|shard| ShardStatus {
                    name: shard.name.clone(),
                    is_master: shard.master,
                    phase: Phase::Stopped,
                    pid: None,
                    identity: None,
                    readiness: Readiness::default(),
                    error: None,
                })
                .collect(),
            operations: OperationHistory::default(),
            error: None,
        };
        let (status, _) = watch::channel(initial);
        let (interrupted, _) = watch::channel(0);
        Ok(Arc::new(Self {
            cluster_name: std::env::var("DST_SERVER_CLUSTER_NAME")
                .ok()
                .filter(|name| !name.is_empty())
                .unwrap_or_else(|| {
                    cluster
                        .directory
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into_owned()
                }),
            directory: cluster.directory.clone(),
            executable: executable.as_ref().to_owned(),
            runtime: Mutex::new(tokio::runtime::Handle::try_current().ok()),
            lock: Mutex::new(Some(lock)),
            cluster: Mutex::new(cluster),
            options: Mutex::new(options),
            ports: Mutex::new(BTreeMap::new()),
            drivers: Mutex::new(BTreeMap::new()),
            status,
            mutation: Arc::new(Semaphore::new(1)),
            requests: Arc::new(Semaphore::new(64)),
            interrupted,
            restart_required: AtomicBool::new(false),
            shutdown_pending: AtomicU64::new(0),
            resource: Mutex::new(serde_json::Map::new()),
            events,
            exporter: Mutex::new(None),
            observability: Mutex::new(None),
            output: LocalOutput::new(),
            losses: Arc::new(StreamLossCounters::default()),
            relays: Mutex::new(Vec::new()),
        }))
    }

    pub fn set_telemetry_resource(&self, resource: serde_json::Map<String, Value>) {
        *self.resource.lock().unwrap() = resource;
    }
    pub fn set_observability(
        &self,
        observability: Option<Arc<crate::observability::Observability>>,
    ) {
        *self.observability.lock().unwrap() = observability;
    }
    pub fn set_log_exporter(&self, exporter: Option<Arc<LogExporter>>) {
        *self.exporter.lock().unwrap() = exporter;
    }
    pub fn telemetry_stats(&self) -> Value {
        let process: BTreeMap<_, _> = self
            .drivers()
            .into_iter()
            .map(|(name, driver)| (name, driver.output_stats()))
            .collect();
        json!({"streams":self.losses.snapshot(),"local_output":self.output.stats(),"processes":process,"exporter":self.exporter.lock().unwrap().as_ref().map(|exporter|exporter.stats())})
    }
    pub async fn close_telemetry(&self) -> Result<Value> {
        let relays = std::mem::take(&mut *self.relays.lock().unwrap());
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
        let relays_drained = relay_errors.is_empty();
        let output_drained = self.output.close().await;
        let exporter = self.exporter.lock().unwrap().clone();
        let observability = self.observability.lock().unwrap().clone();
        let (export_error, observability_error) = tokio::join!(
            async move {
                if let Some(exporter) = exporter {
                    exporter.close().await.err().map(|error| error.to_string())
                } else {
                    None
                }
            },
            async move {
                if let Some(observability) = observability {
                    observability
                        .close()
                        .await
                        .err()
                        .map(|error| error.to_string())
                } else {
                    None
                }
            }
        );
        let failed = !relays_drained
            || !output_drained
            || export_error.is_some()
            || observability_error.is_some();
        let report = json!({"relays_drained":relays_drained,"relay_errors":relay_errors,"output_drained":output_drained,"export_error":export_error,"observability_error":observability_error,"stats":self.telemetry_stats()});
        if failed {
            Err(
                Error::new(ErrorCode::Internal, "telemetry shutdown did not complete")
                    .with_details(report),
            )
        } else {
            Ok(report)
        }
    }
    pub async fn while_stopped(self: &Arc<Self>) -> Result<StoppedOperation> {
        let permit = self
            .mutation
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::new(ErrorCode::Busy, "another room operation is running"))?;
        self.require_stopped()?;
        for driver in self.drivers().values() {
            if !driver.wait().await.map_err(driver_error)?.output_drained {
                return Err(Error::new(
                    ErrorCode::NotReady,
                    "previous game output did not drain",
                ));
            }
        }
        Ok(StoppedOperation {
            room: self.clone(),
            _permit: permit,
        })
    }
    pub fn driver_states(&self) -> BTreeMap<String, DriverState> {
        self.drivers()
            .into_iter()
            .map(|(name, driver)| (name, driver.snapshot()))
            .collect()
    }
    pub async fn force_fault(self: &Arc<Self>, error: Error) -> Result<StopResult> {
        self.interrupt();
        let permit = self
            .mutation
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| Error::new(ErrorCode::Internal, "room operation queue closed"))?;
        let room = self.clone();
        let (send, receive) = oneshot::channel();
        tokio::spawn(async move {
            let _permit = permit;
            room.restart_required.store(true, Ordering::Release);
            let result = room.stop_inner(false).await;
            room.set_phase(Phase::Failed, Some(error));
            drop(_permit);
            let _ = send.send(result);
        });
        receive
            .await
            .map_err(|_| Error::new(ErrorCode::Internal, "fault cleanup worker exited"))?
    }
    pub fn status(&self) -> RoomStatus {
        self.status.borrow().clone()
    }
    pub fn watch(&self) -> watch::Receiver<RoomStatus> {
        self.status.subscribe()
    }
    pub fn requires_container_restart(&self) -> bool {
        self.restart_required.load(Ordering::Acquire)
    }
    pub fn with_lock<T>(
        &self,
        callback: impl FnOnce(&mut RoomLock) -> anyhow::Result<T>,
    ) -> Result<T> {
        let mut lock = self.lock.lock().unwrap();
        callback(
            lock.as_mut()
                .expect("room lock is held while the room exists"),
        )
        .map_err(internal)
    }
    pub fn set_external_ports(&self, ports: BTreeMap<String, u16>) -> Result<()> {
        self.require_stopped()?;
        let names: BTreeSet<_> = self
            .cluster
            .lock()
            .unwrap()
            .shards
            .iter()
            .map(|shard| shard.name.clone())
            .collect();
        if ports.keys().cloned().collect::<BTreeSet<_>>() != names
            || ports.values().any(|port| *port == 0)
        {
            return Err(Error::invalid(
                "external_ports",
                "published ports must identify every configured shard",
            ));
        }
        *self.ports.lock().unwrap() = ports;
        Ok(())
    }
    pub fn configure_driver_control(&self, control: Value) -> Result<()> {
        self.require_stopped()?;
        self.options.lock().unwrap().control = control;
        Ok(())
    }
    pub fn require_stopped(&self) -> Result<()> {
        if self
            .drivers
            .lock()
            .unwrap()
            .values()
            .any(|driver| driver.snapshot().running)
        {
            Err(Error::new(
                ErrorCode::Busy,
                "all game processes must be stopped",
            ))
        } else if self
            .drivers
            .lock()
            .unwrap()
            .values()
            .any(|driver| !driver.snapshot().output_drained)
        {
            Err(Error::new(
                ErrorCode::NotReady,
                "previous game output did not drain",
            ))
        } else {
            Ok(())
        }
    }
    fn drivers(&self) -> BTreeMap<String, Driver> {
        self.drivers.lock().unwrap().clone()
    }
    fn driver(&self, name: &str) -> Result<Driver> {
        self.drivers
            .lock()
            .unwrap()
            .get(name)
            .cloned()
            .ok_or_else(|| {
                Error::new(ErrorCode::NotReady, "shard process is stopped")
                    .with_details(json!({"shard":name}))
            })
    }
    fn master(&self) -> String {
        self.cluster.lock().unwrap().master().name.clone()
    }
    fn set_phase(&self, phase: Phase, error: Option<Error>) {
        self.status.send_modify(|status| {
            status.phase = phase;
            status.error = error.clone();
            for shard in &mut status.shards {
                shard.phase = phase;
            }
        });
        self.publish_status();
    }
    fn publish_status(&self) {
        self.events.publish("lifecycle", &json!({"event":"room_status", "status":self.status(), "observed_timestamp_ns":timestamp()}));
    }
    fn refresh_shard(&self, state: &DriverState) {
        if let Some(observer) = self.observability.lock().unwrap().as_ref() {
            observer.observe_state(state, &self.cluster_name);
        }
        self.status.send_modify(|status| {
            if let Some(shard) = status
                .shards
                .iter_mut()
                .find(|shard| shard.name == state.shard)
            {
                shard.pid = state.running.then_some(state.pid);
                shard.identity = identity(state).ok();
                shard.readiness.process_running = state.running;
                shard.readiness.world_loaded = state.native_ready && state.session_id.is_some();
                shard.readiness.control_ready = state.ready;
                shard.error = state
                    .failure
                    .as_ref()
                    .map(|failure| Error::new(ErrorCode::Transport, failure));
                if !state.running {
                    shard.phase = if state.failure.is_some() {
                        Phase::Failed
                    } else {
                        Phase::Stopped
                    };
                }
            }
        });
    }
    fn interrupt(&self) {
        self.interrupted
            .send_modify(|revision| *revision = revision.wrapping_add(1));
    }

    /// A timeout or dropped caller only stops waiting; the accepted mutation remains owned here.
    pub async fn invoke(self: &Arc<Self>, envelope: Envelope) -> Result<Value> {
        self.invoke_inner(envelope, false).await
    }

    /// The Agent already owns the caller's wait and retains this result through cleanup.
    pub(crate) async fn invoke_until_complete(
        self: &Arc<Self>,
        envelope: Envelope,
    ) -> Result<Value> {
        self.invoke_inner(envelope, true).await
    }

    async fn invoke_inner(
        self: &Arc<Self>,
        envelope: Envelope,
        wait_for_completion: bool,
    ) -> Result<Value> {
        envelope.validate()?;
        if let Target::Shard(name) = &envelope.target
            && !self
                .cluster
                .lock()
                .unwrap()
                .shards
                .iter()
                .any(|shard| &shard.name == name)
        {
            return Err(Error::new(ErrorCode::NotFound, "unknown shard"));
        }
        let wait = envelope.timeout()?;
        let deadline = envelope.request.completion_timeout()?;
        if matches!(envelope.request, Request::ReleaseArchive { .. }) {
            return self.dispatch(&envelope.target, &envelope.request).await;
        }
        if self.shutdown_pending.load(Ordering::Acquire) > 0
            && envelope.request.mutating()
            && !matches!(envelope.request, Request::Kill {})
        {
            return Err(Error::new(ErrorCode::Busy, "room shutdown is in progress"));
        }
        if matches!(envelope.request, Request::Status {}) {
            return self.dispatch(&envelope.target, &envelope.request).await;
        }
        let request_slot = self
            .requests
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::new(ErrorCode::Busy, "room request limit reached"))?;
        if !envelope.request.mutating() {
            return timeout(wait, self.dispatch(&envelope.target, &envelope.request))
                .await
                .map_err(|_| Error::new(ErrorCode::Timeout, "query wait timed out"))?;
        }
        let killing = matches!(envelope.request, Request::Kill {});
        if killing {
            self.interrupt();
            for driver in self.drivers().values() {
                driver.request_kill();
            }
        }
        let permit = if killing {
            None
        } else {
            Some(self.mutation.clone().try_acquire_owned().map_err(|_| {
                Error::new(ErrorCode::Busy, "another room mutation is running")
                    .with_details(json!({"operation": self.status().operations.current}))
            })?)
        };
        let operation = OperationStatus {
            id: ulid::Ulid::new().to_string(),
            method: envelope.request.method().into(),
            target: envelope.target.clone(),
            started_at_ns: timestamp(),
            completed_at_ns: None,
            result: None,
        };
        let operation_id = operation.id.clone();
        let (reply, receive) = oneshot::channel();
        let room = self.clone();
        let mut interrupted = self.interrupted.subscribe();
        let interruption_revision = *interrupted.borrow();
        tokio::spawn(async move {
            let operation_permit = match permit {
                Some(permit) => permit,
                None => match room.mutation.clone().acquire_owned().await {
                    Ok(permit) => permit,
                    Err(_) => {
                        let _ = reply.send(Err(Error::new(
                            ErrorCode::Internal,
                            "room mutation queue closed",
                        )));
                        return;
                    }
                },
            };
            room.status
                .send_modify(|state| state.operations.current = Some(operation));
            let shard = match &envelope.target {
                Target::Shard(name) => Some(name.as_str()),
                Target::Room => None,
            };
            let session = room
                .drivers()
                .get(shard.unwrap_or(&room.master()))
                .and_then(|driver| driver.snapshot().session_id);
            let observation = room.observability.lock().unwrap().as_ref().map(|observer| {
                observer.begin_operation(
                    &room.cluster_name,
                    shard,
                    envelope.request.method(),
                    session.as_deref(),
                )
            });
            let mut result = if let Request::UpdateMods { notice, restart } = &envelope.request {
                room.update_mods(notice, *restart, interrupted.clone())
                    .await
            } else if matches!(envelope.request, Request::ExportArchive { .. }) {
                room.dispatch(&envelope.target, &envelope.request).await
            } else {
                tokio::select! {
                    result=timeout(deadline,room.dispatch(&envelope.target,&envelope.request))=>result.unwrap_or_else(|_|Err(Error::new(ErrorCode::Unknown,"operation deadline expired; the world outcome may be unknown"))),
                    _=interrupted.changed(),if !killing=>Err(Error::new(ErrorCode::Unknown,"operation interrupted by room shutdown")),
                }
            };
            let phase = room.status().phase;
            if result.is_err() && matches!(phase, Phase::Starting | Phase::Stopping) {
                let failed_start =
                    phase == Phase::Starting && *interrupted.borrow() == interruption_revision;
                if failed_start {
                    room.restart_required.store(true, Ordering::Release);
                }
                let cleanup = room.stop_inner(false).await;
                if let Err(error) = &mut result {
                    error.details = json!({"cause":error.details,"cleanup":Outcome::from(cleanup)});
                }
                if failed_start {
                    room.set_phase(Phase::Failed, result.as_ref().err().cloned());
                }
            }
            if let Some(observation) = observation {
                observation.finish(result.as_ref().err());
            }
            room.status.send_modify(|state| {
                if let Some(mut operation) = state.operations.current.take() {
                    operation.completed_at_ns = Some(timestamp());
                    operation.result = Some(result.clone().into());
                    state.operations.last = Some(operation);
                }
            });
            room.publish_status();
            drop(operation_permit);
            drop(request_slot);
            let _ = reply.send(result);
        });
        let completed = if wait_for_completion {
            receive.await
        } else {
            timeout(wait, receive).await.map_err(|_| {
                Error::new(
                    ErrorCode::Timeout,
                    "caller wait timed out; the operation is still owned by the room",
                )
                .with_details(json!({"operation_id":operation_id}))
            })?
        };
        completed.map_err(|_| Error::new(ErrorCode::Internal, "operation worker exited"))?
    }

    pub async fn shutdown_priority(self: &Arc<Self>) -> Result<StopResult> {
        self.shutdown_pending.fetch_add(1, Ordering::AcqRel);
        self.set_phase(Phase::Stopping, None);
        self.interrupt();
        let permit = self.mutation.clone().try_acquire_owned().ok();
        let room = self.clone();
        let (reply, receive) = oneshot::channel();
        tokio::spawn(async move {
            let permit = match permit {
                Some(permit) => Ok(permit),
                None => room.mutation.clone().acquire_owned().await,
            };
            let result = if let Ok(permit) = permit {
                let result = room.stop_inner(false).await;
                drop(permit);
                result
            } else {
                Err(Error::new(
                    ErrorCode::Internal,
                    "room operation queue closed",
                ))
            };
            room.shutdown_pending.fetch_sub(1, Ordering::AcqRel);
            let _ = reply.send(result);
        });
        receive
            .await
            .map_err(|_| Error::new(ErrorCode::Internal, "priority shutdown worker exited"))?
    }
    pub async fn shutdown(self: &Arc<Self>) -> Result<StopResult> {
        let value = self
            .invoke(Envelope::new(Target::Room, Request::Stop { notice: None })?)
            .await?;
        serde_json::from_value(value).map_err(internal)
    }
    pub async fn kill(self: &Arc<Self>) -> Result<StopResult> {
        let value = self
            .invoke(Envelope::new(Target::Room, Request::Kill {})?)
            .await?;
        serde_json::from_value(value).map_err(internal)
    }
    pub async fn refresh_health(self: &Arc<Self>) -> Result<Value> {
        let health = self.all("health", json!({})).await?;
        let healthy = health.as_array().is_some_and(|shards| {
            !shards.is_empty()
                && shards.iter().all(|shard| {
                    shard["result"]["status"] == "success"
                        && shard["result"]["value"]["protocol"] == 3
                        && shard["result"]["value"]["capabilities"]["players"] == "active"
                })
        });
        if !healthy {
            return Err(
                Error::new(ErrorCode::NotReady, "required shard control is unhealthy")
                    .with_details(health),
            );
        }
        self.wait_ready(Duration::from_secs(4)).await?;
        Ok(health)
    }

    fn monitor(self: &Arc<Self>, name: String, driver: &Driver) {
        let mut subscription = driver.subscribe();
        let events = self.events.clone();
        let exporter = self.exporter.lock().unwrap().clone();
        let observer = self.observability.lock().unwrap().clone();
        let resource = self.resource.lock().unwrap().clone();
        let output = self.output.clone();
        let losses = self.losses.clone();
        let cluster = self.cluster_name.clone();
        if let Some(observer) = &observer {
            observer.observe_state(&driver.snapshot(), &cluster);
        }
        let relay = tokio::spawn(async move {
            let mut dropped = 0;
            while let Some(event) = subscription.recv().await {
                let new_drops = subscription.dropped();
                losses.record_subscription_drop(new_drops.saturating_sub(dropped));
                if let Some(observer) = &observer {
                    observer.record_subscription_drop(
                        &cluster,
                        &event.shard,
                        new_drops.saturating_sub(dropped),
                    );
                    observer.observe_driver(&event, &cluster);
                }
                dropped = new_drops;
                let kind = match event.kind {
                    DriverEventKind::Log => "logs",
                    DriverEventKind::Lifecycle | DriverEventKind::Control => "lifecycle",
                    _ => "events",
                };
                if let Ok(log) = LogEvent::from_driver(&event, &cluster) {
                    let mut record = serde_json::to_value(&log).unwrap_or(Value::Null);
                    record["resource"] = json!(resource);
                    events.publish(kind, &record);
                    if !output.submit(&record) {
                        losses.record_log_drop(1);
                    }
                    if let Some(exporter) = &exporter {
                        let _ = exporter.submit(&log);
                    }
                } else {
                    losses.record_log_drop(1);
                }
            }
            losses.record_subscription_drop(subscription.dropped().saturating_sub(dropped));
        });
        {
            let mut relays = self.relays.lock().unwrap();
            relays.retain(|relay| !relay.is_finished());
            relays.push(relay);
        }
        let mut states = driver.watch();
        let weak = Arc::downgrade(self);
        let nonce = driver.snapshot().nonce;
        tokio::spawn(async move {
            let mut source_gaps = 0;
            loop {
                let state = states.borrow_and_update().clone();
                let Some(room) = weak.upgrade() else {
                    break;
                };
                if room
                    .drivers()
                    .get(&name)
                    .is_none_or(|current| current.snapshot().nonce != nonce)
                {
                    break;
                }
                room.losses
                    .record_source_gap(state.telemetry_gaps.saturating_sub(source_gaps));
                source_gaps = state.telemetry_gaps;
                room.refresh_shard(&state);
                if !state.running {
                    if matches!(room.status().phase, Phase::Starting | Phase::Running) {
                        let error = Error::new(
                            ErrorCode::Transport,
                            state
                                .failure
                                .clone()
                                .unwrap_or_else(|| "configured shard exited unexpectedly".into()),
                        )
                        .with_details(json!({"shard":name}));
                        room.restart_required.store(true, Ordering::Release);
                        room.interrupt();
                        room.set_phase(Phase::Failed, Some(error.clone()));
                        let cleanup = room.clone();
                        tokio::spawn(async move {
                            let Ok(_permit) = cleanup.mutation.clone().acquire_owned().await else {
                                return;
                            };
                            let _ = cleanup.stop_inner(false).await;
                            cleanup.set_phase(Phase::Failed, Some(error));
                        });
                    }
                    break;
                }
                drop(room);
                if states.changed().await.is_err() {
                    break;
                }
            }
        });
    }

    async fn start_inner(self: &Arc<Self>) -> Result<Value> {
        *self.runtime.lock().unwrap() = Some(tokio::runtime::Handle::current());
        if self.status().phase == Phase::Running {
            return value(self.status());
        }
        self.require_stopped()?;
        if self.requires_container_restart() {
            return Err(Error::new(
                ErrorCode::NotReady,
                "the room container must be recreated after a forced or abnormal stop",
            ));
        }
        self.set_phase(Phase::Preparing, None);
        self.with_lock(|lock| {
            crate::mods::PreparedMods::prepare(lock, &self.executable, None)?;
            Ok(())
        })?;
        let cluster = configuration::discover(&self.directory).map_err(internal)?;
        *self.cluster.lock().unwrap() = cluster.clone();
        self.status.send_modify(|status| {
            status.master = cluster.master().name.clone();
            status.shards = cluster
                .shards
                .iter()
                .map(|shard| ShardStatus {
                    name: shard.name.clone(),
                    is_master: shard.master,
                    phase: Phase::Starting,
                    pid: None,
                    identity: None,
                    readiness: Readiness::default(),
                    error: None,
                })
                .collect();
        });
        self.drivers.lock().unwrap().clear();
        self.set_phase(Phase::Starting, None);
        for shard in &cluster.shards {
            let mut options = self.options.lock().unwrap().clone();
            if let Some(port) = self.ports.lock().unwrap().get(&shard.name) {
                options
                    .extra_args
                    .extend(["-external_port".into(), port.to_string().into()]);
            }
            let driver = match Driver::spawn(&cluster, &self.executable, &shard.name, options) {
                Ok(driver) => driver,
                Err(error) => {
                    let error = internal(error);
                    self.set_phase(Phase::Failed, Some(error.clone()));
                    let _ = self.stop_inner(false).await;
                    self.set_phase(Phase::Failed, Some(error.clone()));
                    return Err(error);
                }
            };
            self.drivers
                .lock()
                .unwrap()
                .insert(shard.name.clone(), driver.clone());
            self.monitor(shard.name.clone(), &driver);
        }
        if let Err(error) = self
            .wait_ready(Duration::from_secs_f64(START_TIMEOUT))
            .await
        {
            self.restart_required.store(true, Ordering::Release);
            let _ = self.stop_inner(false).await;
            self.set_phase(Phase::Failed, Some(error.clone()));
            return Err(error);
        }
        self.set_phase(Phase::Running, None);
        value(self.status())
    }

    async fn wait_ready(&self, duration: Duration) -> Result<()> {
        let deadline = Instant::now() + duration;
        let drivers = self.drivers();
        for driver in drivers.values() {
            driver
                .wait_ready(deadline.saturating_duration_since(Instant::now()))
                .await
                .map_err(driver_error)?;
        }
        loop {
            let mut ids = BTreeMap::new();
            for (name, driver) in &drivers {
                let runtime = driver
                    .request(
                        "runtime",
                        json!({}),
                        COMMAND.min(deadline.saturating_duration_since(Instant::now())),
                    )
                    .await
                    .map_err(driver_error)?;
                let id = runtime["shard_id"].as_str().ok_or_else(|| {
                    Error::new(ErrorCode::Protocol, "runtime has no shard identity")
                })?;
                if ids.insert(id.to_owned(), name.clone()).is_some() {
                    return Err(Error::new(
                        ErrorCode::Conflict,
                        "shards report duplicate native identities",
                    ));
                }
            }
            let mut connected = true;
            for (name, driver) in &drivers {
                let peers = driver
                    .request(
                        "connected_shards",
                        json!({"current_name":name}),
                        COMMAND.min(deadline.saturating_duration_since(Instant::now())),
                    )
                    .await
                    .map_err(driver_error)?;
                let peers = peers.as_array().ok_or_else(|| {
                    Error::new(ErrorCode::Protocol, "invalid native shard connections")
                })?;
                let missing: Vec<_> = ids
                    .iter()
                    .filter(|(id, _)| {
                        !peers.iter().any(|peer| {
                            peer["id"].as_str() == Some(id.as_str()) && peer["ready"] == true
                        })
                    })
                    .map(|(_, name)| name.clone())
                    .collect();
                connected &= missing.is_empty();
                self.refresh_shard(&driver.snapshot());
                self.status.send_modify(|status| {
                    if let Some(shard) = status.shards.iter_mut().find(|shard| &shard.name == name)
                    {
                        shard.readiness.missing_shards = missing.clone();
                    }
                });
            }
            if connected {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    ErrorCode::Timeout,
                    "configured shard connections did not become ready",
                ));
            }
            sleep(Duration::from_millis(250)).await;
        }
    }

    async fn stop_inner(&self, force: bool) -> Result<StopResult> {
        self.set_phase(Phase::Stopping, None);
        let drivers = self.drivers();
        let stopped = join_all(drivers.into_iter().map(|(name, driver)| async move {
            let before = driver.snapshot();
            let report = if force {
                driver.kill().await
            } else {
                driver.stop(Duration::from_secs_f64(STOP_TIMEOUT)).await
            };
            let after = driver.snapshot();
            // Retain the original boundary when cleanup resumes the same shutdown worker.
            let before = driver.stop_snapshot().unwrap_or(before);
            let stopped = ShardStopped {
                returncode: report
                    .as_ref()
                    .map_or(after.returncode, |report| report.returncode()),
                forced: force || report.as_ref().map_or(after.forced, |report| report.forced),
                output_drained: report
                    .as_ref()
                    .map_or(after.output_drained, |report| report.output_drained),
                saved_snapshot: saved_after(&after, &before, last_save_id(&before), None, true),
            };
            let result = report.map_err(driver_error).and_then(|report| {
                if !report.output_drained {
                    return Err(Error::new(
                        ErrorCode::Transport,
                        "game output did not drain",
                    ));
                }
                if !force {
                    if let Some(error) = report.protocol_error.or(after.failure.clone()) {
                        return Err(Error::new(ErrorCode::Protocol, error));
                    }
                    if !report.status.success() || report.forced {
                        return Err(Error::new(
                            ErrorCode::Transport,
                            "game process did not exit cleanly",
                        ));
                    }
                }
                Ok(stopped.clone())
            });
            if stopped.forced || result.is_err() {
                self.restart_required.store(true, Ordering::Release);
            }
            let result = result.map_err(|mut error| {
                error.details = json!({"stopped":stopped,"cause":error.details});
                error
            });
            self.refresh_shard(&after);
            ShardResult {
                shard: name,
                result: result.into(),
            }
        }))
        .await;
        let failure = stopped
            .iter()
            .any(|result| matches!(result.result, Outcome::Failure { .. }));
        let result = StopResult { shards: stopped };
        if failure {
            let error = Error::new(
                ErrorCode::PartialFailure,
                "some shard processes did not stop cleanly",
            )
            .with_details(value(&result)?);
            self.set_phase(Phase::Failed, Some(error.clone()));
            Err(error)
        } else {
            self.set_phase(Phase::Stopped, None);
            Ok(result)
        }
    }
    async fn save(&self) -> Result<Value> {
        self.require_ready()?;
        let drivers = self.drivers();
        let mut expected = BTreeMap::new();
        for (name, driver) in &drivers {
            let runtime = driver
                .request("runtime", json!({}), COMMAND)
                .await
                .map_err(driver_error)?;
            expected.insert(
                name.clone(),
                (
                    driver.snapshot(),
                    runtime["snapshot"].as_u64().ok_or_else(|| {
                        Error::new(ErrorCode::Protocol, "runtime snapshot is missing")
                    })?,
                ),
            );
        }
        let master = self.master();
        let (master_before, target) = &expected[&master];
        let target = *target;
        if let Some((shard, (_, snapshot))) = expected
            .iter()
            .find(|(_, (_, snapshot))| *snapshot > target)
        {
            return Err(Error::new(
                ErrorCode::Conflict,
                "a secondary snapshot is ahead of the master; saving could roll it backward",
            )
            .with_details(
                json!({"shard":shard,"snapshot_id":snapshot,"master_snapshot_id":target}),
            ));
        }
        self.driver(&master)?
            .request(
                "save",
                json!({"session_id":master_before.session_id,"snapshot_id":target}),
                COMMAND,
            )
            .await
            .map_err(driver_error)?;
        let results = join_all(drivers.into_iter().map(|(name, driver)| {
            let (before, _) = expected[&name].clone();
            async move {
                let baseline = last_save_id(&before);
                let mut watch = driver.watch();
                let result = timeout(Duration::from_secs_f64(SAVE_TIMEOUT), async {
                    loop {
                        let current = watch.borrow_and_update().clone();
                        if let Some(saved) =
                            saved_after(&current, &before, baseline, Some(target), false)
                        {
                            return Ok(saved);
                        }
                        if !current.running
                            || current.generation != before.generation
                            || current.nonce != before.nonce
                        {
                            return Err(Error::new(
                                ErrorCode::Unknown,
                                "world exited or changed before the save callback",
                            ));
                        }
                        if current.control_records.iter().any(|record| {
                            record["save_id"].as_u64().is_some_and(|id| id > baseline)
                                && record["snapshot_id"] == target
                                && matches!(
                                    record["event"].as_str(),
                                    Some("save_failed" | "save_unconfirmed")
                                )
                        }) {
                            return Err(Error::new(
                                ErrorCode::Unknown,
                                "native save callback did not confirm the target snapshot",
                            ));
                        }
                        watch.changed().await.map_err(|_| {
                            Error::new(ErrorCode::Transport, "save observer closed")
                        })?;
                    }
                })
                .await
                .unwrap_or_else(|_| {
                    Err(Error::new(
                        ErrorCode::Unknown,
                        "save callback deadline expired",
                    ))
                });
                ShardResult {
                    shard: name,
                    result: result.into(),
                }
            }
        }))
        .await;
        let result = SaveResult { shards: results };
        if result
            .shards
            .iter()
            .any(|shard| matches!(shard.result, Outcome::Failure { .. }))
        {
            Err(Error::new(
                ErrorCode::PartialFailure,
                "not every shard confirmed its save callback",
            )
            .with_details(value(&result)?))
        } else {
            value(result)
        }
    }

    fn require_ready(&self) -> Result<()> {
        let status = self.status();
        if status.phase != Phase::Running {
            return Err(Error::new(ErrorCode::NotReady, "room is not running"));
        }
        for shard in &status.shards {
            shard.readiness.require(&shard.name)?;
        }
        Ok(())
    }
    async fn native(&self, shard: &str, method: &str, args: Value) -> Result<Value> {
        self.driver(shard)?
            .request(method, native_arguments(args), COMMAND)
            .await
            .map_err(driver_error)
    }
    async fn execute(&self, shard: &str, source: &str) -> Result<Value> {
        let evaluated = self
            .native(shard, "evaluate", json!({"source":source}))
            .await?;
        let output = evaluated["output"].as_str().unwrap_or("");
        let error = evaluated["error"]["message"].as_str().unwrap_or("");
        Ok(json!(
            [output, error]
                .into_iter()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join("\n")
        ))
    }
    async fn all(&self, method: &str, args: Value) -> Result<Value> {
        let names: Vec<_> = self
            .cluster
            .lock()
            .unwrap()
            .shards
            .iter()
            .map(|shard| shard.name.clone())
            .collect();
        let results = join_all(names.into_iter().map(|name| {
            let args = args.clone();
            async move {
                ShardResult {
                    result: self.native(&name, method, args).await.into(),
                    shard: name,
                }
            }
        }))
        .await;
        value(results)
    }
    async fn update_mods(
        self: &Arc<Self>,
        notice: &Option<Countdown>,
        restart: bool,
        mut cancelled: watch::Receiver<u64>,
    ) -> Result<Value> {
        tokio::select! { result=self.countdown(notice)=>result?, _=cancelled.changed()=>return Err(Error::new(ErrorCode::Unknown,"Mod maintenance interrupted before stopping")) };
        self.stop_inner(false).await?;
        if cancelled.has_changed().unwrap_or(true) {
            return Err(Error::new(
                ErrorCode::Unknown,
                "Mod maintenance interrupted after stopping",
            ));
        }
        if self.requires_container_restart() {
            return Err(Error::new(
                ErrorCode::NotReady,
                "the room container must be recreated before Mod maintenance",
            ));
        }
        let proxy = std::env::var("DST_SERVER_MOD_PROXY")
            .ok()
            .filter(|proxy| !proxy.is_empty());
        let prepared = self.with_lock(|lock| {
            Ok(crate::mods::PreparedMods::prepare(
                lock,
                &self.executable,
                proxy,
            )?)
        })?;
        let result = prepared
            .update_cancellable(&self.events, cancelled.clone())
            .await?;
        if cancelled.has_changed().unwrap_or(true) {
            return Err(Error::new(
                ErrorCode::Unknown,
                "Mod maintenance interrupted before restarting",
            ));
        }
        if restart {
            tokio::select! { started=self.start_inner()=>{started?;},_=cancelled.changed()=>return Err(Error::new(ErrorCode::Unknown,"Mod maintenance interrupted while restarting")) }
        }
        Ok(result)
    }
    async fn countdown(&self, notice: &Option<Countdown>) -> Result<()> {
        let Some(notice) = notice else {
            return Ok(());
        };
        if self
            .drivers()
            .values()
            .all(|driver| !driver.snapshot().ready)
        {
            return Ok(());
        }
        let deadline = Instant::now() + Duration::from_secs_f64(notice.delay);
        loop {
            let remaining = deadline
                .saturating_duration_since(Instant::now())
                .as_secs_f64()
                .ceil() as u64;
            let mut parameters = notice.parameters.clone();
            parameters.insert("remaining".into(), json!(remaining));
            parameters.insert("minutes".into(), json!(remaining.div_ceil(60)));
            parameters.insert(
                "when".into(),
                json!(if remaining == 0 {
                    "现在".to_owned()
                } else {
                    format!("将在 {remaining} 秒后")
                }),
            );
            self.native(&self.master(), "announce", json!({"message":render_notice(&notice.template, &parameters), "count":1, "interval":notice.interval})).await?;
            if remaining == 0 {
                return Ok(());
            }
            sleep(
                Duration::from_secs_f64(notice.interval)
                    .min(deadline.saturating_duration_since(Instant::now())),
            )
            .await;
        }
    }

    async fn announce(&self, message: &str, count: u64, interval: f64) -> Result<Value> {
        let interval = Duration::from_secs_f64(interval);
        for index in 0..count {
            if index > 0 {
                sleep(interval).await;
            }
            let sent = self
                .native(
                    &self.master(),
                    "announce",
                    json!({"message":message,"count":1}),
                )
                .await?;
            if sent != true {
                return Ok(sent);
            }
        }
        Ok(json!(true))
    }

    async fn snapshots(&self, shard: &str, limit: u64, before: Option<u64>) -> Result<Value> {
        let driver = self.driver(shard)?;
        let initial = identity(&driver.snapshot())?;
        let mut catalog = driver
            .request(
                "list_snapshots",
                native_arguments(json!({"limit":limit,"before":before})),
                COMMAND,
            )
            .await
            .map_err(driver_error)?;
        let session = catalog["session_id"]
            .as_str()
            .ok_or_else(|| Error::new(ErrorCode::Protocol, "snapshot session is missing"))?
            .to_owned();
        if session != initial.session_id {
            return Err(Error::new(
                ErrorCode::StaleReference,
                "snapshot catalog belongs to another world session",
            ));
        }
        let snapshots = catalog["snapshots"]
            .as_array_mut()
            .ok_or_else(|| Error::new(ErrorCode::Protocol, "snapshot catalog is invalid"))?;
        self.with_lock(|lock| {
            for snapshot in snapshots {
                snapshot["metadata"] = Value::Null;
                let Some(path) = snapshot["world_file"].as_str() else {
                    continue;
                };
                let id = snapshot["snapshot_id"]
                    .as_u64()
                    .ok_or_else(|| anyhow::anyhow!("snapshot identifier is missing"))?;
                let expected = format!("session/{session}/{id:010}");
                anyhow::ensure!(
                    path == expected,
                    "native snapshot path does not match its session and ID"
                );
                // Both the save and its metadata are opened through the held directory descriptor.
                let relative = format!("{shard}/save/{expected}");
                if let Some(source) = lock.read_optional_text(format!("{relative}.meta"))? {
                    let _world = lock.open_regular(Path::new(&relative))?;
                    let mut metadata = crate::lua::parse_return_table(&source)?;
                    if let Some(cycles) = metadata["clock"]["cycles"].as_u64() {
                        metadata["day"] = json!(cycles + 1);
                    }
                    snapshot["metadata"] = metadata;
                }
            }
            Ok(())
        })?;
        if identity(&driver.snapshot())? != initial {
            return Err(Error::new(
                ErrorCode::StaleReference,
                "world changed while reading snapshot metadata",
            ));
        }
        Ok(catalog)
    }

    async fn complete_snapshot(
        &self,
        id: u64,
        sessions: &BTreeMap<String, SessionIdentity>,
        day: Option<u64>,
    ) -> Result<bool> {
        for (name, identity) in sessions {
            let catalog = self.snapshots(name, 1, Some(id + 1)).await?;
            if catalog["session_id"].as_str() != Some(&identity.session_id) {
                return Err(Error::new(
                    ErrorCode::StaleReference,
                    "world changed during snapshot selection",
                ));
            }
            let snapshot = &catalog["snapshots"][0];
            if snapshot["snapshot_id"] != id
                || snapshot["world_file"].as_str().is_none()
                || day.is_some_and(|day| snapshot["metadata"]["day"] != day)
            {
                return Ok(false);
            }
        }
        Ok(true)
    }
    async fn select_snapshot(
        &self,
        mut count: u64,
        day: Option<u64>,
        sessions: &BTreeMap<String, SessionIdentity>,
    ) -> Result<Value> {
        let mut before = None;
        let mut selected = None;
        for _ in 0..64 {
            let catalog = self.snapshots(&self.master(), 100, before).await?;
            let snapshots = catalog["snapshots"]
                .as_array()
                .ok_or_else(|| Error::new(ErrorCode::Protocol, "invalid snapshot list"))?;
            for snapshot in snapshots {
                let id = snapshot["snapshot_id"].as_u64().ok_or_else(|| {
                    Error::new(ErrorCode::Protocol, "invalid snapshot identifier")
                })?;
                if id == 0
                    || snapshot["world_file"].as_str().is_none()
                    || day.is_some_and(|day| snapshot["metadata"]["day"] != day)
                {
                    continue;
                }
                if day.is_none() && count > 0 {
                    count -= 1;
                    continue;
                }
                if self.complete_snapshot(id, sessions, day).await? {
                    selected = Some(snapshot.clone());
                    if day.is_none() {
                        return Ok(snapshot.clone());
                    }
                } else if day.is_none() {
                    return Err(Error::new(
                        ErrorCode::NotFound,
                        "requested snapshot is missing from a configured shard",
                    ));
                }
            }
            if catalog["has_more"] != true {
                return selected.ok_or_else(|| {
                    Error::new(
                        ErrorCode::NotFound,
                        "no complete room snapshot matches the requested target",
                    )
                });
            }
            let next = snapshots
                .last()
                .and_then(|snapshot| snapshot["snapshot_id"].as_u64())
                .ok_or_else(|| {
                    Error::new(ErrorCode::Protocol, "snapshot catalog did not advance")
                })?;
            if before.is_some_and(|before| next >= before) {
                return Err(Error::new(
                    ErrorCode::Protocol,
                    "snapshot catalog did not advance",
                ));
            }
            before = Some(next);
        }
        Err(Error::new(
            ErrorCode::Overflow,
            "snapshot selection exceeds 6400 entries",
        ))
    }
    fn identities(&self) -> Result<BTreeMap<String, SessionIdentity>> {
        self.drivers()
            .into_iter()
            .map(|(name, driver)| identity(&driver.snapshot()).map(|identity| (name, identity)))
            .collect()
    }

    async fn reload(&self, target: &Target, request: &Request) -> Result<Value> {
        self.require_ready()?;
        let initial = self.identities()?;
        let mut shard = self.master();
        let mut method = request.method();
        let mut args = request.arguments()?;
        let mut selected = None;
        let mut snapshot_id = None;
        match request {
            Request::Reset {} | Request::Rollback { .. } | Request::RollbackToDay { .. } => {
                let count = if let Request::Rollback { count } = request {
                    *count
                } else {
                    0
                };
                let day = if let Request::RollbackToDay { day } = request {
                    Some(*day)
                } else {
                    None
                };
                let snapshot = self.select_snapshot(count, day, &initial).await?;
                snapshot_id = snapshot["snapshot_id"].as_u64();
                args = json!({"session_id":initial[&shard].session_id,"snapshot_id":snapshot_id});
                method = "rollback_to_snapshot";
                selected = Some(snapshot);
            }
            Request::RollbackToSnapshot {
                session_id,
                snapshot_id: id,
            } => {
                if &initial[&shard].session_id != session_id {
                    return Err(Error::new(
                        ErrorCode::StaleReference,
                        "rollback target belongs to another world session",
                    ));
                }
                if !self.complete_snapshot(*id, &initial, None).await? {
                    return Err(Error::new(
                        ErrorCode::NotFound,
                        "rollback target is incomplete",
                    ));
                }
                snapshot_id = Some(*id);
            }
            Request::Regenerate {
                require_empty: Some(true),
                ..
            } => {
                for name in initial.keys() {
                    let presence = self.native(name, "presence", json!({})).await?;
                    if presence["reliable"] != true
                        || presence["client_count"] != 0
                        || presence["player_count"] != 0
                    {
                        return Err(Error::new(
                            ErrorCode::Conflict,
                            "regeneration requires every shard to be empty",
                        ));
                    }
                }
            }
            Request::RegenerateShard { .. } => {
                if let Target::Shard(name) = target {
                    shard = name.clone();
                }
            }
            _ => {}
        }
        let deadline = Instant::now() + Duration::from_secs_f64(RELOAD_TIMEOUT);
        match self
            .driver(&shard)?
            .request(method, native_arguments(args), COMMAND)
            .await
        {
            Ok(_) => {}
            Err(error)
                if error.written
                    && matches!(
                        error.code,
                        DriverErrorCode::Unknown | DriverErrorCode::Timeout
                    ) => {}
            Err(error) => return Err(driver_error(error)),
        }
        self.set_phase(Phase::Starting, None);
        loop {
            let drivers = self.drivers();
            let all_ready = drivers.iter().all(|(name, driver)| {
                let state = driver.snapshot();
                let changed = matches!(request, Request::RegenerateShard { .. }) && name != &shard
                    || state
                        .generation
                        .is_some_and(|generation| generation > initial[name].generation);
                state.running && state.ready && changed
            });
            if drivers.values().any(|driver| !driver.snapshot().running) {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "shard exited while reloading the world",
                ));
            }
            if all_ready {
                break;
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "new world generation did not become ready",
                ));
            }
            sleep(Duration::from_millis(100)).await;
        }
        self.wait_ready(deadline.saturating_duration_since(Instant::now()))
            .await?;
        for (name, driver) in self.drivers() {
            let state = driver.snapshot();
            let current = identity(&state)?;
            let runtime = state.runtime.as_ref().ok_or_else(|| {
                Error::new(ErrorCode::Protocol, "reloaded world runtime is unavailable")
            })?;
            let regenerating = matches!(request, Request::Regenerate { .. })
                || matches!(request, Request::RegenerateShard { .. }) && name == shard;
            if regenerating && current.session_id == initial[&name].session_id
                || !regenerating && current.session_id != initial[&name].session_id
            {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "reloaded world has an unexpected session identity",
                ));
            }
            if snapshot_id.is_some_and(|id| runtime["snapshot"].as_u64() != Some(id + 1)) {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "reloaded world did not confirm the requested snapshot",
                ));
            }
        }
        self.set_phase(Phase::Running, None);
        Ok(selected.unwrap_or(Value::Null))
    }

    async fn locate(
        &self,
        userid: &str,
        requested: Option<&str>,
    ) -> Result<(PlayerLocation, Option<u64>)> {
        self.require_ready()?;
        let names: Vec<_> = self
            .cluster
            .lock()
            .unwrap()
            .shards
            .iter()
            .filter(|shard| requested.is_none_or(|name| shard.name == name))
            .map(|shard| shard.name.clone())
            .collect();
        let mut observations = Vec::new();
        for name in names {
            let driver = self.driver(&name)?;
            let initial = identity(&driver.snapshot())?;
            let observed = driver
                .request("locate_player", json!({"userid":userid}), COMMAND)
                .await
                .map_err(driver_error)?;
            if identity(&driver.snapshot())? != initial
                || observed["session_id"] != initial.session_id
            {
                return Err(Error::new(
                    ErrorCode::StaleReference,
                    "world changed while locating the player",
                ));
            }
            if !observed["player"].is_null() {
                observations.push((name, initial, observed));
            }
        }
        let active: Vec<_> = observations
            .iter()
            .filter(|(_, _, observed)| {
                observed["guid"].as_u64().is_some() && observed["departing"] != true
            })
            .collect();
        let state = if active.len() > 1 {
            PlayerState::Conflict
        } else if observations
            .iter()
            .any(|(_, _, observed)| observed["departing"] == true)
        {
            PlayerState::Migrating
        } else if active.len() == 1 {
            PlayerState::Active
        } else if observations.len() > 1 {
            PlayerState::Migrating
        } else if observations.len() == 1 {
            PlayerState::Loading
        } else {
            PlayerState::Disconnected
        };
        let found = active.first().copied().or_else(|| observations.first());
        Ok((
            PlayerLocation {
                userid: userid.into(),
                shard: found.map(|(name, _, _)| name.clone()),
                identity: found.map(|(_, identity, _)| identity.clone()),
                state,
                player: found
                    .map(|(_, _, observed)| observed["player"].clone())
                    .unwrap_or(Value::Null),
            },
            found.and_then(|(_, _, observed)| observed["guid"].as_u64()),
        ))
    }
    async fn player_request(&self, target: &Target, request: &Request) -> Result<Value> {
        let userid = request.userid().expect("player request has a userid");
        let requested = match target {
            Target::Shard(name) => Some(name.as_str()),
            Target::Room => None,
        };
        let (location, guid) = self.locate(userid, requested).await?;
        if matches!(request, Request::GetPlayer { .. }) {
            return if requested.is_some() {
                Ok(location.player)
            } else {
                value(location)
            };
        }
        let (name, current) = location.require_active()?;
        let mut args = request.arguments()?;
        args["_expected_session_id"] = json!(current.session_id);
        args["_expected_generation"] = json!(current.generation);
        args["_expected_guid"] =
            json!(guid.ok_or_else(|| Error::new(
                ErrorCode::NotReady,
                "player entity has no native GUID"
            ))?);
        let destination = if let Request::Migrate { shard_id, .. } = request {
            let mut destination = None;
            for (candidate, driver) in self.drivers() {
                let runtime = driver
                    .request("runtime", json!({}), COMMAND)
                    .await
                    .map_err(driver_error)?;
                if runtime["shard_id"] == *shard_id {
                    destination = Some(candidate);
                }
            }
            let destination = destination.ok_or_else(|| {
                Error::new(
                    ErrorCode::Invalid,
                    "migration destination is not a configured shard",
                )
            })?;
            if destination == name {
                return Err(Error::new(
                    ErrorCode::Invalid,
                    "player already occupies the requested shard",
                ));
            }
            Some(destination)
        } else {
            None
        };
        let result = self.native(name, request.method(), args).await?;
        if matches!(request, Request::Kick { .. } | Request::Despawn { .. }) && result == true {
            let deadline = Instant::now() + COMMAND;
            loop {
                let (observed, observed_guid) = self.locate(userid, None).await?;
                let completed = if matches!(request, Request::Kick { .. }) {
                    observed.state == PlayerState::Disconnected
                } else {
                    observed_guid != guid || observed.identity.as_ref() != Some(current)
                };
                if completed {
                    return Ok(result);
                }
                if Instant::now() >= deadline {
                    return Err(Error::new(
                        ErrorCode::Unknown,
                        "player removal did not finish",
                    ));
                }
                sleep(Duration::from_millis(100)).await;
            }
        }
        if let Some(destination) = destination {
            if result != true {
                return Ok(result);
            }
            let deadline = Instant::now() + Duration::from_secs_f64(RELOAD_TIMEOUT);
            loop {
                let (location, _) = self.locate(userid, None).await?;
                if location.state == PlayerState::Active
                    && location.shard.as_deref() == Some(&destination)
                {
                    return value(location);
                }
                if Instant::now() >= deadline {
                    return Err(Error::new(
                        ErrorCode::Unknown,
                        "player migration did not finish entering the destination",
                    ));
                }
                sleep(Duration::from_millis(250)).await;
            }
        }
        Ok(result)
    }

    async fn dispatch(self: &Arc<Self>, target: &Target, request: &Request) -> Result<Value> {
        if request.mutating()
            && !matches!(
                request,
                Request::Start {}
                    | Request::Stop { .. }
                    | Request::Kill {}
                    | Request::Restart { .. }
                    | Request::UpdateMods { .. }
                    | Request::Configure { .. }
                    | Request::SetPolicy { .. }
                    | Request::SetAdmin { .. }
                    | Request::ExportArchive { .. }
                    | Request::ReleaseArchive { .. }
            )
        {
            self.require_ready()?;
        }
        match request {
            Request::Status {} => {
                let status = self.status();
                if let Target::Shard(name) = target {
                    return value(
                        status
                            .shards
                            .into_iter()
                            .find(|shard| &shard.name == name)
                            .ok_or_else(|| Error::new(ErrorCode::NotFound, "unknown shard"))?,
                    );
                }
                value(status)
            }
            Request::Start {} => self.start_inner().await,
            Request::Stop { notice } => {
                self.countdown(notice).await?;
                value(self.stop_inner(false).await?)
            }
            Request::Kill {} => value(self.stop_inner(true).await?),
            Request::Restart { notice } => {
                self.countdown(notice).await?;
                self.stop_inner(false).await?;
                self.start_inner().await
            }
            Request::ReadConfiguration {} => self.with_lock(|lock| {
                let cluster=crate::settings::ClusterConfig::load(lock)?;
                let control=lock.read_control()?;
                Ok(json!({"cluster":cluster.as_value(),"policy":control.get("policy").cloned().unwrap_or_else(||json!({})),"template":control.get("template").cloned()}))
            }),
            Request::ReadPermissions {} => self.with_lock(|lock| {
                let mut permissions=serde_json::Map::new();
                for field in crate::settings::PERMISSION_FIELDS {
                    let source=lock.read_optional_bytes(format!("{field}.txt"))?.unwrap_or_default();
                    permissions.insert(field.into(),json!(crate::files::permission_users(&source)));
                }
                Ok(Value::Object(permissions))
            }),
            Request::IsAdmin { userid } => self.with_lock(|lock| {
                let source=lock.read_optional_bytes("adminlist.txt")?.unwrap_or_default();
                Ok(json!(crate::files::permission_users(&source).iter().any(|entry|entry==userid)))
            }),
            Request::SetAdmin { userid,remove } => {
                let running=self.require_stopped().is_err();
                if running { self.require_ready()?; }
                let users=self.with_lock(|lock| {
                    let source=lock.read_optional_bytes("adminlist.txt")?.unwrap_or_default();
                    let updated=crate::files::edit_permission(&source,userid,*remove);
                    if updated!=source { lock.replace_adminlist(&updated)?; }
                    Ok(crate::files::permission_users(&updated))
                })?;
                if running {
                    let shards=self.all("reload_permissions",json!({})).await?;
                    if shards.as_array().is_some_and(|shards|shards.iter().any(|shard|shard["result"]["status"]=="failure" || shard["result"]["value"]!=true)) {
                        return Err(Error::new(ErrorCode::PartialFailure,"admin list was updated but some shards did not reload permissions").with_details(json!({"adminlist":users,"shards":shards})));
                    }
                }
                Ok(json!(users))
            }
            Request::Configure { configuration,replace_permissions } => {
                self.require_stopped()?;
                let configuration=crate::settings::ClusterConfig::from_value(configuration.clone()).map_err(internal)?;
                let check_ports=!self.ports.lock().unwrap().is_empty();
                let changed=self.with_lock(|lock| {
                    if check_ports {
                        let current=crate::settings::ClusterConfig::load(lock)?.resolved();
                        let next=configuration.resolved();
                        let ports=|value:&Value|->BTreeMap<String,Value> { value["shards"].as_object().map(|shards|shards.iter().map(|(name,shard)|(name.clone(),json!([shard["settings"]["server_port"],shard["settings"]["master_server_port"]]))).collect()).unwrap_or_default() };
                        anyhow::ensure!(ports(&current)==ports(&next),"changing shard names or published game or query ports requires an inactive room service");
                    }
                    configuration.save(&mut lock.while_stopped(),if *replace_permissions { crate::files::PermissionFiles::ReplaceOffline } else { crate::files::PermissionFiles::Preserve })
                })?;
                let cluster=configuration::discover(&self.directory).map_err(internal)?;
                self.status.send_modify(|status|{status.master=cluster.master().name.clone();status.shards=cluster.shards.iter().map(|shard|ShardStatus{name:shard.name.clone(),is_master:shard.master,phase:Phase::Stopped,pid:None,identity:None,readiness:Readiness::default(),error:None}).collect();});
                *self.cluster.lock().unwrap()=cluster;
                self.drivers.lock().unwrap().clear();
                Ok(json!({"changed":changed}))
            }
            Request::ExportArchive { options,compression_level } => {
                self.require_stopped()?;
                let options:crate::archive::ExportOptions=serde_json::from_value(options.clone()).map_err(internal)?;
                let room=self.clone();
                let prepared=tokio::task::spawn_blocking(move||room.with_lock(|lock|crate::archive::prepare(&mut lock.while_stopped(),&options))).await.map_err(internal)??;
                let compressed=prepared.compress(*compression_level as u32).await.map_err(internal)?;
                value(crate::archive::publish_artifact(&self.directory,compressed).await.map_err(internal)?)
            }
            Request::ReleaseArchive { artifact_id } => value(crate::archive::release_artifact(&self.directory,artifact_id).map_err(internal)?),
            Request::SetPolicy { policy } => {
                let policy:crate::policy::Policy=serde_json::from_value(policy.clone()).map_err(internal)?;
                self.with_lock(|lock| { policy.save(lock)?; Ok(json!(policy)) })
            }
            Request::Save {} => self.save().await,
            Request::Reset {}
            | Request::Rollback { .. }
            | Request::RollbackToDay { .. }
            | Request::RollbackToSnapshot { .. }
            | Request::Regenerate { .. }
            | Request::RegenerateShard { .. } => self.reload(target, request).await,
            Request::Snapshots { limit, before } => {
                let name = match target { Target::Shard(name)=>name.clone(), Target::Room=>self.master() };
                self.snapshots(&name,*limit,*before).await
            }
            Request::Execute { source } => { let Target::Shard(name)=target else { unreachable!() }; self.execute(name,source).await }
            Request::ExecuteAll { source } => {
                let names:Vec<_>=self.cluster.lock().unwrap().shards.iter().map(|shard|shard.name.clone()).collect();
                value(join_all(names.into_iter().map(|name| async move { ShardResult { result:self.execute(&name,source).await.into(), shard:name } })).await)
            }
            Request::ConnectedShards {} => {
                let Target::Shard(name) = target else {
                    unreachable!()
                };
                self.native(name, "connected_shards", json!({"current_name":name}))
                    .await
            }
            Request::GetPlayer { .. }
            | Request::Kick { .. }
            | Request::Inventory { .. }
            | Request::SetVitals { .. }
            | Request::KillPlayer { .. }
            | Request::Revive { .. }
            | Request::Despawn { .. }
            | Request::Migrate { .. }
            | Request::Teleport { .. }
            | Request::Give { .. }
            | Request::Remove { .. } => self.player_request(target, request).await,
            Request::Announce { message,count,interval } => self.announce(message,*count,*interval).await,
            Request::Ban { .. }
            | Request::Unban { .. }
            | Request::Blocklist { .. }
            | Request::IsBlocked { .. }
            | Request::IsWhitelisted { .. }
            | Request::Whitelist { .. }
            | Request::Unwhitelist { .. } => {
                self.native(&self.master(), request.method(), request.arguments()?)
                    .await
            }
            Request::UpdateMods { notice,restart } => self.update_mods(notice,*restart,self.interrupted.subscribe()).await,

            _ => match target {
                Target::Shard(name) => {
                    self.native(name, request.method(), request.arguments()?)
                        .await
                }
                Target::Room => self.all(request.method(), request.arguments()?).await,
            },
        }
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
        let output = self.output.clone();
        let relays = std::mem::take(self.relays.get_mut().unwrap());
        let exporter = self.exporter.get_mut().unwrap().clone();
        if let Some(runtime) = self
            .runtime
            .get_mut()
            .unwrap()
            .take()
            .or_else(|| tokio::runtime::Handle::try_current().ok())
        {
            runtime.spawn(async move {
                join_all(drivers.iter().map(Driver::wait)).await;
                drop(lock);
                let _ = timeout(Duration::from_secs(5), join_all(relays)).await;
                output.close().await;
                if let Some(exporter) = exporter {
                    let _ = exporter.close().await;
                }
            });
        } else {
            output.finish();
        }
    }
}

fn identity(state: &DriverState) -> Result<SessionIdentity> {
    Ok(SessionIdentity {
        session_id: state.session_id.clone().ok_or_else(|| {
            Error::new(ErrorCode::NotReady, "native world session is unavailable")
        })?,
        process_id: state.nonce.clone(),
        generation: state
            .generation
            .ok_or_else(|| Error::new(ErrorCode::NotReady, "Lua generation is unavailable"))?,
    })
}
fn last_save_id(state: &DriverState) -> u64 {
    state
        .control_records
        .iter()
        .filter_map(|record| record["save_id"].as_u64())
        .max()
        .unwrap_or(0)
}
fn saved_after(
    state: &DriverState,
    before: &DriverState,
    baseline: u64,
    target: Option<u64>,
    shutdown: bool,
) -> Option<SavedSnapshot> {
    if state.nonce != before.nonce || state.generation != before.generation {
        return None;
    }
    state.control_records.iter().rev().find_map(|record| {
        let id = record["save_id"].as_u64()?;
        let snapshot = record["snapshot_id"].as_u64()?;
        if id <= baseline
            || record["event"] != "save_complete"
            || record["session_id"].as_str() != before.session_id.as_deref()
            || target.is_some_and(|target| snapshot != target)
            || shutdown && record["shutdown"] != true
        {
            return None;
        }
        Some(SavedSnapshot {
            identity: identity(before).ok()?,
            snapshot_id: snapshot,
            save_id: id,
        })
    })
}
fn value(value: impl Serialize) -> Result<Value> {
    serde_json::to_value(value).map_err(internal)
}

fn native_arguments(mut arguments: Value) -> Value {
    // Lua treats JSON null as a value, while omitted optional arguments are nil.
    if let Some(arguments) = arguments.as_object_mut() {
        arguments.retain(|_, value| !value.is_null());
    }
    arguments
}
fn internal(error: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Internal, error.to_string())
}
fn timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64
}
fn driver_error(error: DriverError) -> Error {
    let code = match error.code {
        DriverErrorCode::InvalidRequest => ErrorCode::Invalid,
        DriverErrorCode::NotReady => ErrorCode::NotReady,
        DriverErrorCode::Busy => ErrorCode::Busy,
        DriverErrorCode::Unsupported => ErrorCode::Unsupported,
        DriverErrorCode::NotFound => ErrorCode::NotFound,
        DriverErrorCode::StaleReference => ErrorCode::StaleReference,
        DriverErrorCode::Lua => ErrorCode::Lua,
        DriverErrorCode::Unknown => ErrorCode::Unknown,
        DriverErrorCode::Timeout => ErrorCode::Timeout,
        DriverErrorCode::Transport => ErrorCode::Transport,
        DriverErrorCode::Protocol => ErrorCode::Protocol,
    };
    Error::new(code, error.message.clone()).with_details(json!(error))
}
fn render_notice(template: &str, parameters: &BTreeMap<String, Value>) -> String {
    let mut output = String::new();
    let mut chars = template.chars().peekable();
    while let Some(character) = chars.next() {
        if character == '{' && chars.peek() == Some(&'{') {
            chars.next();
            output.push('{');
        } else if character == '}' && chars.peek() == Some(&'}') {
            chars.next();
            output.push('}');
        } else if character == '{' {
            let name: String = chars
                .by_ref()
                .take_while(|character| *character != '}')
                .collect();
            if let Some(value) = parameters.get(&name) {
                output.push_str(value.as_str().unwrap_or(&value.to_string()));
            }
        } else {
            output.push(character);
        }
    }
    output
}

#[derive(Clone)]
struct LocalOutput {
    shared: Arc<(Mutex<OutputQueue>, Condvar)>,
    finished: watch::Receiver<bool>,
}
#[derive(Default)]
struct OutputQueue {
    records: VecDeque<Vec<u8>>,
    bytes: usize,
    closed: bool,
    dropped: u64,
    written: u64,
}
impl LocalOutput {
    fn new() -> Self {
        let shared = Arc::new((Mutex::new(OutputQueue::default()), Condvar::new()));
        let (complete, finished) = watch::channel(false);
        let output = shared.clone();
        // A blocked journal reader must never occupy the game protocol task or runtime pool.
        std::thread::spawn(move || {
            use std::io::Write;
            loop {
                let record = {
                    let (queue, available) = output.as_ref();
                    let mut queue = queue.lock().unwrap();
                    while queue.records.is_empty() && !queue.closed {
                        queue = available.wait(queue).unwrap();
                    }
                    let Some(record) = queue.records.pop_front() else {
                        break;
                    };
                    record
                };
                let result = std::io::stdout().lock().write_all(&record);
                let mut queue = output.0.lock().unwrap();
                queue.bytes = queue.bytes.saturating_sub(record.len());
                if result.is_ok() {
                    queue.written += 1;
                } else {
                    queue.dropped += 1;
                }
            }
            complete.send_replace(true);
        });
        Self { shared, finished }
    }
    fn submit(&self, record: &Value) -> bool {
        let Ok(mut encoded) = serde_json::to_vec(record) else {
            return false;
        };
        let mut framed = b"DST_RECORD|".to_vec();
        framed.append(&mut encoded);
        framed.push(b'\n');
        let (queue, available) = self.shared.as_ref();
        let mut queue = queue.lock().unwrap();
        if queue.closed
            || queue.records.len() >= 1024
            || queue.bytes + framed.len() > 8 * 1024 * 1024
        {
            queue.dropped += 1;
            return false;
        }
        queue.bytes += framed.len();
        queue.records.push_back(framed);
        available.notify_one();
        true
    }
    fn stats(&self) -> Value {
        let queue = self.shared.0.lock().unwrap();
        json!({"queued_records":queue.records.len(),"queued_bytes":queue.bytes,"dropped":queue.dropped,"written":queue.written,"closed":queue.closed})
    }
    fn finish(&self) {
        self.shared.0.lock().unwrap().closed = true;
        self.shared.1.notify_all();
    }
    async fn close(&self) -> bool {
        self.finish();
        let mut finished = self.finished.clone();
        timeout(Duration::from_secs(5), async {
            while !*finished.borrow_and_update() {
                if finished.changed().await.is_err() {
                    return false;
                }
            }
            true
        })
        .await
        .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn telemetry_shutdown_reports_failed_relays_and_allows_counted_drops() {
        for failed_relay in [true, false] {
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
            )
            .unwrap();
            room.losses.record_subscription_drop(3);
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
            assert_eq!(report["stats"]["streams"]["subscription_dropped"], 3);
            assert!(report["export_error"].is_null());
            assert!(report["observability_error"].is_null());
        }
    }
}
