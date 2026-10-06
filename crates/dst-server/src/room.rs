//! One room owns every configured shard and each accepted operation until completion.

use crate::{
    configuration::{self, Cluster},
    driver::{
        Driver, DriverError, DriverErrorCode, DriverEvents, DriverOptions, DriverState,
        control_ready,
    },
    files::RoomLock,
    model::*,
    operation::{Layer, OperationCompletion, OperationContext, Source, timestamp},
    recovery::{self, FailureStage, FailureTrigger, FailureTriggerKind},
    rpc::{EventHub, StreamKind},
    telemetry::{LogEvent, TelemetryConfig},
};
use futures::future::join_all;
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore, oneshot, watch},
    time::{Instant, sleep, timeout},
};

const COMMAND: Duration = Duration::from_secs(120);

mod configuration_edits;
mod players;
mod telemetry;
use self::telemetry::RoomTelemetry;
mod shutdown;
mod worlds;
use shutdown::StopIntent;
pub(crate) use shutdown::{ShutdownDeadlines, StopReservation};

pub(crate) fn mod_download_outcome(result: &Result<Value>) -> &'static str {
    match result {
        Ok(value) if value["download_completed"] == true => "download_completed",
        Ok(_) => "download_skipped",
        Err(_) => "download_failed",
    }
}

enum StartOutcome {
    Ready,
    Cancelled,
}

struct RunAttempt {
    id: String,
    evidence_lost: bool,
}

pub struct StoppedOperation {
    room: Arc<Room>,
    _permit: OwnedSemaphorePermit,
    interrupted: watch::Receiver<u64>,
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
    pub(crate) fn record_prepared(&self) -> Result<()> {
        self.check_interrupted()?;
        self.room
            .update_recovery("target_prepared", None, recovery::mark_prepared)
    }
    pub(crate) fn check_interrupted(&self) -> Result<()> {
        if self.interrupted.has_changed().unwrap_or(true) {
            return Err(Error::shutdown(ErrorCode::Unknown));
        }
        Ok(())
    }
    pub(crate) async fn interrupted(&self) {
        let mut interrupted = self.interrupted.clone();
        let _ = interrupted.changed().await;
    }
    pub(crate) fn spawn_driver(
        &self,
        cluster: &Cluster,
        shard: &str,
        nonce: &str,
        arguments: Vec<std::ffi::OsString>,
    ) -> Result<Driver> {
        let _drivers = self.room.drivers.lock().unwrap();
        self.check_interrupted()?;
        Driver::spawn(cluster, self.executable(), shard, nonce, arguments, None).map_err(internal)
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
    attempt: Mutex<Option<RunAttempt>>,
    status: watch::Sender<RoomStatus>,
    mutation: Arc<Semaphore>,
    requests: Arc<Semaphore>,
    interrupted: watch::Sender<u64>,
    restart_required: AtomicBool,
    shutdown_pending: AtomicU64,
    service_shutdown: Mutex<Option<ShutdownDeadlines>>,
    shutdown_expedited: AtomicBool,
    stop_intent: watch::Sender<Option<StopIntent>>,
    fault_cleanup: Mutex<Option<watch::Receiver<Option<Result<StopResult>>>>>,
    telemetry: Arc<RoomTelemetry>,
    relays: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    telemetry_close: Mutex<Option<watch::Receiver<Option<Result<Value>>>>>,
    recovery_observed: Mutex<BTreeMap<String, Value>>,
}

impl Room {
    pub fn open(
        directory: impl AsRef<Path>,
        executable: impl AsRef<Path>,
        options: DriverOptions,
        events: EventHub,
        mut telemetry: TelemetryConfig,
    ) -> Result<Arc<Self>> {
        let lock = RoomLock::try_acquire(directory.as_ref()).map_err(internal)?;
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
        let cluster_name = telemetry.bind_room(executable.as_ref(), &cluster.directory);
        crate::telemetry::validate_context(
            &telemetry.resource,
            &cluster_name,
            cluster.shards.iter().map(|shard| shard.name.as_str()),
        )?;
        let telemetry = Arc::new(RoomTelemetry::new(telemetry, events));
        Ok(Arc::new(Self {
            cluster_name,
            directory: cluster.directory.clone(),
            executable: executable.as_ref().to_owned(),
            runtime: Mutex::new(tokio::runtime::Handle::try_current().ok()),
            lock: Mutex::new(Some(lock)),
            cluster: Mutex::new(cluster),
            options: Mutex::new(options),
            ports: Mutex::new(BTreeMap::new()),
            drivers: Mutex::new(BTreeMap::new()),
            attempt: Mutex::new(None),
            status,
            mutation: Arc::new(Semaphore::new(1)),
            requests: Arc::new(Semaphore::new(64)),
            interrupted,
            restart_required: AtomicBool::new(false),
            shutdown_pending: AtomicU64::new(0),
            service_shutdown: Mutex::new(None),
            shutdown_expedited: AtomicBool::new(false),
            stop_intent: watch::channel(None).0,
            fault_cleanup: Mutex::new(None),
            telemetry,
            relays: Mutex::new(Vec::new()),
            telemetry_close: Mutex::new(None),
            recovery_observed: Mutex::new(BTreeMap::new()),
        }))
    }

    pub(crate) fn record_management(
        &self,
        event: &str,
        body: Value,
        severity: &str,
        context: Option<&OperationContext>,
    ) {
        let mut log = LogEvent::new(event, body, severity);
        log.attributes
            .insert("dst.cluster.name".into(), json!(self.cluster_name));
        if let Some(context) = context {
            log.correlate(context);
        }
        self.telemetry.publish(&log, StreamKind::Lifecycle);
    }

    pub(crate) fn begin_operation(
        &self,
        operation: &OperationStatus,
        context: &mut OperationContext,
        stage: &str,
    ) -> Option<crate::observability::OperationObservation> {
        context.started = std::time::Instant::now();
        let shard = match &operation.target {
            Target::Shard(shard) => Some(shard.as_str()),
            Target::Room => None,
        };
        let session = self
            .drivers()
            .get(shard.unwrap_or(&self.master()))
            .and_then(|driver| driver.snapshot().session_id);
        let observation = self.telemetry.observer.as_ref().map(|observer| {
            observer.begin_context_operation(
                &self.cluster_name,
                shard,
                &operation.method,
                session.as_deref(),
                context,
            )
        });
        self.record_operation(operation, context, stage, None);
        observation
    }

    pub(crate) fn finish_operation(
        &self,
        operation: &OperationStatus,
        context: &OperationContext,
        observation: Option<crate::observability::OperationObservation>,
        result: std::result::Result<&'static str, &Error>,
    ) {
        let completion = OperationCompletion::from(result);
        self.record_operation(operation, context, "finished", Some(completion));
        if let Some(observation) = observation {
            observation.finish_with(completion);
        }
    }

