//! Persistent shard control. Written commands keep the native channel until their reply or deadline.

mod protocol;
use protocol::handle_line;

use std::{
    collections::BTreeMap,
    ffi::OsString,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use anyhow::{Context, Result as SetupResult, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    sync::{Mutex as AsyncMutex, Semaphore, mpsc, oneshot, watch},
    time::{Instant, sleep, timeout_at},
};
use ulid::Ulid;

use crate::{
    bounded,
    configuration::Cluster,
    events::{JsonPosition, RejectionReason},
    files::RoomLock,
    process::{
        EventKind, ExitReport, ExitSummary, GameProcess, MAX_PROTOCOL_LINE_BYTES,
        MAX_REQUEST_BYTES, NativeDiagnostics, ProcessOutput, ProcessSpec, StreamKind,
        native_message, write_request,
    },
};

const MAX_PENDING: usize = 64;
const RELAY_SLOTS: usize = 1024;
const RELAY_BYTES: usize = 8 * 1024 * 1024;
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize, clap::ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum TelemetryProfile {
    Off,
    Timeline,
    #[default]
    Play,
}

#[derive(Clone, Debug)]
pub struct DriverOptions {
    pub profile: TelemetryProfile,
    /// Optional recovery request to run before loading the world.
    pub control: Value,
    pub extra_args: Vec<OsString>,
}

impl Default for DriverOptions {
    fn default() -> Self {
        Self {
            profile: TelemetryProfile::default(),
            control: json!({}),
            extra_args: vec!["-skip_update_server_mods".into()],
        }
    }
}

impl DriverOptions {
    /// The shards about to launch read the same nonce and options on every Lua reload.
    pub fn save<'a>(
        &self,
        room: &RoomLock,
        shards: impl IntoIterator<Item = &'a str>,
    ) -> SetupResult<String> {
        validate_control(&self.control)?;
        let nonce = Ulid::generate().to_string();
        let encoded = serde_json::to_vec(&json!({
            "driver": {"nonce": nonce, "profile": self.profile, "control": self.control}
        }))?;
        ensure!(
            encoded.len() < MAX_PROTOCOL_LINE_BYTES,
            "driver options exceed the native size limit"
        );
        for shard in shards {
            room.write_driver_options(shard, &encoded)?;
        }
        Ok(nonce)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DriverErrorCode {
    InvalidRequest,
    NotReady,
    Busy,
    Unsupported,
    NotFound,
    StaleReference,
    Lua,
    Unknown,
    Timeout,
    Transport,
    Protocol,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlFailureReason {
    Json,
    Identity,
    Generation,
    Bootstrap,
    Rpc,
    Control,
    Lifecycle,
    Length,
    Transport,
    InvalidFrame,
}

impl ControlFailureReason {
    fn error(self, message: impl Into<String>) -> DriverError {
        let code = match self {
            Self::Transport => DriverErrorCode::Transport,
            _ => DriverErrorCode::Protocol,
        };
        DriverError {
            reason: Some(self),
            ..DriverError::new(code, message)
        }
    }
}

#[derive(Clone, Debug, Serialize, thiserror::Error)]
#[error("{code:?}: {message}")]
pub struct DriverError {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<ControlFailureReason>,
    pub code: DriverErrorCode,
    pub message: String,
    pub request_id: Option<String>,
    pub written: bool,
    pub accepted: bool,
}

impl DriverError {
    fn new(code: DriverErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            reason: None,
            message: message.into(),
            request_id: None,
            written: false,
            accepted: false,
        }
    }
}

pub type DriverResult<T> = Result<T, DriverError>;

#[derive(Clone, Debug, Serialize)]
pub struct RequestRecord {
    pub id: String,
    pub method: String,
    pub generation: u64,
    pub written: bool,
    pub accepted: bool,
    pub completed: bool,
    pub error: Option<DriverError>,
}

#[derive(Clone, Debug, Serialize)]
pub struct DriverState {
    pub shard: String,
    pub pid: u32,
    pub process_id: String,
    pub nonce: String,
    pub generation: Option<u64>,
    pub generation_changes: u64,
    pub native_ready: bool,
    pub ready: bool,
    pub running: bool,
    pub stopping: bool,
    pub exit: Option<ExitSummary>,
    pub native_diagnostics: NativeDiagnostics,
    pub load_failure: Option<Value>,
    pub session_id: Option<String>,
    pub health: Option<Value>,
    pub runtime: Option<Value>,
    pub last_native_save: Option<Value>,
    pub last_control: Option<Value>,
    /// Required evidence in this generation, including repeated acknowledgements.
    pub control_count: u64,
    /// Driver control errors remain separate from the Process exit report.
    pub failure: Option<DriverError>,
    pub telemetry_sequence: u64,
    pub telemetry_gaps: u64,
    pub invalid_telemetry: u64,
    pub stale_telemetry: u64,
    pub telemetry_rejections: BTreeMap<RejectionReason, u64>,
    pub last_telemetry_rejection: Option<TelemetryRejection>,
    pub unmatched_replies: u64,
    pub recent_request: Option<RequestRecord>,
}

#[derive(Clone, Debug, Serialize)]
pub struct TelemetryRejection {
    pub source: StreamKind,
    pub bytes: Option<usize>,
    pub reason: RejectionReason,
    pub position: Option<JsonPosition>,
}

impl DriverState {
    pub fn login_retry_permitted(&self) -> bool {
        self.native_diagnostics.login_transport.is_some()
            && self.generation.is_none()
            && !self.native_ready
            && self.runtime.is_none()
            && self.session_id.is_none()
            && self.last_native_save.is_none()
    }

    fn reject_telemetry(
        &mut self,
        source: StreamKind,
        bytes: Option<usize>,
        reason: RejectionReason,
        position: Option<JsonPosition>,
    ) {
        if matches!(
            reason,
            RejectionReason::Generation | RejectionReason::Sequence
        ) {
            self.stale_telemetry += 1;
        } else {
            self.invalid_telemetry += 1;
        }
        *self.telemetry_rejections.entry(reason).or_default() += 1;
        self.last_telemetry_rejection = Some(TelemetryRejection {
            source,
            bytes,
            reason,
            position,
        });
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", content = "data", rename_all = "snake_case")]
pub enum DriverEventKind {
    Lifecycle(Value),
    Control(Value),
    Telemetry(Box<crate::events::GameEvent>),
    Log(Value),
    Diagnostic(Value),
}

#[derive(Clone, Debug, Serialize)]
pub struct DriverEvent {
    pub id: String,
    #[serde(flatten)]
    pub kind: DriverEventKind,
    pub shard: String,
    pub nonce: String,
    pub observed_timestamp_ns: u64,
    pub generation: Option<u64>,
}

pub struct DriverEventSender {
    events: bounded::Sender<DriverEvent>,
    dropped: Arc<AtomicU64>,
}

/// Events from one game process, including all of its world generations.
pub struct DriverEvents {
    events: mpsc::Receiver<bounded::Item<DriverEvent>>,
    dropped: Arc<AtomicU64>,
}

impl DriverEvents {
    /// Create the event sender to pass when starting one shard.
    pub fn channel() -> (DriverEventSender, Self) {
        let (tx, events) = bounded::channel(RELAY_SLOTS, RELAY_BYTES);
        let dropped = Arc::new(AtomicU64::new(0));
        (
            DriverEventSender {
                events: tx,
                dropped: dropped.clone(),
            },
            Self { events, dropped },
        )
    }

    pub async fn recv(&mut self) -> Option<DriverEvent> {
        self.events.recv().await.map(bounded::Item::into_inner)
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    pub(crate) fn drop_counter(&self) -> Arc<AtomicU64> {
        self.dropped.clone()
    }
}

struct Pending {
    record: RequestRecord,
    result: Option<DriverResult<Value>>,
    response: Option<oneshot::Sender<DriverResult<Value>>>,
    synchronized: bool,
}

impl Pending {
    fn error(&self, code: DriverErrorCode, message: &str) -> DriverError {
        DriverError {
            code,
            reason: None,
            message: message.into(),
            request_id: Some(self.record.id.clone()),
            written: self.record.written,
            accepted: self.record.accepted,
        }
    }

    fn deliver(&mut self, result: DriverResult<Value>) {
        self.record.completed = true;
        self.record.error = result.as_ref().err().cloned();
        if let Some(response) = self.response.take() {
            let _ = response.send(result);
        }
    }
}

struct Shared {
    state: DriverState,
    stop_requested: bool,
    // Lua reports a local epoch; its public identity is allocated here.
    observation: Option<(u64, String)>,
    // Only one native command may be awaiting its uncorrelated Done/Busy marker.
    active: Option<Pending>,
}

type StopCompletion = Option<DriverResult<ExitReport>>;

#[derive(Clone, Copy)]
pub(crate) struct StopDeadlines {
    pub native: Instant,
    pub terminate: Instant,
}

struct Stopping {
    completion: watch::Receiver<StopCompletion>,
    deadlines: watch::Sender<StopDeadlines>,
}

impl Stopping {
    fn tighten(&self, deadlines: StopDeadlines) {
        self.deadlines.send_if_modified(|current| {
            let native = current.native.min(deadlines.native);
            let terminate = current.terminate.min(deadlines.terminate);
            let changed = native != current.native || terminate != current.terminate;
            *current = StopDeadlines { native, terminate };
            changed
        });
    }
}

struct Inner {
    process: GameProcess,
    shared: Mutex<Shared>,
    snapshots: watch::Sender<DriverState>,
    native_command: Arc<AsyncMutex<()>>,
    slots: Arc<Semaphore>,
    pump_finished: watch::Receiver<bool>,
    stop: Mutex<Option<Stopping>>,
}

impl Inner {
    fn change<T>(&self, change: impl FnOnce(&mut Shared) -> T) -> T {
        let mut shared = self.shared.lock().expect("driver state lock");
        let result = change(&mut shared);
        shared.state.native_diagnostics = self.process.native_diagnostics();
        self.snapshots.send_replace(shared.state.clone());
        result
    }

    fn snapshot(&self) -> DriverState {
        let mut state = self.shared.lock().expect("driver state lock").state.clone();
        state.native_diagnostics = self.process.native_diagnostics();
        state
    }

    fn fail(&self, error: DriverError) {
        self.change(|shared| {
            shared.state.failure.get_or_insert(error);
            shared.state.ready = false;
            if let Some(active) = &mut shared.active {
                active.deliver(Err(active.error(
                    DriverErrorCode::Unknown,
                    "control failed after command dispatch began",
                )));
                shared.state.recent_request = Some(active.record.clone());
            }
        });
        self.process.request_kill();
    }

    async fn waited(&self) -> DriverResult<ExitReport> {
        let report = self
            .process
            .wait()
            .await
            .map_err(|error| DriverError::new(DriverErrorCode::Transport, error.to_string()))?;
        let mut finished = self.pump_finished.clone();
        while !*finished.borrow_and_update() {
            finished.changed().await.map_err(|_| {
                DriverError::new(
                    DriverErrorCode::Transport,
                    "driver pump ended without completing",
                )
            })?;
        }
        Ok(report)
    }
}

struct Handle {
    inner: Arc<Inner>,
}
impl Drop for Handle {
    fn drop(&mut self) {
        self.inner.process.request_kill();
    }
}

#[derive(Clone)]
pub struct Driver {
    handle: Arc<Handle>,
}

impl Driver {
    /// Launch a shard with optional event delivery. Control-only callers can pass `None`.
    pub fn spawn(
        cluster: &Cluster,
        executable: &Path,
        shard: &str,
        nonce: &str,
        extra_args: Vec<OsString>,
        events: Option<DriverEventSender>,
    ) -> SetupResult<Self> {
        ensure!(
            cluster
                .shards
                .iter()
                .any(|candidate| candidate.name == shard),
            "shard is not part of the validated cluster"
        );
        let executable = executable
            .canonicalize()
            .context("resolve game executable")?;
        let mut args: Vec<OsString> = vec![
            "-persistent_storage_root".into(),
            cluster
                .directory
                .parent()
                .context("cluster has no parent")?
                .into(),
            "-conf_dir".into(),
            ".".into(),
            "-cluster".into(),
            cluster
                .directory
                .file_name()
                .context("cluster has no name")?
                .into(),
            "-shard".into(),
            shard.into(),
            "-ugc_directory".into(),
            cluster.directory.join("mods/ugc").into(),
            "-monitor_parent_process".into(),
            std::process::id().to_string().into(),
        ];
        args.extend(extra_args);
        args.push("-cloudserver".into());
        let cwd = executable
            .parent()
            .context("game executable has no directory")?
            .to_owned();
        Self::attach(
            ProcessSpec {
                program: executable,
                args,
                cwd,
            },
            nonce.to_owned(),
            shard.to_owned(),
            events,
        )
    }

    /// Attach the same driver to an explicit launch command, useful for isolated tests.
    pub fn attach(
        spec: ProcessSpec,
        nonce: String,
        shard: String,
        events: Option<DriverEventSender>,
    ) -> SetupResult<Self> {
        ensure!(
            Ulid::from_string(&nonce).is_ok_and(|id| id.to_string() == nonce),
            "nonce must be a canonical ULID"
        );
        ensure!(
            !shard.is_empty() && shard.len() <= 255 && !shard.contains(['\0', '\r', '\n']),
            "invalid shard name"
        );
        let (process, output) = GameProcess::spawn(spec)?;
        Ok(Self::start_pump(process, output, nonce, shard, events))
    }

    fn start_pump(
        process: GameProcess,
        output: ProcessOutput,
        nonce: String,
        shard: String,
        events: Option<DriverEventSender>,
    ) -> Self {
        let state = DriverState {
            shard,
            pid: process.pid(),
            process_id: Ulid::generate().to_string(),
            nonce,
            generation: None,
            generation_changes: 0,
            native_ready: false,
            ready: false,
            running: true,
            stopping: false,
            exit: None,
            native_diagnostics: NativeDiagnostics::default(),
            load_failure: None,
            session_id: None,
            health: None,
            runtime: None,
            last_native_save: None,
            last_control: None,
            control_count: 0,
            failure: None,
            telemetry_sequence: 0,
            telemetry_gaps: 0,
            invalid_telemetry: 0,
            stale_telemetry: 0,
            telemetry_rejections: BTreeMap::new(),
            last_telemetry_rejection: None,
            unmatched_replies: 0,
            recent_request: None,
        };
        let (snapshots, _) = watch::channel(state.clone());
        let (finished, pump_finished) = watch::channel(false);
        let inner = Arc::new(Inner {
            process,
            shared: Mutex::new(Shared {
                state,
                stop_requested: false,
                observation: None,
                active: None,
            }),
            snapshots,
            native_command: Arc::new(AsyncMutex::new(())),
            slots: Arc::new(Semaphore::new(MAX_PENDING)),
            pump_finished,
            stop: Mutex::new(None),
        });
        let pump = Pump {
            inner: inner.clone(),
            events,
        };
        tokio::spawn(pump.run(output, finished));
        Self {
            handle: Arc::new(Handle { inner }),
        }
    }

    pub fn snapshot(&self) -> DriverState {
        self.handle.inner.snapshot()
    }
    /// Control and loss changes notify; a contiguous telemetry sequence only updates the value.
    pub fn watch(&self) -> watch::Receiver<DriverState> {
        self.handle.inner.snapshots.subscribe()
    }
    pub(crate) fn same_process(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.handle, &other.handle)
    }

    pub fn pid(&self) -> u32 {
        self.handle.inner.process.pid()
    }

    pub fn output_stats(&self) -> crate::process::OutputStats {
        self.handle.inner.process.stats()
    }

    pub fn request_kill(&self) {
        self.handle.inner.process.request_kill();
    }

    pub(crate) fn request_shutdown(&self, deadline: Instant) {
        self.handle.inner.process.request_shutdown(deadline);
    }

    pub(crate) fn tighten_stop(&self, deadlines: StopDeadlines) {
        if let Some(stop) = self
            .handle
            .inner
            .stop
            .lock()
            .expect("stop state lock")
            .as_ref()
        {
            stop.tighten(deadlines);
        }
    }

    pub async fn wait_ready(&self, duration: Duration) -> DriverResult<DriverState> {
        let deadline = deadline(duration)?;
        let mut state = self.watch();
        loop {
            let snapshot = state.borrow_and_update().clone();
            if snapshot.ready {
                return Ok(snapshot);
            }
            if !snapshot.running || snapshot.failure.is_some() {
                return Err(DriverError::new(
                    DriverErrorCode::NotReady,
                    snapshot
                        .failure
                        .map_or_else(|| "game process exited".into(), |error| error.message),
                ));
            }
            timeout_at(deadline, state.changed())
                .await
                .map_err(|_| {
                    DriverError::new(
                        DriverErrorCode::Timeout,
                        "driver readiness deadline exceeded",
                    )
                })?
                .map_err(|_| DriverError::new(DriverErrorCode::Transport, "driver state closed"))?;
        }
    }

    /// Cancellation discards an unsent command. After writing, the Driver owns its completion.
    pub async fn request(
        &self,
        method: &str,
        arguments: Value,
        duration: Duration,
    ) -> DriverResult<Value> {
        let deadline = deadline(duration)?;
        if !valid_method(method) || !arguments.is_object() {
            return Err(DriverError::new(
                DriverErrorCode::InvalidRequest,
                "method must be an identifier and arguments an object",
            ));
        }
        let state = self.snapshot();
        if !state.ready {
            return Err(DriverError::new(
                DriverErrorCode::NotReady,
                "shard control is not ready",
            ));
        }
        let generation = state
            .generation
            .ok_or_else(|| DriverError::new(DriverErrorCode::NotReady, "generation is unknown"))?;
        let id = Ulid::generate().to_string();
        let frame = format!(
            "DST_RPC|{}\n",
            json!({"nonce": state.nonce, "id": id,
            "generation": generation, "method": method, "arguments": arguments})
        )
        .into_bytes();
        if frame.len() > MAX_REQUEST_BYTES {
            return Err(DriverError::new(
                DriverErrorCode::InvalidRequest,
                "encoded request exceeds 4096 bytes",
            ));
        }
        let permit = self
            .handle
            .inner
            .slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| {
                DriverError::new(
                    DriverErrorCode::Busy,
                    "64 shard requests are already pending",
                )
            })?;
        let record = RequestRecord {
            id,
            method: method.into(),
            generation,
            written: false,
            accepted: false,
            completed: false,
            error: None,
        };
        let inner = self.handle.inner.clone();
        let native = timeout_at(deadline, inner.native_command.clone().lock_owned())
            .await
            .map_err(|_| {
                DriverError::new(DriverErrorCode::Timeout, "request expired before dispatch")
            })?;
        let pending = dispatch(&inner, &record, &frame, deadline).await?;
        let (mut response, receive) = oneshot::channel();
        tokio::spawn(async move {
            let _permits = (permit, native);
            let result = finish(&inner, &record, &frame, deadline, pending, &mut response).await;
            let _ = response.send(result);
        });
        receive
            .await
            .map_err(|_| DriverError::new(DriverErrorCode::Transport, "request worker stopped"))?
    }

    pub async fn wait(&self) -> DriverResult<ExitReport> {
        self.handle.inner.waited().await
    }

    pub async fn kill(&self) -> DriverResult<ExitReport> {
        self.handle.inner.process.request_kill();
        self.wait().await
    }

    /// Native saving shutdown, followed by SIGTERM and SIGKILL when its deadline expires.
    /// The shared worker continues even when every stop caller cancels its wait.
    pub async fn stop(&self, grace: Duration) -> DriverResult<ExitReport> {
        let native = deadline(grace)?;
        let terminate = native.checked_add(Duration::from_secs(10)).ok_or_else(|| {
            DriverError::new(DriverErrorCode::InvalidRequest, "timeout is too large")
        })?;
        self.stop_until(StopDeadlines { native, terminate }).await
    }

    pub(crate) async fn stop_until(&self, deadlines: StopDeadlines) -> DriverResult<ExitReport> {
        let mut completion = {
            let mut stop = self.handle.inner.stop.lock().expect("stop state lock");
            let stopping = stop.get_or_insert_with(|| {
                let (complete, completion) = watch::channel(None);
                let (deadlines, changes) = watch::channel(deadlines);
                let inner = self.handle.inner.clone();
                tokio::spawn(async move {
                    complete.send_replace(Some(stop_native(&inner, changes).await));
                });
                Stopping {
                    completion,
                    deadlines,
                }
            });
            stopping.tighten(deadlines);
            stopping.completion.clone()
        };
        loop {
            if let Some(result) = completion.borrow_and_update().clone() {
                return result;
            }
            completion
                .changed()
                .await
                .map_err(|_| DriverError::new(DriverErrorCode::Transport, "stop worker stopped"))?;
        }
    }
}

fn deadline(duration: Duration) -> DriverResult<Instant> {
    if duration.is_zero() {
        return Err(DriverError::new(
            DriverErrorCode::InvalidRequest,
            "timeout must be positive",
        ));
    }
    Instant::now()
        .checked_add(duration)
        .ok_or_else(|| DriverError::new(DriverErrorCode::InvalidRequest, "timeout is too large"))
}

fn valid_method(method: &str) -> bool {
    !method.is_empty()
        && method.len() <= 128
        && method.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase() || byte == b'_' || (index > 0 && byte.is_ascii_digit())
        })
}

fn safe_integer(value: &Value) -> Option<u64> {
    value.as_u64().filter(|value| *value <= MAX_SAFE_INTEGER)
}

pub(crate) fn control_ready(health: &Value) -> bool {
    health["control_ready"] == true
}

fn validate_control(control: &Value) -> SetupResult<()> {
    let control = control
        .as_object()
        .context("control options must be an object")?;
    ensure!(
        control.keys().all(|key| key == "recovery"),
        "unknown control option"
    );
    if let Some(recovery) = control.get("recovery") {
        let request = recovery.as_object().context("recovery must be an object")?;
        let mode = request
            .get("mode")
            .and_then(Value::as_str)
            .context("recovery mode is required")?;
        ensure!(
            ["catalog", "apply"].contains(&mode),
            "invalid recovery mode"
        );
        ensure!(
            request.keys().all(|key| key == "mode"
                || key == "session_id"
                || (mode == "apply" && ["snapshot_id", "world_file"].contains(&key.as_str()))),
            "unknown recovery option"
        );
        if mode == "apply" || request.contains_key("session_id") {
            ensure!(
                recovery["session_id"]
                    .as_str()
                    .is_some_and(|value| !value.is_empty()
                        && value.len() <= 128
                        && !value.contains(['\0', '\r', '\n'])),
                "invalid recovery session"
            );
        }
        if mode == "apply" {
            ensure!(
                safe_integer(&recovery["snapshot_id"]).is_some_and(|id| id > 0),
                "invalid recovery snapshot"
            );
            ensure!(
                recovery["world_file"]
                    .as_str()
                    .is_some_and(|value| !value.is_empty()
                        && value.len() <= 4096
                        && !value.contains(['\0', '\r', '\n'])),
                "invalid recovery path"
            );
        }
    }
    Ok(())
}

async fn dispatch(
    inner: &Inner,
    record: &RequestRecord,
    frame: &[u8],
    deadline: Instant,
) -> DriverResult<oneshot::Receiver<DriverResult<Value>>> {
    let expired = || DriverError::new(DriverErrorCode::Timeout, "request expired before dispatch");
    let send = inner.process.with_writable_input(|input| {
        inner.change(|shared| {
            if Instant::now() >= deadline {
                return Ok(Err(expired()));
            }
            if !shared.state.ready
                || shared.state.stopping
                || shared.state.generation != Some(record.generation)
            {
                return Ok(Err(DriverError::new(
                    DriverErrorCode::NotReady,
                    "shard generation or readiness changed before dispatch",
                )));
            }
            if shared
                .active
                .as_ref()
                .is_some_and(|active| !active.record.completed)
            {
                return Ok(Err(DriverError::new(
                    DriverErrorCode::NotReady,
                    "previous native command is still pending",
                )));
            }
            // Holding the state lock across the atomic write registers even an immediate reply.
            write_request(input, frame)?;
            let (response, receive) = oneshot::channel();
            let mut record = record.clone();
            record.written = true;
            shared.state.recent_request = Some(record.clone());
            // Only a correlated reply can establish framing after an expired command.
            shared.active = Some(Pending {
                synchronized: shared.active.is_none(),
                record,
                result: None,
                response: Some(response),
            });
            Ok(Ok(receive))
        })
    });
    timeout_at(deadline, send)
        .await
        .map_err(|_| expired())?
        .map_err(|error| DriverError::new(DriverErrorCode::Transport, error.to_string()))?
}

async fn finish(
    inner: &Inner,
    record: &RequestRecord,
    frame: &[u8],
    deadline: Instant,
    mut pending: oneshot::Receiver<DriverResult<Value>>,
    caller: &mut oneshot::Sender<DriverResult<Value>>,
) -> DriverResult<Value> {
    loop {
        match timeout_at(deadline, pending).await {
            Ok(Ok(Err(error))) if error.code == DriverErrorCode::Busy && !error.accepted => {
                let retry = async {
                    timeout_at(deadline, sleep(Duration::from_millis(100)))
                        .await
                        .map_err(|_| {
                            DriverError::new(
                                DriverErrorCode::Timeout,
                                "native Lua remained busy without accepting the request",
                            )
                        })?;
                    dispatch(inner, record, frame, deadline).await
                };
                pending = tokio::select! {
                    biased;
                    _ = caller.closed() => return Err(error),
                    result = retry => result?,
                };
            }
            Ok(Ok(result)) => return result,
            _ => {
                return expire(
                    inner,
                    &record.id,
                    "native request result could not be confirmed before its deadline",
                );
            }
        }
    }
}

fn expire(inner: &Inner, id: &str, message: &str) -> DriverResult<Value> {
    inner.change(|shared| {
        let Some(active) = shared
            .active
            .as_mut()
            .filter(|active| active.record.id == id)
        else {
            return Err(DriverError::new(DriverErrorCode::Unknown, message));
        };
        // Retain the old frame until Done or until another correlated request
        // supersedes it. Uncorrelated Busy/Done cannot reject that newer request.
        let result = active
            .result
            .clone()
            .unwrap_or_else(|| Err(active.error(DriverErrorCode::Unknown, message)));
        active.deliver(result.clone());
        shared.state.recent_request = Some(active.record.clone());
        result
    })
}

async fn stop_native(
    inner: &Arc<Inner>,
    mut deadlines: watch::Receiver<StopDeadlines>,
) -> DriverResult<ExitReport> {
    inner.change(|shared| {
        shared.stop_requested = true;
        shared.state.stopping = true;
        shared.state.ready = false;
    });
    if inner.snapshot().running {
        {
            let native = async {
                {
                    let _native = inner.native_command.lock().await;
                    let _ = inner.process.send(b"c_shutdown()\n").await;
                }
                inner.process.wait().await
            };
            tokio::pin!(native);
            loop {
                let at = *deadlines.borrow_and_update();
                tokio::select! {
                    biased;
                    _ = tokio::time::sleep_until(at.native) => break,
                    _ = &mut native => return inner.waited().await,
                    _ = deadlines.changed() => {},
                }
            }
        }
        loop {
            let at = *deadlines.borrow_and_update();
            inner.process.request_shutdown(at.terminate);
            tokio::select! {
                _ = inner.process.wait() => break,
                _ = deadlines.changed() => {},
            }
        }
    }
    inner.waited().await
}

struct Pump {
    inner: Arc<Inner>,
    events: Option<DriverEventSender>,
}

impl Pump {
    fn publish(&self, payload_bytes: usize, payload: impl FnOnce() -> DriverEventKind) {
        let Some(sender) = self.events.as_ref().filter(|tx| !tx.events.is_closed()) else {
            return;
        };
        let (shard, nonce, generation) = {
            let state = self.inner.snapshots.borrow();
            (state.shard.clone(), state.nonce.clone(), state.generation)
        };
        let event = DriverEvent {
            id: Ulid::generate().to_string(),
            kind: payload(),
            shard,
            nonce,
            observed_timestamp_ns: crate::operation::timestamp(),
            generation,
        };
        let bytes = size_of::<DriverEvent>()
            + event.id.len()
            + event.shard.len()
            + event.nonce.len()
            + payload_bytes;
        let result = sender.events.try_send(event, bytes);
        if matches!(
            result,
            Err(bounded::TrySendError::Slots | bounded::TrySendError::Bytes)
        ) {
            sender.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    async fn run(self, mut output: ProcessOutput, finished: watch::Sender<bool>) {
        let inner = &self.inner;
        while let Some(event) = output.next_event().await {
            // Let pipe readers and pending commands run during bursts of CPU-heavy frames.
            tokio::task::consume_budget().await;
            let outcome = match event.kind {
                EventKind::Line(line) => handle_line(&self, event.stream, &line),
                EventKind::Oversized { mandatory: true } => {
                    Err(ControlFailureReason::Length
                        .error("mandatory frame exceeded its line limit"))
                }
                EventKind::Oversized { mandatory: false } => {
                    if event.stream == StreamKind::Reply {
                        inner.change(|shared| {
                            shared.state.reject_telemetry(
                                event.stream,
                                None,
                                RejectionReason::Length,
                                None,
                            );
                        });
                    }
                    self.publish(32, || {
                        DriverEventKind::Diagnostic(json!({"error":"oversized_output"}))
                    });
                    Ok(())
                }
                EventKind::ReadError(error) => Err(ControlFailureReason::Transport.error(error)),
            };
            if let Err(error) = outcome {
                self.publish(error.message.len() + 64, || {
                    DriverEventKind::Diagnostic(
                        json!({"error": error.message, "reason": error.reason}),
                    )
                });
                inner.fail(error);
            }
        }
        let report = inner.process.wait().await;
        inner.change(|shared| {
            shared.state.running = false;
            shared.state.ready = false;
            match &report {
                Ok(report) => {
                    shared.state.exit = Some(report.into());
                }
                Err(error) => {
                    shared.state.failure.get_or_insert_with(|| {
                        ControlFailureReason::Transport.error(error.to_string())
                    });
                }
            }
            if let Some(mut active) = shared.active.take() {
                let result = active.result.take().unwrap_or_else(|| {
                    Err(active.error(
                        DriverErrorCode::Unknown,
                        "game exited before command completion",
                    ))
                });
                active.deliver(result);
                shared.state.recent_request = Some(active.record);
            }
        });
        // The remaining exit fields are bounded counters, flags, timestamps and identifiers.
        let bytes = 1024
            + report
                .as_ref()
                .ok()
                .and_then(|report| report.protocol_error.as_ref())
                .map_or(0, String::len);
        self.publish(bytes, || {
            let state = inner.snapshots.borrow();
            DriverEventKind::Lifecycle(json!({
                "event":"process_exited", "process_id":state.process_id, "exit":state.exit,
                "control_error":state.failure.as_ref().map(|error| error.code),
                "native_diagnostics":state.native_diagnostics,
            }))
        });
        drop(self.events);
        finished.send_replace(true);
    }
}

fn telemetry(pump: &Pump, payload: &[u8]) {
    let inner = &pump.inner;
    let record = crate::events::parse(payload);
    let mut shared = inner.shared.lock().expect("driver state lock");
    let rejection = (|| {
        let record = match &record {
            Ok(record) => record,
            Err(error) => {
                let reason = serde_json::from_value(error.details["reason"].clone())
                    .expect("event parser supplies a fixed rejection reason");
                let position = serde_json::from_value(error.details["position"].clone())
                    .expect("event parser supplies an optional JSON position");
                return Some((reason, position));
            }
        };
        if record.nonce != shared.state.nonce {
            return Some((RejectionReason::Nonce, None));
        }
        let sequence = record.seq.0;
        if Some(record.generation.0) != shared.state.generation {
            return Some((RejectionReason::Generation, None));
        }
        if sequence <= shared.state.telemetry_sequence {
            return Some((RejectionReason::Sequence, None));
        }
        shared.state.telemetry_gaps += sequence - shared.state.telemetry_sequence - 1;
        shared.state.telemetry_sequence = sequence;
        None
    })();
    let accepted = rejection.is_none();
    if let Some((reason, position)) = rejection {
        shared
            .state
            .reject_telemetry(StreamKind::Reply, Some(payload.len()), reason, position);
    }
    inner.snapshots.send_if_modified(|snapshot| {
        let state = &shared.state;
        let changed = snapshot.telemetry_gaps != state.telemetry_gaps
            || snapshot.invalid_telemetry != state.invalid_telemetry
            || snapshot.stale_telemetry != state.stale_telemetry;
        snapshot.telemetry_sequence = state.telemetry_sequence;
        snapshot.telemetry_gaps = state.telemetry_gaps;
        snapshot.invalid_telemetry = state.invalid_telemetry;
        snapshot.stale_telemetry = state.stale_telemetry;
        if changed {
            snapshot.telemetry_rejections = state.telemetry_rejections.clone();
            snapshot.last_telemetry_rejection = state.last_telemetry_rejection.clone();
        }
        changed
    });
    drop(shared);
    if accepted {
        pump.publish(payload.len(), || {
            DriverEventKind::Telemetry(Box::new(record.expect("validated event")))
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn queued_frames_keep_their_generation_when_bootstrap_overtakes_play() {
        let nonce = Ulid::generate().to_string();
        let play = |generation| {
            json!({
                "nonce":nonce, "generation":generation, "session_id":null, "seq":1,
                "event":"dst.world.state_changed", "tick":1, "monotonic_ms":1, "cycle":null,
                "data":{"name":"cycles","value":1}
            })
        };
        let mut frames = String::new();
        for (prefix, value) in [
            ("DST_DRIVER", json!({"nonce":nonce,"generation":1})),
            ("DST_OTEL", play(1)),
            ("DST_DRIVER", json!({"nonce":nonce,"generation":2})),
            (
                "DST_CONTROL",
                json!({"nonce":nonce,"generation":1,"event":"world_load_failed"}),
            ),
            ("DST_OTEL", play(2)),
        ] {
            use std::fmt::Write;
            writeln!(frames, "{prefix}|{value}").unwrap();
        }
        let (process, output) = GameProcess::spawn(ProcessSpec {
            program: "python3".into(),
            args: vec![
                "-c".into(),
                "import os, sys; os.write(4, sys.argv[1].encode())".into(),
                frames.into(),
            ],
            cwd: std::env::temp_dir(),
        })
        .unwrap();
        // Both queues contain their complete input before the pump can read either one.
        assert!(process.wait().await.unwrap().output_drained);
        let (sender, mut events) = DriverEvents::channel();
        let driver = Driver::start_pump(process, output, nonce, "Master".into(), Some(sender));
        driver.wait().await.unwrap();
        let state = driver.snapshot();
        assert_eq!(state.telemetry_rejections[&RejectionReason::Generation], 1);
        assert_eq!((state.telemetry_sequence, state.telemetry_gaps), (1, 0));
        assert_eq!(state.control_count, 0);
        assert!(state.last_control.is_none() && state.load_failure.is_none());
        let play = events.recv().await.unwrap();
        assert!(matches!(play.kind, DriverEventKind::Telemetry(_)));
        assert_eq!(play.generation, Some(2));
        assert_eq!(json!(&play)["data"]["generation"], 2);
        assert_eq!(
            json!(events.recv().await.unwrap())["data"]["event"],
            "process_exited"
        );
        assert!(events.recv().await.is_none());
    }

    #[tokio::test]
    async fn telemetry_sequence_updates_do_not_wake_control_watchers() {
        let nonce = Ulid::generate().to_string();
        let driver = Driver::attach(
            ProcessSpec {
                program: "python3".into(),
                args: vec!["-c".into(), "import time; time.sleep(60)".into()],
                cwd: std::env::temp_dir(),
            },
            nonce.clone(),
            "Master".into(),
            None,
        )
        .unwrap();
        let inner = &driver.handle.inner;
        inner.change(|shared| shared.state.generation = Some(1));
        let mut states = driver.watch();
        let (sender, mut events) = DriverEvents::channel();
        let pump = Pump {
            inner: inner.clone(),
            events: Some(sender),
        };
        let mut event = json!({
            "nonce":nonce,"generation":1,"session_id":null,"seq":1,
            "event":"dst.world.state_changed","tick":1,"monotonic_ms":1,"cycle":null,
            "data":{"name":"cycles","value":1}
        });
        telemetry(&pump, &serde_json::to_vec(&event).unwrap());
        assert!(!states.has_changed().unwrap());
        assert_eq!(states.borrow().telemetry_sequence, 1);
        assert_eq!(driver.snapshot().telemetry_sequence, 1);
        assert_eq!(json!(events.recv().await.unwrap())["data"], event);

        telemetry(&pump, &serde_json::to_vec(&event).unwrap());
        assert!(states.has_changed().unwrap());
        assert_eq!(states.borrow_and_update().stale_telemetry, 1);
        telemetry(&pump, b"{invalid");
        assert!(states.has_changed().unwrap());
        assert_eq!(states.borrow_and_update().invalid_telemetry, 1);
        event["nonce"] = json!(Ulid::generate().to_string());
        telemetry(&pump, &serde_json::to_vec(&event).unwrap());
        assert_eq!(states.borrow_and_update().invalid_telemetry, 2);

        event["nonce"] = json!(nonce);
        event["seq"] = json!(3);
        telemetry(&pump, &serde_json::to_vec(&event).unwrap());
        assert!(states.has_changed().unwrap());
        assert_eq!(states.borrow_and_update().telemetry_gaps, 1);
        assert_eq!(json!(events.recv().await.unwrap())["data"], event);
        assert_eq!(driver.snapshot().telemetry_sequence, 3);
        assert_eq!(events.dropped(), 0);
        event["generation"] = json!(0);
        telemetry(&pump, &serde_json::to_vec(&event).unwrap());
        event["generation"] = json!(1);
        let mut invalid = event.clone();
        invalid["data"] = json!({"SECRET_TOKEN": "private chat"});
        for (payload, reason) in [
            (
                vec![b' '; crate::events::MAX_EVENT_BYTES + 1],
                RejectionReason::Length,
            ),
            (br#"{"SECRET_TOKEN":"#.to_vec(), RejectionReason::JsonEof),
            (
                b"{invalid SECRET_TOKEN}".to_vec(),
                RejectionReason::JsonSyntax,
            ),
            (
                format!("{}0{}", "[".repeat(66), "]".repeat(66)).into_bytes(),
                RejectionReason::Complexity,
            ),
            (
                serde_json::to_vec(&invalid).unwrap(),
                RejectionReason::Schema,
            ),
        ] {
            telemetry(&pump, &payload);
            let snapshot = states.borrow_and_update();
            let rejection = snapshot.last_telemetry_rejection.as_ref().unwrap();
            assert_eq!(rejection.reason, reason);
            assert_eq!(rejection.bytes, Some(payload.len()));
            assert_eq!(rejection.source, StreamKind::Reply);
            let metadata = serde_json::to_string(rejection).unwrap();
            assert!(!metadata.contains("SECRET_TOKEN") && !metadata.contains("private chat"));
            assert!(!metadata.contains("field") && !metadata.contains("nonce"));
        }
        let snapshot = driver.snapshot();
        assert_eq!(snapshot.invalid_telemetry, 7);
        assert_eq!(snapshot.stale_telemetry, 2);
        assert_eq!(
            snapshot.telemetry_rejections[&RejectionReason::JsonSyntax],
            2
        );
        assert_eq!(snapshot.telemetry_rejections[&RejectionReason::Nonce], 1);
        assert_eq!(
            snapshot.telemetry_rejections[&RejectionReason::Generation],
            1
        );
        assert_eq!(snapshot.telemetry_rejections[&RejectionReason::Sequence], 1);
        event["seq"] = json!(4);
        telemetry(&pump, &serde_json::to_vec(&event).unwrap());
        assert!(!states.has_changed().unwrap());
        assert_eq!(json!(events.recv().await.unwrap())["data"], event);
        let maximum = crate::events::MAX_EVENT_BYTES;
        for (bytes, ending, accepted) in [
            (maximum, "\n", true),
            (maximum - 1, "\r\n", true),
            (maximum, "\r\n", false),
            (maximum, "", true),
            (maximum + 1, "\n", false),
        ] {
            event["seq"] = json!(driver.snapshot().telemetry_sequence + 1);
            let payload = format!("{:bytes$}", event.to_string());
            let frame = format!("DST_OTEL|{payload}{ending}");
            handle_line(&pump, StreamKind::Reply, frame.as_bytes()).unwrap();
            assert_eq!(states.has_changed().unwrap(), !accepted);
            let snapshot = states.borrow_and_update();
            if accepted {
                assert_eq!(snapshot.telemetry_sequence, event["seq"].as_u64().unwrap());
            } else {
                let rejection = snapshot.last_telemetry_rejection.as_ref().unwrap();
                assert_eq!(rejection.reason, RejectionReason::Length);
                assert_eq!(rejection.bytes, Some(bytes + usize::from(ending == "\r\n")));
            }
            drop(snapshot);
            if accepted {
                assert_eq!(json!(events.recv().await.unwrap())["data"], event);
            }
        }
        for (stream, prefix) in [
            (StreamKind::Reply, "[0:00:01]: DST_OTEL|"),
            (StreamKind::Reply, "xDST_OTEL|"),
            (StreamKind::Reply, "DST_OTELx|"),
            (StreamKind::Stdout, "[0:00:01]: [0:00:02]: DST_OTEL|"),
        ] {
            handle_line(&pump, stream, format!("{prefix}{{invalid\n").as_bytes()).unwrap();
            assert!(!states.has_changed().unwrap());
            assert!(matches!(
                events.recv().await.unwrap().kind,
                DriverEventKind::Log(_)
            ));
        }
        for generation in [2, 3] {
            event["generation"] = json!(generation);
            event["seq"] = json!(1);
            telemetry(&pump, &serde_json::to_vec(&event).unwrap());
            assert_eq!(driver.snapshot().generation, Some(generation - 1));
            handle_line(
                &pump,
                StreamKind::Reply,
                format!(
                    "DST_DRIVER|{}\n",
                    json!({"nonce":nonce,"generation":generation})
                )
                .as_bytes(),
            )
            .unwrap();
            assert_eq!(driver.snapshot().generation, Some(generation));
            telemetry(&pump, &serde_json::to_vec(&event).unwrap());
            assert_eq!(driver.snapshot().telemetry_sequence, 1);
        }
        driver.kill().await.unwrap();
    }
}