    fn record_operation(
        &self,
        operation: &OperationStatus,
        context: &OperationContext,
        stage: &str,
        completion: Option<OperationCompletion<'_>>,
    ) {
        let error = completion.and_then(|completion| completion.error);
        let mut body = json!({
            "method":operation.method,"target":operation.target,"phase":stage,
            "started_at_ns":operation.started_at_ns,
            "completed_at_ns":completion.map(|_| timestamp()),
            "duration_seconds":completion.map(|_| context.started.elapsed().as_secs_f64()),
            "outcome":completion.map(|completion| completion.outcome),
            "error_code":error.map(|error| error.code),
        });
        for (role, userid) in &context.player_roles {
            body[*role] = json!(userid);
        }
        self.record_management(
            "dst.server.operation",
            body,
            if error.is_some() { "WARN" } else { "INFO" },
            Some(context),
        );
    }

    pub(crate) fn update_recovery<T>(
        &self,
        stage: &str,
        context: Option<&OperationContext>,
        update: impl FnOnce(&mut RoomLock) -> anyhow::Result<T>,
    ) -> Result<T> {
        self.update_recovery_if_changed(stage, context, |lock| Ok((update(lock)?, true)))
    }

    pub(crate) fn update_recovery_if_changed<T>(
        &self,
        stage: &str,
        context: Option<&OperationContext>,
        update: impl FnOnce(&mut RoomLock) -> anyhow::Result<(T, bool)>,
    ) -> Result<T> {
        // Keep the observed summary in the same order as persisted recovery updates.
        let mut previous = self.recovery_observed.lock().unwrap();
        let (result, snapshot, changed) = self.with_lock(|lock| {
            let (result, changed) = update(lock)?;
            let snapshot = changed
                .then(|| recovery::load_with_run(lock).ok())
                .flatten();
            Ok((result, snapshot, changed))
        })?;
        if !changed {
            return Ok(result);
        }
        if let Some((state, run)) = snapshot {
            let stage = if stage == "failure_finalized" && run.failure.is_none() {
                "stopped"
            } else {
                stage
            };
            let mut records = crate::telemetry::management::recovery_records(&state, &run);
            for (key, body) in &mut records {
                let reset = stage.strip_prefix("reset_");
                if reset.is_some() && !key.is_empty()
                    || reset.is_none() && previous.get(key) == Some(body)
                {
                    continue;
                }
                let mut body = body.clone();
                body["stage"] = json!(if reset.is_some() {
                    "budget_reset"
                } else {
                    stage
                });
                if let Some(reason) = reset {
                    body["reason"] = json!(reason);
                }
                self.record_management(
                    "dst.recovery.transition",
                    body,
                    if stage != "stopped"
                        && (state.closed.is_some()
                            || matches!(
                                stage,
                                "failure_captured" | "failure_finalized" | "recovery_failed"
                            ))
                    {
                        "ERROR"
                    } else {
                        "INFO"
                    },
                    context,
                );
            }
            *previous = records;
        } else {
            self.telemetry.observation_failed();
        }
        Ok(result)
    }

    pub async fn while_stopped(self: &Arc<Self>) -> Result<StoppedOperation> {
        let interrupted = self.interrupted.subscribe();
        self.ensure_open()?;
        let permit = self
            .mutation
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::new(ErrorCode::Busy, "another room operation is running"))?;
        self.ensure_open()?;
        if interrupted.has_changed().unwrap_or(true) {
            return Err(Error::shutdown(ErrorCode::Unknown));
        }
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
            interrupted,
        })
    }
    pub fn driver_states(&self) -> BTreeMap<String, DriverState> {
        self.drivers()
            .into_iter()
            .map(|(name, driver)| (name, driver.snapshot()))
            .collect()
    }
    fn failure_trigger(
        &self,
        kind: FailureTriggerKind,
        stage: FailureStage,
        code: ErrorCode,
        force_requested: bool,
        context: Option<&OperationContext>,
    ) -> FailureTrigger {
        FailureTrigger {
            kind,
            stage,
            code: Some(code),
            force_requested,
            operation_id: self
                .status()
                .operations
                .current
                .map(|operation| operation.id)
                .or_else(|| context.map(|context| context.id.clone())),
            observed_at_ms: timestamp() / 1_000_000,
        }
    }
    fn record_failure(
        &self,
        trigger: FailureTrigger,
        states: &BTreeMap<String, DriverState>,
        failed: &BTreeSet<String>,
    ) -> Result<()> {
        let required = self
            .cluster
            .lock()
            .unwrap()
            .shards
            .iter()
            .map(|shard| shard.name.clone())
            .collect();
        let mut attempt = self.attempt.lock().unwrap();
        let Some(attempt) = attempt.as_mut() else {
            return Ok(());
        };
        let result = self.update_recovery("failure_captured", None, |lock| {
            recovery::record_failure(lock, &attempt.id, trigger, &required, states, failed)
        });
        attempt.evidence_lost |= result.is_err();
        result
    }
    fn merge_failure(&self, states: &BTreeMap<String, DriverState>) -> Result<()> {
        let mut attempt = self.attempt.lock().unwrap();
        let Some(attempt) = attempt.as_mut() else {
            return Ok(());
        };
        let result = self.update_recovery("failure_captured", None, |lock| {
            recovery::merge_failure(lock, &attempt.id, states)
        });
        attempt.evidence_lost |= result.is_err();
        result
    }
    pub(crate) fn finish_failure(&self) -> Result<()> {
        self.finish_failure_after(None)
    }
    fn finish_failure_after(&self, before: Option<&BTreeMap<String, DriverState>>) -> Result<()> {
        let states = self.driver_states();
        let mut current = self.attempt.lock().unwrap();
        let Some(attempt) = current.as_mut() else {
            return Ok(());
        };
        let result = self.update_recovery("failure_finalized", None, |lock| {
            recovery::finish_failure(lock, &attempt.id, before, &states, attempt.evidence_lost)
        });
        match result {
            Ok(true) => *current = None,
            Ok(false) => {}
            Err(error) => {
                attempt.evidence_lost = true;
                return Err(error);
            }
        }
        Ok(())
    }
    pub async fn force_fault(self: &Arc<Self>, error: Error) -> Result<StopResult> {
        self.force_fault_in_context(error, &BTreeSet::new(), None)
            .await
    }

    pub(crate) async fn force_fault_in_context(
        self: &Arc<Self>,
        error: Error,
        failed: &BTreeSet<String>,
        parent: Option<&OperationContext>,
    ) -> Result<StopResult> {
        let context = parent.map_or_else(
            || OperationContext::new(Source::RoomApi, Layer::Room, None),
            |parent| parent.child(Layer::Room),
        );
        self.restart_required.store(true, Ordering::Release);
        let states = self.driver_states();
        let stage = match self.status().phase {
            Phase::Preparing => FailureStage::Prepare,
            Phase::Starting => FailureStage::Startup,
            _ => FailureStage::Runtime,
        };
        let persisted = self.record_failure(
            self.failure_trigger(
                FailureTriggerKind::Operation,
                stage,
                error.code,
                false,
                Some(&context),
            ),
            &states,
            failed,
        );
        self.start_fault_cleanup(error, persisted.clone(), context);
        let result = self
            .wait_fault_cleanup()
            .await?
            .ok_or_else(|| Error::new(ErrorCode::Internal, "fault cleanup was not started"))?;
        persisted.map(|()| result)
    }

    fn start_fault_cleanup(
        self: &Arc<Self>,
        error: Error,
        persisted: Result<()>,
        mut context: OperationContext,
    ) {
        if let Err(error) = &persisted {
            self.record_management(
                "dst.recovery.transition",
                json!({"stage":"persistence_failed","error_code":error.code}),
                "ERROR",
                Some(&context),
            );
        }
        let mut current = self.fault_cleanup.lock().unwrap();
        if current.is_some() {
            return;
        }
        let (complete, completion) = watch::channel(None);
        *current = Some(completion);
        self.interrupt();
        let room = self.clone();
        tokio::spawn(async move {
            let permit = room.mutation.clone().acquire_owned().await;
            let operation = context.status("fault_cleanup", Target::Room);
            let observation = room.begin_operation(&operation, &mut context, "started");
            let result = if permit.is_ok() {
                room.stop_inner(false, Some(&context))
                    .await
                    .and_then(|result| persisted.map(|()| result))
            } else {
                Err(Error::new(
                    ErrorCode::Internal,
                    "room operation queue closed",
                ))
            };
            room.set_phase(Phase::Failed, Some(error));
            room.finish_operation(
                &operation,
                &context,
                observation,
                result.as_ref().map(|_| "completed"),
            );
            drop(permit);
            complete.send_replace(Some(result));
        });
    }

    pub(crate) async fn wait_fault_cleanup(&self) -> Result<Option<StopResult>> {
        let completion = self.fault_cleanup.lock().unwrap().clone();
        let Some(mut completion) = completion else {
            return Ok(None);
        };
        completion
            .wait_for(Option::is_some)
            .await
            .map_err(|_| Error::new(ErrorCode::Internal, "fault cleanup worker exited"))?
            .clone()
            .expect("completed fault cleanup")
            .map(Some)
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
        .map_err(|error| error.downcast::<Error>().unwrap_or_else(internal))
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
        let drivers = self.drivers.lock().unwrap();
        if drivers.values().any(|driver| driver.snapshot().running) {
            Err(Error::new(
                ErrorCode::Busy,
                "all game processes must be stopped",
            ))
        } else if drivers.values().any(|driver| {
            driver
                .snapshot()
                .exit
                .as_ref()
                .is_none_or(|exit| !exit.output_drained)
        }) {
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
        let events = &self.telemetry.local.events;
        if events.has_subscribers(StreamKind::Lifecycle) {
            events.publish(StreamKind::Lifecycle, &json!({"event":"room_status", "status":self.status(), "observed_timestamp_ns":timestamp()}));
        }
    }
    fn record_admin_operation(
        &self,
        operation: &OperationStatus,
        envelope: &Envelope,
        result: &Result<Value>,
        context: &OperationContext,
    ) {
        if !matches!(
            envelope.request,
            Request::Kick { .. }
                | Request::Ban { .. }
                | Request::Unban { .. }
                | Request::SetAdmin { .. }
                | Request::Whitelist { .. }
                | Request::Unwhitelist { .. }
        ) {
            return;
        }
        let Ok(mut body) = envelope.request.arguments() else {
            self.telemetry.observation_failed();
            return;
        };
        let error = result.as_ref().err();
        body["operation_id"] = json!(operation.id);
        body["method"] = json!(operation.method);
        body["target"] = json!(operation.target);
        body["started_at_ns"] = json!(operation.started_at_ns);
        body["status"] = json!(match error {
            None => "success",
            Some(error) if error.code == ErrorCode::Unknown => "unknown",
            Some(_) => "failure",
        });
        body["result"] = json!(result.as_ref().ok().and_then(Value::as_bool));
        body["error_code"] = json!(error.map(|error| error.code));
        self.record_management(
            "dst.admin.operation",
            body,
            if error.is_some() { "WARN" } else { "INFO" },
            Some(context),
        );
    }
    pub(crate) fn observe_presence(
        &self,
        shard: &ShardStatus,
        presence: Option<&crate::policy::Presence>,
    ) {
        if let (Some(observer), Some(identity)) =
            (self.telemetry.observer.as_ref(), shard.identity.as_ref())
        {
            observer.observe_presence(&self.cluster_name, &shard.name, identity, presence);
        }
    }

    fn refresh_shard(&self, state: &DriverState) {
        if let Some(observer) = self.telemetry.observer.as_ref() {
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
                shard.readiness.world_loaded =
                    state.running && state.native_ready && state.session_id.is_some();
                shard.readiness.control_ready = state.ready;
                shard.error = state
                    .failure
                    .as_ref()
                    .map(|failure| driver_error(failure.clone()));
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
        let _drivers = self.drivers.lock().unwrap();
        self.interrupted
            .send_modify(|revision| *revision = revision.wrapping_add(1));
    }

    /// A timeout or dropped caller only stops waiting; the accepted mutation remains owned here.
    pub async fn invoke(self: &Arc<Self>, envelope: Envelope) -> Result<Value> {
        self.invoke_inner(envelope, false, None, None).await
    }

    /// The Agent already owns the caller's wait and retains this result through cleanup.
    pub(crate) async fn invoke_until_complete(
        self: &Arc<Self>,
        envelope: Envelope,
    ) -> Result<Value> {
        self.invoke_inner(envelope, true, None, None).await
    }

    pub(crate) async fn invoke_in_context(
        self: &Arc<Self>,
        envelope: Envelope,
        context: &OperationContext,
    ) -> Result<Value> {
        self.invoke_inner(envelope, true, Some(context), None)
            .await
            .map_err(|error| {
                if error.is_shutdown() {
                    Error::shutdown(ErrorCode::Unknown)
                } else {
                    error
                }
            })
    }

    async fn invoke_inner(
        self: &Arc<Self>,
        envelope: Envelope,
        wait_for_completion: bool,
        parent: Option<&OperationContext>,
        mut reservation: Option<StopReservation>,
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
        let mut interrupted = reservation.as_ref().map_or_else(
            || self.interrupted.subscribe(),
            |reservation| reservation.interrupted.clone(),
        );
        let interruption_revision = reservation
            .as_ref()
            .map_or_else(|| *interrupted.borrow(), |reservation| reservation.revision);
        if matches!(envelope.request, Request::Status {}) {
            return self
                .dispatch(&envelope.target, &envelope.request, None)
                .await;
        }
        if !matches!(envelope.request, Request::Kill {}) {
            self.ensure_open()?;
        }
        let mut request_slot = if reservation.is_some() {
            None
        } else {
            Some(
                self.requests
                    .clone()
                    .try_acquire_owned()
                    .map_err(|_| Error::new(ErrorCode::Busy, "room request limit reached"))?,
            )
        };
        if !envelope.request.mutating() {
            return tokio::select! {
                biased;
                _ = sleep(wait) => Err(Error::new(ErrorCode::Timeout, "query wait timed out")),
                result = self.dispatch(&envelope.target, &envelope.request, None) => result,
            };
        }
        let mut context = parent
            .map_or_else(
                || OperationContext::new(Source::RoomApi, Layer::Room, None),
                |parent| parent.child(Layer::Room),
            )
            .with_request_players(&envelope.request);
        let operation = context.status(envelope.request.method(), envelope.target.clone());
        let killing = matches!(envelope.request, Request::Kill {});
        let mut kill_persistence = Ok(());
        if killing {
            let states = self.driver_states();
            let mut trigger = self.failure_trigger(
                FailureTriggerKind::ForcedStop,
                FailureStage::Shutdown,
                ErrorCode::Unknown,
                true,
                Some(&context),
            );
            trigger.operation_id = Some(operation.id.clone());
            kill_persistence =
                self.record_failure(trigger, &states, &states.keys().cloned().collect());
            self.restart_required.store(true, Ordering::Release);
            self.interrupt();
            for driver in self.drivers().values() {
                driver.request_kill();
            }
        }
        let mut permit = if killing || reservation.is_some() {
            None
        } else {
            Some(self.mutation.clone().try_acquire_owned().map_err(|_| {
                Error::new(ErrorCode::Busy, "another room mutation is running")
                    .with_details(json!({"operation": self.status().operations.current}))
            })?)
        };
        if let Some(reservation) = reservation.as_mut() {
            let (slot, mutation) = reservation.handoff(&context)?;
            request_slot = Some(slot);
            permit = Some(mutation);
        } else if !killing {
            let _drivers = self.drivers.lock().unwrap();
            self.ensure_open()?;
            if *interrupted.borrow() != interruption_revision {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "operation interrupted before admission",
                ));
            }
            if let Request::Stop { notice }
            | Request::Restart { notice }
            | Request::UpdateMods { notice, .. } = &envelope.request
            {
                self.register_stop_intent(&operation.id, notice);
            }
        }
        let operation_id = operation.id.clone();
        let (reply, receive) = oneshot::channel();
        let room = self.clone();
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
            let mut operation = operation;
            operation.started_at_ns = timestamp();
            room.status
                .send_modify(|state| state.operations.current = Some(operation.clone()));
            let observation = room.begin_operation(&operation, &mut context, "started");
            let mut result = if !killing && *interrupted.borrow() != interruption_revision {
                Err(Error::new(
                    ErrorCode::Unknown,
                    "operation interrupted before execution",
                ))
            } else if let Request::UpdateMods { notice, restart } = &envelope.request {
                room.update_mods(notice, *restart, interrupted.clone(), &context, deadline)
                    .await
            } else {
                tokio::select! {
                    result=async {
                        if matches!(envelope.request, Request::Start {}) {
                            room.dispatch(&envelope.target,&envelope.request, Some(&context)).await
                        } else {
                            timeout(deadline,room.dispatch(&envelope.target,&envelope.request, Some(&context))).await.unwrap_or_else(|_|Err(Error::new(ErrorCode::Unknown,"operation deadline expired; the world outcome may be unknown")))
                        }
                    }=>result,
                    _=interrupted.changed(),if !killing=>Err(Error::new(ErrorCode::Unknown,"operation interrupted by room shutdown")),
                }
            };
            let phase = room.status().phase;
            if result.is_err() && matches!(phase, Phase::Starting | Phase::Stopping) {
                let failed_start =
                    phase == Phase::Starting && *interrupted.borrow() == interruption_revision;
                if failed_start {
                    let states = room.driver_states();
                    let trigger = room.failure_trigger(
                        FailureTriggerKind::Operation,
                        FailureStage::Startup,
                        result.as_ref().unwrap_err().code,
                        false,
                        Some(&context),
                    );
                    if let Err(error) = room.record_failure(trigger, &states, &BTreeSet::new()) {
                        result.as_mut().unwrap_err().details = json!({"persistence":error});
                    }
                    room.restart_required.store(true, Ordering::Release);
                }
                let cleanup = room.stop_inner(false, Some(&context)).await;
                if let Err(error) = &mut result {
                    error.details = json!({"cause":error.details,"cleanup":Outcome::from(cleanup)});
                }
                if failed_start {
                    room.set_phase(Phase::Failed, result.as_ref().err().cloned());
                }
            }
            if let Err(error) = kill_persistence {
                result = Err(error);
            }
            room.finish_operation(
                &operation,
                &context,
                observation,
                result.as_ref().map(|_| match &envelope.request {
                    Request::Start {} => "ready",
                    Request::UpdateMods { .. } => mod_download_outcome(&result),
                    _ => "completed",
                }),
            );
            room.record_admin_operation(&operation, &envelope, &result, &context);
            room.status.send_modify(|state| {
                if let Some(mut operation) = state.operations.current.take() {
                    operation.complete(&result);
                    state.operations.last = Some(operation);
                }
            });
            room.publish_status();
            room.clear_stop_intent(&operation.id);
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
        self.shutdown_priority_in_context(None).await
    }

    pub(crate) async fn shutdown_priority_in_context(
        self: &Arc<Self>,
        parent: Option<&OperationContext>,
    ) -> Result<StopResult> {
        {
            let _drivers = self.drivers.lock().unwrap();
            self.shutdown_pending.fetch_add(1, Ordering::AcqRel);
        }
        self.set_phase(Phase::Stopping, None);
        self.interrupt();
        let permit = self.mutation.clone().try_acquire_owned().ok();
        let room = self.clone();
        let mut context = parent.map_or_else(
            || OperationContext::new(Source::RoomApi, Layer::Room, None),
            |parent| parent.child(Layer::Room),
        );
        let (reply, receive) = oneshot::channel();
        tokio::spawn(async move {
            let permit = match permit {
                Some(permit) => Ok(permit),
                None => room.mutation.clone().acquire_owned().await,
            };
            let result = if let Ok(permit) = permit {
                let operation = context.status("stop", Target::Room);
                let observation = room.begin_operation(&operation, &mut context, "started");
                let result = room.stop_inner(false, Some(&context)).await;
                room.finish_operation(
                    &operation,
                    &context,
                    observation,
                    result.as_ref().map(|_| "completed"),
                );
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
        let health = self.all("health", json!({})).await;
        if health.is_empty()
            || !health.iter().all(
                |shard| matches!(&shard.result, Outcome::Success { value } if control_ready(value)),
            )
        {
            return Err(
                Error::new(ErrorCode::NotReady, "required shard control is unhealthy")
                    .with_details(json!(health)),
            );
        }
        self.wait_ready(Duration::from_secs(4), false).await?;
        value(health)
    }

    fn observe_monitored_shard(self: &Arc<Self>, driver: &Driver, state: &DriverState) {
        if self
            .drivers
            .lock()
            .unwrap()
            .get(&state.shard)
            .is_none_or(|current| !current.same_process(driver))
        {
            return;
        }
        self.refresh_shard(state);
        let phase = self.status.borrow().phase;
        if state.running || !matches!(phase, Phase::Starting | Phase::Running) {
            return;
        }
        let mut error = Error::new(
            ErrorCode::Transport,
            state.failure.as_ref().map_or_else(
                || "configured shard exited unexpectedly".into(),
                |error| error.message.clone(),
            ),
        )
        .with_details(json!({"shard":state.shard}));
        let stage = if phase == Phase::Starting {
            FailureStage::Startup
        } else {
            FailureStage::Runtime
        };
        self.restart_required.store(true, Ordering::Release);
        let trigger = self.failure_trigger(
            FailureTriggerKind::ProcessExited,
            stage,
            error.code,
            false,
            None,
        );
        let persisted = self.record_failure(
            trigger,
            &self.driver_states(),
            &BTreeSet::from([state.shard.clone()]),
        );
        if let Err(persistence) = &persisted {
            error.details = json!({"shard":state.shard,"persistence":persistence});
        }
        self.set_phase(Phase::Failed, Some(error.clone()));
        self.start_fault_cleanup(
            error,
            persisted,
            OperationContext::new(Source::Recovery, Layer::Room, None),
        );
    }

    async fn start_inner(self: &Arc<Self>, context: Option<&OperationContext>) -> Result<Value> {
        self.start_for(Duration::from_secs_f64(START_TIMEOUT), context)
            .await
    }

    async fn start_for(
        self: &Arc<Self>,
        duration: Duration,
        context: Option<&OperationContext>,
    ) -> Result<Value> {
        *self.runtime.lock().unwrap() = Some(tokio::runtime::Handle::current());
        self.ensure_open()
            .map_err(|_| Error::shutdown(ErrorCode::Unknown))?;
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
        self.drivers.lock().unwrap().clear();
        let attempt_id = self
            .status()
            .operations
            .current
            .map(|operation| operation.id)
            .unwrap_or_else(|| ulid::Ulid::generate().to_string());
        {
            let mut attempt = self.attempt.lock().unwrap();
            *attempt = None;
            self.update_recovery("started", context, |lock| {
                recovery::begin_run(lock, attempt_id.clone())
            })?;
            *attempt = Some(RunAttempt {
                id: attempt_id,
                evidence_lost: false,
            });
        }
        self.set_phase(Phase::Preparing, None);
        let result = self.prepare_and_start(duration).await;
        if matches!(result, Ok(StartOutcome::Cancelled)) {
            let cleanup = self.stop_inner(false, context).await;
            return Err(
                Error::new(ErrorCode::Unknown, "startup interrupted by room shutdown")
                    .with_details(json!({"cleanup":Outcome::from(cleanup)})),
            );
        }
        if let Err(mut error) = result {
            let stage = if self.status().phase == Phase::Preparing {
                FailureStage::Prepare
            } else {
                FailureStage::Startup
            };
            let trigger = self.failure_trigger(
                FailureTriggerKind::Operation,
                stage,
                error.code,
                false,
                context,
            );
            if let Err(persistence) =
                self.record_failure(trigger, &self.driver_states(), &BTreeSet::new())
            {
                error.details = json!({"cause":error.details,"persistence":persistence});
            }
            self.restart_required.store(true, Ordering::Release);
            let cleanup = self.stop_inner(false, context).await;
            error.details = json!({"cause":error.details,"cleanup":Outcome::from(cleanup)});
            self.set_phase(Phase::Failed, Some(error.clone()));
            return Err(error);
        }
        {
            let attempt = self.attempt.lock().unwrap();
            let attempt = attempt
                .as_ref()
                .ok_or_else(|| internal("room run attempt is missing"))?;
            self.update_recovery("ready", context, |lock| {
                recovery::mark_ready(lock, &attempt.id)
            })?;
        }
        self.set_phase(Phase::Running, None);
        value(self.status())
    }

    async fn prepare_and_start(self: &Arc<Self>, duration: Duration) -> Result<StartOutcome> {
        self.with_lock(|lock| {
            crate::mods::PreparedMods::prepare(lock, &self.executable, None)?;
            Ok(())
        })?;
        let cluster = configuration::discover(&self.directory).map_err(internal)?;
        crate::telemetry::validate_context(
            &self.telemetry.local.resource,
            &self.cluster_name,
            cluster.shards.iter().map(|shard| shard.name.as_str()),
        )?;
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
        self.set_phase(Phase::Starting, None);
        let options = self.options.lock().unwrap().clone();
        let nonce = self.with_lock(|lock| {
            options.save(lock, cluster.shards.iter().map(|shard| shard.name.as_str()))
        })?;
        for shard in &cluster.shards {
            let mut extra_args = options.extra_args.clone();
            if let Some(port) = self.ports.lock().unwrap().get(&shard.name) {
                extra_args.extend(["-external_port".into(), port.to_string().into()]);
            }
            let mut drivers = self.drivers.lock().unwrap();
            if self.ensure_open().is_err() {
                return Ok(StartOutcome::Cancelled);
            }
            let (sender, events) = DriverEvents::channel();
            let driver = Driver::spawn(
                &cluster,
                &self.executable,
                &shard.name,
                &nonce,
                extra_args,
                Some(sender),
            )
            .map_err(internal)?;
            drivers.insert(shard.name.clone(), driver.clone());
            self.monitor(shard.name.clone(), &driver, events);
        }
        self.wait_ready(duration, false)
            .await
            .map(|()| StartOutcome::Ready)
    }

    async fn wait_ready(&self, duration: Duration, verify_snapshot: bool) -> Result<()> {
        let deadline = Instant::now() + duration;
        let drivers = self.drivers();
        loop {
            for driver in drivers.values() {
                driver
                    .wait_ready(deadline.saturating_duration_since(Instant::now()))
                    .await
                    .map_err(driver_error)?;
            }
            let generations: Vec<_> = drivers
                .values()
                .map(|driver| driver.snapshot().generation)
                .collect();
            let connected = async {
                let remaining = || COMMAND.min(deadline.saturating_duration_since(Instant::now()));
                for driver in drivers.values() {
                    // Recovery needs runtime evidence for this generation; reload also verifies its snapshot.
                    let state = driver.snapshot();
                    let session = state
                        .runtime
                        .as_ref()
                        .and_then(|runtime| runtime["session_id"].as_str())
                        .filter(|session| !session.is_empty());
                    if verify_snapshot
                        || session.is_none()
                        || session != state.session_id.as_deref()
                    {
                        let runtime = driver
                            .request("runtime", json!({}), remaining())
                            .await
                            .map_err(driver_error)?;
                        if runtime["session_id"].as_str().is_none_or(str::is_empty) {
                            return Err(Error::new(
                                ErrorCode::Protocol,
                                "runtime has no world session",
                            ));
                        }
                    }
                }
                let mut ids = BTreeMap::new();
                let mut connections = Vec::with_capacity(drivers.len());
                for (name, driver) in &drivers {
                    let peers = driver
                        .request(
                            "connected_shards",
                            json!({"current_name":name}),
                            remaining(),
                        )
                        .await
                        .map_err(driver_error)?;
                    let peers = peers.as_array().ok_or_else(|| {
                        Error::new(ErrorCode::Protocol, "invalid native shard connections")
                    })?;
                    let mut current = peers.iter().filter(|peer| peer["is_current"] == true);
                    let id = current
                        .next()
                        .filter(|peer| peer["name"] == name.as_str())
                        .and_then(|peer| peer["id"].as_str())
                        .filter(|id| !id.is_empty() && current.next().is_none())
                        .ok_or_else(|| {
                            Error::new(
                                ErrorCode::Protocol,
                                "invalid current shard in native connections",
                            )
                        })?;
                    if ids.insert(id.to_owned(), name.clone()).is_some() {
                        return Err(Error::new(
                            ErrorCode::Conflict,
                            "shards report duplicate native identities",
                        ));
                    }
                    let ready: BTreeSet<_> = peers
                        .iter()
                        .filter(|peer| peer["ready"] == true)
                        .filter_map(|peer| peer["id"].as_str().map(str::to_owned))
                        .collect();
                    connections.push((name, driver, ready));
                }
                let mut connected = true;
                for (name, driver, ready) in connections {
                    let missing: Vec<_> = ids
                        .iter()
                        .filter(|(id, _)| !ready.contains(id.as_str()))
                        .map(|(_, name)| name.clone())
                        .collect();
                    connected &= missing.is_empty();
                    self.refresh_shard(&driver.snapshot());
                    self.status.send_modify(|status| {
                        if let Some(shard) =
                            status.shards.iter_mut().find(|shard| &shard.name == name)
                        {
                            shard.readiness.missing_shards = missing;
                        }
                    });
                }
                Ok(connected)
            }
            .await;
            let stable = drivers
                .values()
                .zip(generations)
                .all(|(driver, generation)| {
                    let state = driver.snapshot();
                    state.ready && state.generation == generation
                });
            match connected {
                Ok(true) if stable => return Ok(()),
                // Native snapshot synchronization can reload a shard during these read-only checks.
                Err(error)
                    if error.code == ErrorCode::NotReady
                        || error.code == ErrorCode::Unknown && !stable => {}
                Err(error) => return Err(error),
                _ => {}
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

    async fn stop_inner(
        &self,
        force: bool,
        context: Option<&OperationContext>,
    ) -> Result<StopResult> {
        let before = self.driver_states();
        let capture = if force {
            self.restart_required.store(true, Ordering::Release);
            self.record_failure(
                self.failure_trigger(
                    FailureTriggerKind::ForcedStop,
                    FailureStage::Shutdown,
                    ErrorCode::Unknown,
                    true,
                    context,
                ),
                &before,
                &before.keys().cloned().collect(),
            )
        } else {
            self.merge_failure(&before)
        };
        self.set_phase(Phase::Stopping, None);
        let drivers = self.drivers();
        let service = *self.service_shutdown.lock().unwrap();
        let stopped = join_all(drivers.into_iter().map(|(name, driver)| {
            let before = &before;
            async move {
                let report = if force {
                    driver.kill().await
                } else if let Some(service) = service {
                    let native = (Instant::now() + Duration::from_secs_f64(STOP_TIMEOUT)).min(service.native);
                    let terminate = (native + Duration::from_secs(10)).min(service.terminate);
                    driver.stop_until(crate::driver::StopDeadlines { native, terminate }).await
                } else {
                    driver.stop(Duration::from_secs_f64(STOP_TIMEOUT)).await
                };
                let after = driver.snapshot();
                let stopped = after.exit.clone();
                let mut result = report.map_err(driver_error).and_then(|report| {
                    if !report.output_drained {
                        return Err(Error::new(ErrorCode::Transport, "game output did not drain"));
                    }
                    if !force {
                        if let Some(error) = &report.protocol_error {
                            return Err(Error::new(ErrorCode::Protocol, error));
                        }
                        if let Some(error) = after.failure.clone() {
                            return Err(driver_error(error));
                        }
                        if !report.status.success() || report.forced {
                            return Err(Error::new(ErrorCode::Transport, "game process did not exit cleanly"));
                        }
                    }
                    Ok(ShardStopped::from(&report))
                });
                if let Err(error) = &mut result {
                    self.restart_required.store(true, Ordering::Release);
                    let trigger = self.failure_trigger(FailureTriggerKind::Operation, FailureStage::Shutdown, error.code, force, context);
                    let persisted = self.record_failure(trigger, before, &BTreeSet::from([name.clone()]));
                    error.details = json!({"stopped":stopped,"force_requested":force,"cause":error.details,"persistence_error":persisted.err()});
                }
                self.refresh_shard(&after);
                ShardResult { shard: name, result: result.into() }
            }
        })).await;
        // Every retained report is final now, including failures from sibling cleanup.
        let persisted = capture.and(self.finish_failure_after(Some(&before)));
        let failure = stopped
            .iter()
            .any(|result| matches!(result.result, Outcome::Failure { .. }));
        let result = StopResult {
            force_requested: force,
            shards: stopped,
        };
        if failure || persisted.is_err() {
            self.restart_required.store(true, Ordering::Release);
            let error = match persisted {
                Err(error) => Error::new(
                    ErrorCode::Internal,
                    format!("failed to persist room cleanup: {error}"),
                ),
                Ok(()) => Error::new(
                    ErrorCode::PartialFailure,
                    "some shard processes did not stop cleanly",
                ),
            }
            .with_details(value(&result)?);
            self.set_phase(Phase::Failed, Some(error.clone()));
            Err(error)
        } else {
            self.set_phase(Phase::Stopped, None);
            Ok(result)
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
    async fn all(&self, method: &str, args: Value) -> Vec<ShardResult> {
        let names: Vec<_> = self
            .cluster
            .lock()
            .unwrap()
            .shards
            .iter()
            .map(|shard| shard.name.clone())
            .collect();
        join_all(names.into_iter().map(|name| {
            let args = args.clone();
            async move {
                ShardResult {
                    result: self.native(&name, method, args).await.into(),
                    shard: name,
                }
            }
        }))
        .await
    }
    async fn update_mods(
        self: &Arc<Self>,
        notice: &Option<Countdown>,
        restart: bool,
        mut cancelled: watch::Receiver<u64>,
        context: &OperationContext,
        duration: Duration,
    ) -> Result<Value> {
        let deadline = Instant::now() + duration;
        tokio::select! {
            _ = self.countdown(notice, Some(context)) => {},
            _ = cancelled.changed() => return Err(Error::shutdown(ErrorCode::Unknown)),
            _ = tokio::time::sleep_until(deadline) => return Err(Error::new(ErrorCode::Unknown,"Mod operation deadline expired before stopping")),
        };
        tokio::time::timeout_at(deadline, self.stop_inner(false, Some(context)))
            .await
            .map_err(|_| {
                Error::new(
                    ErrorCode::Unknown,
                    "Mod operation deadline expired while stopping",
                )
            })??;
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
        let proxy = crate::mods::proxy_from_env()?;
        let prepared = self.with_lock(|lock| {
            Ok(crate::mods::PreparedMods::prepare(
                lock,
                &self.executable,
                proxy,
            )?)
        })?;
        let (cancel, local_cancelled) = watch::channel(*cancelled.borrow());
        let process = {
            let _drivers = self.drivers.lock().unwrap();
            self.ensure_open()
                .map_err(|_| Error::shutdown(ErrorCode::Unknown))?;
            if cancelled.has_changed().unwrap_or(true) {
                return Err(Error::shutdown(ErrorCode::Unknown));
            }
            if Instant::now() >= deadline {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "Mod operation deadline expired before download",
                ));
            }
            prepared.spawn_update()?
        };
        let emit = |mut log: LogEvent| {
            log.attributes
                .insert("dst.cluster.name".into(), json!(self.cluster_name));
            log.correlate(context);
            self.telemetry.publish(&log, StreamKind::Logs);
        };
        let update = prepared.finish_update(process, &emit, local_cancelled);
        tokio::pin!(update);
        let result = tokio::select! {
            result = &mut update => result,
            _ = cancelled.changed() => { cancel.send_modify(|revision| *revision = revision.wrapping_add(1)); update.await },
            _ = tokio::time::sleep_until(deadline) => { cancel.send_modify(|revision| *revision = revision.wrapping_add(1)); let _ = update.await; Err(Error::new(ErrorCode::Unknown, "Mod operation deadline expired after downloader cleanup")) },
        }?;
        if cancelled.has_changed().unwrap_or(true) {
            return Err(Error::new(
                ErrorCode::Unknown,
                "Mod maintenance interrupted before restarting",
            ));
        }
        if restart {
            self.ensure_open()
                .map_err(|_| Error::shutdown(ErrorCode::Unknown))?;
            tokio::select! { started=tokio::time::timeout_at(deadline, self.start_inner(Some(context)))=>{started.map_err(|_|Error::new(ErrorCode::Unknown,"Mod operation deadline expired while restarting"))??;},_=cancelled.changed()=>return Err(Error::new(ErrorCode::Unknown,"Mod maintenance interrupted while restarting")) }
        }
        Ok(result)
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

    async fn set_admin(&self, userid: &str, enabled: bool) -> Result<Value> {
        self.require_ready()?;
        let users = self.with_lock(|lock| crate::permissions::set_admin(lock, userid, enabled))?;
        let shards = self.all("reload_permissions", json!({})).await;
        if !shards.iter().all(|shard| {
            matches!(
                shard.result,
                Outcome::Success {
                    value: Value::Bool(true)
                }
            )
        }) {
            return Err(Error::new(
                ErrorCode::PartialFailure,
                "admin list was updated but some shards did not reload permissions",
            )
            .with_details(json!({"adminlist":users, "shards":shards})));
        }
        Ok(Value::Null)
    }

    async fn dispatch(
        self: &Arc<Self>,
        target: &Target,
        request: &Request,
        context: Option<&OperationContext>,
    ) -> Result<Value> {
        if crate::permissions::supports_offline(request) && self.require_stopped().is_ok() {
            return self
                .with_lock(|lock| crate::permissions::apply(&mut lock.while_stopped(), request));
        }
        if request.mutating()
            && !matches!(
                request,
                Request::Start {}
                    | Request::Stop { .. }
                    | Request::Kill {}
                    | Request::Restart { .. }
                    | Request::UpdateMods { .. }
                    | Request::Configure { .. }
                    | Request::EditConfiguration { .. }
                    | Request::SetPolicy { .. }
                    | Request::SetAdmin { .. }
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
            Request::Start {} => self.start_inner(context).await,
            Request::Stop { notice } => {
                self.countdown(notice, context).await;
                value(self.stop_inner(false, context).await?)
            }
            Request::Kill {} => value(self.stop_inner(true, context).await?),
            Request::Restart { notice } => {
                self.countdown(notice, context).await;
                self.stop_inner(false, context).await?;
                self.start_inner(context).await
            }
            Request::ReadConfiguration {} => self.read_configuration(),
            Request::ReadPermissions {} => self.with_lock(|lock| crate::permissions::read(lock)),
            Request::IsAdmin { userid } => self.with_lock(|lock| {
                Ok(json!(crate::permissions::contains(
                    lock,
                    "adminlist.txt",
                    userid
                )?))
            }),
            Request::SetAdmin { userid, enabled } => self.set_admin(userid, *enabled).await,
            Request::Configure {
                configuration,
                replace_permissions,
            } => {
                let cluster = crate::settings::ClusterConfig::from_value(configuration.clone())
                    .map_err(|e| Error::invalid("configuration", e.to_string()))?;
                Ok(json!({"changed": self.configure(&cluster, *replace_permissions)?}))
            }
            Request::EditConfiguration { changes, unset } => {
                self.edit_configuration(changes, unset)
            }
            Request::SetPolicy { policy } => {
                let policy: crate::policy::Policy = serde_json::from_value(policy.clone())
                    .map_err(|e| Error::invalid("policy", e.to_string()))?;
                policy
                    .validate()
                    .map_err(|e| Error::invalid("policy", e.to_string()))?;
                self.with_lock(|lock| {
                    policy.save(lock)?;
                    Ok(json!(policy))
                })
            }
            Request::Save {} => self.save().await,
            Request::Reset {}
            | Request::Rollback { .. }
            | Request::RollbackToDay { .. }
            | Request::RollbackToSnapshot { .. }
            | Request::Regenerate { .. }
            | Request::RegenerateShard { .. } => self.reload(target, request).await,
            Request::Snapshots { limit, before } => {
                let name = match target {
                    Target::Shard(name) => name.clone(),
                    Target::Room => self.master(),
                };
                self.snapshots(&name, *limit, *before).await
            }
            Request::Execute { .. } => {
                let Target::Shard(name) = target else {
                    unreachable!()
                };
                let result = self.native(name, "evaluate", request.arguments()?).await?;
                console_output(result)
            }
            Request::ExecuteAll { .. } => {
                let mut shards = self.all("evaluate", request.arguments()?).await;
                for shard in &mut shards {
                    if let Outcome::Success { value } = &mut shard.result {
                        shard.result = console_output(value.take()).into();
                    }
                }
                shard_results(shards)
            }
            Request::ConnectedShards {} => {
                let Target::Shard(name) = target else {
                    unreachable!()
                };
                self.native(name, "connected_shards", json!({"current_name":name}))
                    .await
            }
            Request::GetPlayer { .. }
            | Request::Inventory { .. }
            | Request::SetVitals { .. }
            | Request::KillPlayer { .. }
            | Request::Revive { .. }
            | Request::Despawn { .. }
            | Request::Migrate { .. }
            | Request::Teleport { .. }
            | Request::Give { .. }
            | Request::Remove { .. } => self.player_request(target, request).await,
            Request::Kick { userid } => self.kick(userid).await,
            Request::VoteCommands {}
            | Request::GetVote {}
            | Request::StartVote { .. }
            | Request::CancelVote {} => {
                let master = self.master();
                let mut args = request.arguments()?;
                if let Request::StartVote { starter_userid, .. } = request {
                    self.check_remote_vote_access(&master, starter_userid, &mut args)
                        .await?;
                }
                vote_result(self.native(&master, request.method(), args).await?)
            }
            Request::Announce {
                message,
                count,
                interval,
            } => self.announce(message, *count, *interval).await,
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
            Request::UpdateMods { .. } => {
                unreachable!("Mod updates are owned by the retained room worker")
            }

            Request::ExecuteJson { .. }
            | Request::Evaluate { .. }
            | Request::Pause { .. }
            | Request::Health {}
            | Request::Presence {}
            | Request::Room {}
            | Request::World {}
            | Request::Runtime {}
            | Request::Mods {}
            | Request::ListPlayers {} => match target {
                Target::Shard(name) => {
                    self.native(name, request.method(), request.arguments()?)
                        .await
                }
                Target::Room => {
                    shard_results(self.all(request.method(), request.arguments()?).await)
                }
            },
        }
    }
}

fn identity(state: &DriverState) -> Result<SessionIdentity> {
    Ok(SessionIdentity {
        session_id: state.session_id.clone().ok_or_else(|| {
            Error::new(ErrorCode::NotReady, "native world session is unavailable")
        })?,
        process_id: state.process_id.clone(),
        generation: state
            .generation
            .ok_or_else(|| Error::new(ErrorCode::NotReady, "Lua generation is unavailable"))?,
    })
}
fn value(value: impl Serialize) -> Result<Value> {
    serde_json::to_value(value).map_err(internal)
}

fn shard_results(shards: Vec<ShardResult>) -> Result<Value> {
    if shards
        .iter()
        .any(|shard| matches!(shard.result, Outcome::Failure { .. }))
    {
        return Err(Error::new(
            ErrorCode::PartialFailure,
            "one or more shard requests failed",
        )
        .with_details(json!({"shards": shards})));
    }
    value(shards)
}

fn console_output(mut evaluated: Value) -> Result<Value> {
    if !evaluated["error"].is_null() {
        let message = evaluated["error"]["message"]
            .as_str()
            .unwrap_or("Lua evaluation failed");
        return Err(Error::new(ErrorCode::Lua, message).with_details(evaluated));
    }
    let output = evaluated
        .get_mut("output")
        .filter(|output| output.is_string())
        .ok_or_else(|| {
            Error::new(
                ErrorCode::Protocol,
                "Lua evaluation returned no console output",
            )
        })?;
    Ok(output.take())
}

fn vote_result(result: Value) -> Result<Value> {
    if let Some(error) = result.get("error") {
        return Err(serde_json::from_value(error.clone()).map_err(internal)?);
    }
    Ok(result)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn login_timeout_preserves_cause_and_cleanup_exit_before_retry_is_reserved() {
        use std::{fs, os::unix::fs::PermissionsExt};
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("cluster");
        fs::create_dir_all(root.join("Master")).unwrap();
        fs::write(root.join("cluster.ini"), "").unwrap();
        fs::write(root.join("Master/server.ini"), "[SHARD]\nis_master=true\n").unwrap();
        let game = directory.path().join("game.py");
        fs::write(&game, r#"#!/usr/bin/env python3
import os
os.write(1, b'CURL ERROR: (login.kleientertainment.com) [6]Could not resolve host: login.kleientertainment.com\n[0] Account Failed (6): ""\n')
while os.read(3, 4096) != b'c_shutdown()\n': pass
raise SystemExit(7)
"#).unwrap();
        fs::set_permissions(&game, fs::Permissions::from_mode(0o700)).unwrap();
        let room = Room::open(
            &root,
            game,
            DriverOptions::default(),
            EventHub::default(),
            Default::default(),
        )
        .unwrap();
        let error = room
            .start_for(Duration::from_millis(150), None)
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Timeout);
        room.with_lock(|lock| {
            let state = recovery::load(lock)?;
            let record = state.last_failure.unwrap();
            assert_eq!(record.cause, recovery::FailureCause::LoginTransport);
            assert_eq!(record.trigger.code, Some(ErrorCode::Timeout));
            assert_eq!(record.trigger.stage, FailureStage::Startup);
            assert!(
                record.complete && matches!(record.classification, recovery::Failure::Retryable)
            );
            let exit = record.shards["Master"].exit.as_ref().unwrap();
            assert_eq!(exit.returncode, Some(7));
            assert!(!exit.forced && exit.output_drained);
            let required = BTreeSet::from(["Master".into()]);
            let catalogs = BTreeMap::from([(
                "Master".into(),
                recovery::Catalog {
                    session_id: "SESSION".into(),
                    latest_world_file: "session/SESSION/0000000001".into(),
                    snapshots: vec![recovery::Snapshot {
                        snapshot_id: 1,
                        world_file: Some("session/SESSION/0000000001".into()),
                    }],
                },
            )]);
            let decision =
                recovery::on_failure(lock, record.classification, &required, &catalogs, 100)?;
            assert!(matches!(
                decision,
                recovery::Decision::RetryCurrent { attempt: 1, .. }
            ));
            assert!(!recovery::load_run(lock)?.active);
            assert_eq!(
                recovery::load(lock)?.last_failure.unwrap().cause,
                recovery::FailureCause::LoginTransport
            );
            Ok(())
        })
        .unwrap();
    }
}
