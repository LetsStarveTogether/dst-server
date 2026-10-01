//! Persistent shard control. Caller cancellation never replays or cancels a command.

use std::{
    collections::VecDeque,
    ffi::OsString,
    fmt,
    fs::{self, File},
    io::Write,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result as SetupResult, ensure};
use serde::Serialize;
use serde_json::{Value, json};
use tokio::{
    sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore, mpsc, oneshot, watch},
    time::{Instant, sleep, timeout_at},
};
use ulid::Ulid;

use crate::{
    configuration::Cluster,
    process::{
        EventKind, ExitReport, GameProcess, MAX_PROTOCOL_LINE_BYTES, MAX_REQUEST_BYTES,
        ProcessSpec, StreamKind, native_message,
    },
};

const MAX_PENDING: usize = 64;
const CONTROL_HISTORY: usize = 128;
const SUBSCRIPTION_LINES: usize = 1024;
const SUBSCRIPTION_BYTES: usize = 8 * 1024 * 1024;
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

pub const DEFAULT_ACTIONS: &[&str] = &[
    "ACTIVATE",
    "ADDFUEL",
    "ATTACK",
    "BUILD",
    "CASTAOE",
    "CASTSPELL",
    "CHOP",
    "CONSTRUCT",
    "COOK",
    "DEPLOY",
    "DIG",
    "EXTINGUISH",
    "FERTILIZE",
    "FISH",
    "FISH_OCEAN",
    "GIVE",
    "GIVETOPLAYER",
    "HAMMER",
    "HARVEST",
    "HEAL",
    "LIGHT",
    "MIGRATE",
    "MINE",
    "MURDER",
    "PICK",
    "PICKUP",
    "PLANT",
    "REPAIR",
    "REVIVE_CORPSE",
    "TELEPORT",
    "UNLOCK",
    "UPGRADE",
];

#[derive(Clone, Debug)]
pub struct DriverOptions {
    pub profile: String,
    pub actions: Vec<String>,
    /// Native save observation and, optionally, a fixed pre-load recovery target.
    pub control: Value,
    pub extra_args: Vec<OsString>,
}

impl Default for DriverOptions {
    fn default() -> Self {
        Self {
            profile: "history".into(),
            actions: DEFAULT_ACTIONS
                .iter()
                .map(|action| (*action).into())
                .collect(),
            control: json!({"observe_saves": true}),
            extra_args: vec!["-skip_update_server_mods".into()],
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
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

#[derive(Clone, Debug, Serialize)]
pub struct DriverError {
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
            message: message.into(),
            request_id: None,
            written: false,
            accepted: false,
        }
    }
}

impl fmt::Display for DriverError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{:?}: {}", self.code, self.message)
    }
}
impl std::error::Error for DriverError {}

pub type DriverResult<T> = Result<T, DriverError>;

#[derive(Clone, Debug, Serialize)]
pub struct RequestRecord {
    pub id: String,
    pub method: String,
    pub generation: u64,
    pub written: bool,
    pub accepted: bool,
    pub completed: bool,
    pub result: Option<Value>,
    pub error: Option<DriverError>,
}

#[derive(Clone, Debug, Serialize)]
pub struct DriverState {
    pub shard: String,
    pub pid: u32,
    pub nonce: String,
    pub generation: Option<u64>,
    pub generation_changes: u64,
    pub native_ready: bool,
    pub ready: bool,
    pub running: bool,
    pub stopping: bool,
    /// Exit code or negative terminating signal; None until an exit is observed.
    pub returncode: Option<i32>,
    pub forced: bool,
    pub output_drained: bool,
    pub load_failure: Option<Value>,
    pub session_id: Option<String>,
    pub health: Option<Value>,
    pub runtime: Option<Value>,
    pub last_native_save: Option<Value>,
    pub last_control: Option<Value>,
    /// Required evidence is retained before optional subscriber delivery.
    pub control_records: VecDeque<Value>,
    pub failure: Option<String>,
    pub telemetry_sequence: u64,
    pub telemetry_gaps: u64,
    pub invalid_telemetry: u64,
    pub stale_telemetry: u64,
    pub unmatched_replies: u64,
    pub recent_request: Option<RequestRecord>,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DriverEventKind {
    Lifecycle,
    Control,
    Telemetry,
    Log,
    Diagnostic,
}

#[derive(Clone, Debug, Serialize)]
pub struct DriverEvent {
    pub kind: DriverEventKind,
    pub shard: String,
    pub nonce: String,
    pub observed_timestamp_ns: u64,
    pub generation: Option<u64>,
    pub data: Value,
}

struct QueuedEvent {
    event: Arc<DriverEvent>,
    _bytes: OwnedSemaphorePermit,
}

pub struct Subscription {
    events: mpsc::Receiver<QueuedEvent>,
    dropped: Arc<AtomicU64>,
}

impl Subscription {
    pub async fn recv(&mut self) -> Option<Arc<DriverEvent>> {
        self.events.recv().await.map(|event| event.event)
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

struct Subscriber {
    events: mpsc::Sender<QueuedEvent>,
    bytes: Arc<Semaphore>,
    dropped: Arc<AtomicU64>,
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
            message: message.into(),
            request_id: Some(self.record.id.clone()),
            written: self.record.written,
            accepted: self.record.accepted,
        }
    }

    fn deliver(&mut self, result: DriverResult<Value>) {
        self.record.completed = true;
        match &result {
            Ok(data) => {
                self.record.result = Some(data.clone());
                self.record.error = None;
            }
            Err(error) => self.record.error = Some(error.clone()),
        }
        if let Some(response) = self.response.take() {
            let _ = response.send(result);
        }
    }
}

struct Shared {
    state: DriverState,
    stop_requested: bool,
    // Only one native command may be awaiting its uncorrelated Done/Busy marker.
    active: Option<Pending>,
}

type StopCompletion = Option<DriverResult<ExitReport>>;

struct StopState {
    before: Option<DriverState>,
    completion: watch::Receiver<StopCompletion>,
}

struct Inner {
    process: Arc<GameProcess>,
    shared: Mutex<Shared>,
    snapshots: watch::Sender<DriverState>,
    native_command: AsyncMutex<()>,
    slots: Arc<Semaphore>,
    subscribers: Mutex<Vec<Subscriber>>,
    pump_finished: watch::Receiver<bool>,
    stop: Mutex<Option<StopState>>,
}

impl Inner {
    fn change<T>(&self, change: impl FnOnce(&mut Shared) -> T) -> T {
        let mut shared = self.shared.lock().expect("driver state lock");
        let result = change(&mut shared);
        self.snapshots.send_replace(shared.state.clone());
        result
    }

    fn snapshot(&self) -> DriverState {
        self.shared.lock().expect("driver state lock").state.clone()
    }

    fn publish(&self, kind: DriverEventKind, data: Value) {
        let mut subscribers = self.subscribers.lock().expect("subscriber lock");
        subscribers.retain(|subscriber| !subscriber.events.is_closed());
        if subscribers.is_empty() {
            return;
        }
        let state = self.snapshots.borrow();
        let event = Arc::new(DriverEvent {
            kind,
            shard: state.shard.clone(),
            nonce: state.nonce.clone(),
            observed_timestamp_ns: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
                .min(u64::MAX as u128) as u64,
            generation: state.generation,
            data,
        });
        drop(state);
        let size = serde_json::to_vec(&*event).map_or(SUBSCRIPTION_BYTES + 1, |bytes| bytes.len());
        for subscriber in subscribers.iter() {
            let permit = u32::try_from(size)
                .ok()
                .and_then(|size| subscriber.bytes.clone().try_acquire_many_owned(size).ok());
            let sent = permit.is_some_and(|permit| {
                subscriber
                    .events
                    .try_send(QueuedEvent {
                        event: event.clone(),
                        _bytes: permit,
                    })
                    .is_ok()
            });
            if !sent {
                subscriber.dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    fn fail(&self, message: impl Into<String>) {
        let message = message.into();
        self.change(|shared| {
            shared.state.failure.get_or_insert(message.clone());
            shared.state.ready = false;
            if let Some(active) = &mut shared.active {
                active.deliver(Err(active.error(
                    DriverErrorCode::Unknown,
                    "control failed after command dispatch began",
                )));
                shared.state.recent_request = Some(active.record.clone());
            }
        });
        self.publish(DriverEventKind::Diagnostic, json!({"error": message}));
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
    pub fn spawn(
        cluster: &Cluster,
        executable: &Path,
        shard: &str,
        options: DriverOptions,
    ) -> SetupResult<Self> {
        ensure!(
            cluster
                .shards
                .iter()
                .any(|candidate| candidate.name == shard),
            "shard is not part of the validated cluster"
        );
        validate_options(&options)?;
        let executable = executable
            .canonicalize()
            .context("resolve game executable")?;
        let nonce = Ulid::new().to_string();
        let directory = cluster.directory.join(shard);
        let options_path = directory.join("dst_server_driver.json");
        match fs::symlink_metadata(&options_path) {
            Ok(metadata) => ensure!(
                metadata.is_file() && !metadata.file_type().is_symlink(),
                "driver options must be a regular file"
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(error) => return Err(error).context("inspect driver options"),
        }
        let encoded = serde_json::to_vec(&json!({"nonce": nonce, "profile": options.profile,
            "actions": options.actions, "control": options.control}))?;
        ensure!(
            encoded.len() < MAX_PROTOCOL_LINE_BYTES,
            "driver options exceed the native size limit"
        );
        let mut file = tempfile::NamedTempFile::new_in(&directory)?;
        file.write_all(&encoded)?;
        file.write_all(b"\n")?;
        file.as_file().sync_all()?;
        file.persist(options_path)
            .context("commit driver options")?;
        File::open(&directory)?.sync_all()?;
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
        args.extend(options.extra_args);
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
            nonce,
            shard.to_owned(),
        )
    }

    /// Attach the same driver to an explicit launch command, useful for isolated tests.
    pub fn attach(spec: ProcessSpec, nonce: String, shard: String) -> SetupResult<Self> {
        ensure!(
            Ulid::from_string(&nonce).is_ok() && nonce == nonce.to_ascii_uppercase(),
            "nonce must be a canonical ULID"
        );
        ensure!(
            !shard.is_empty() && shard.len() <= 255 && !shard.contains(['\0', '\r', '\n']),
            "invalid shard name"
        );
        let process = Arc::new(GameProcess::spawn(spec)?);
        let state = DriverState {
            shard,
            pid: process.pid(),
            nonce,
            generation: None,
            generation_changes: 0,
            native_ready: false,
            ready: false,
            running: true,
            stopping: false,
            returncode: None,
            forced: false,
            output_drained: false,
            load_failure: None,
            session_id: None,
            health: None,
            runtime: None,
            last_native_save: None,
            last_control: None,
            control_records: VecDeque::new(),
            failure: None,
            telemetry_sequence: 0,
            telemetry_gaps: 0,
            invalid_telemetry: 0,
            stale_telemetry: 0,
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
                active: None,
            }),
            snapshots,
            native_command: AsyncMutex::new(()),
            slots: Arc::new(Semaphore::new(MAX_PENDING)),
            subscribers: Mutex::new(Vec::new()),
            pump_finished,
            stop: Mutex::new(None),
        });
        tokio::spawn(pump(inner.clone(), finished));
        Ok(Self {
            handle: Arc::new(Handle { inner }),
        })
    }

    pub fn snapshot(&self) -> DriverState {
        self.handle.inner.snapshot()
    }
    pub fn watch(&self) -> watch::Receiver<DriverState> {
        self.handle.inner.snapshots.subscribe()
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

    pub fn subscribe(&self) -> Subscription {
        let (events, receiver) = mpsc::channel(SUBSCRIPTION_LINES);
        let dropped = Arc::new(AtomicU64::new(0));
        self.handle
            .inner
            .subscribers
            .lock()
            .expect("subscriber lock")
            .push(Subscriber {
                events,
                bytes: Arc::new(Semaphore::new(SUBSCRIPTION_BYTES)),
                dropped: dropped.clone(),
            });
        Subscription {
            events: receiver,
            dropped,
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
                        .unwrap_or_else(|| "game process exited".into()),
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

    /// Submit once. Cancelling this future abandons only the caller's wait.
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
        let id = Ulid::new().to_string();
        let frame = format!(
            "DST_RPC|{}\n",
            json!({"v": 1, "nonce": state.nonce, "id": id,
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
            result: None,
            error: None,
        };
        let (response, receive) = oneshot::channel();
        let inner = self.handle.inner.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let result = execute(&inner, record, frame, deadline).await;
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

    pub(crate) fn stop_snapshot(&self) -> Option<DriverState> {
        self.handle
            .inner
            .stop
            .lock()
            .expect("stop state lock")
            .as_ref()
            .and_then(|stop| stop.before.clone())
    }

    /// Native saving shutdown, followed by SIGTERM and SIGKILL when its deadline expires.
    /// The shared worker continues even when every stop caller cancels its wait.
    pub async fn stop(&self, grace: Duration) -> DriverResult<ExitReport> {
        let deadline = deadline(grace)?;
        let mut completion = {
            let mut stop = self.handle.inner.stop.lock().expect("stop state lock");
            stop.get_or_insert_with(|| {
                let (complete, completion) = watch::channel(None);
                let inner = self.handle.inner.clone();
                tokio::spawn(async move {
                    complete.send_replace(Some(stop_native(&inner, deadline).await));
                });
                StopState {
                    before: None,
                    completion,
                }
            })
            .completion
            .clone()
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

fn validate_options(options: &DriverOptions) -> SetupResult<()> {
    ensure!(
        ["off", "critical", "history"].contains(&options.profile.as_str()),
        "invalid telemetry profile"
    );
    ensure!(
        options.actions.len() <= 1024
            && options.actions.iter().all(|action| !action.is_empty()
                && action.len() <= 128
                && action.bytes().all(|byte| byte.is_ascii_uppercase()
                    || byte == b'_'
                    || byte.is_ascii_digit())),
        "invalid telemetry action list"
    );
    let control = options
        .control
        .as_object()
        .context("control options must be an object")?;
    ensure!(
        control
            .keys()
            .all(|key| ["observe_saves", "recovery"].contains(&key.as_str())),
        "unknown control option"
    );
    ensure!(
        control.get("observe_saves").is_none_or(Value::is_boolean),
        "observe_saves must be boolean"
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

async fn execute(
    inner: &Arc<Inner>,
    record: RequestRecord,
    frame: Vec<u8>,
    deadline: Instant,
) -> DriverResult<Value> {
    let _native = timeout_at(deadline, inner.native_command.lock())
        .await
        .map_err(|_| {
            DriverError::new(DriverErrorCode::Timeout, "request expired before dispatch")
        })?;
    loop {
        let (response, receive) = oneshot::channel();
        inner.change(|shared| {
            if !shared.state.ready
                || shared.state.stopping
                || shared.state.generation != Some(record.generation)
            {
                return Err(DriverError::new(
                    DriverErrorCode::NotReady,
                    "shard generation or readiness changed before dispatch",
                ));
            }
            if shared
                .active
                .as_ref()
                .is_some_and(|active| !active.record.completed)
            {
                return Err(DriverError::new(
                    DriverErrorCode::NotReady,
                    "previous native command is still pending",
                ));
            }
            // An expired command may have been consumed without a Lua context,
            // producing neither Busy nor Done. A new correlated response can
            // establish framing again; never replay the expired command.
            let synchronized = shared.active.is_none();
            shared.state.recent_request = Some(record.clone());
            shared.active = Some(Pending {
                record: record.clone(),
                result: None,
                response: Some(response),
                synchronized,
            });
            Ok(())
        })?;
        match timeout_at(deadline, inner.process.send(&frame)).await {
            Ok(Ok(())) => inner.change(|shared| {
                if let Some(active) = &mut shared.active {
                    active.record.written = true;
                    shared.state.recent_request = Some(active.record.clone());
                }
            }),
            result => {
                let message = match result {
                    Err(_) => "native request write deadline exceeded".into(),
                    Ok(Err(error)) => format!("native request write failed: {error}"),
                    Ok(Ok(())) => unreachable!(),
                };
                return expire(inner, &record.id, &message);
            }
        }
        match timeout_at(deadline, receive).await {
            Ok(Ok(Err(error))) if error.code == DriverErrorCode::Busy && !error.accepted => {
                timeout_at(deadline, sleep(Duration::from_millis(100)))
                    .await
                    .map_err(|_| {
                        DriverError::new(
                            DriverErrorCode::Timeout,
                            "native Lua remained busy without accepting the request",
                        )
                    })?;
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

async fn stop_native(inner: &Arc<Inner>, deadline: Instant) -> DriverResult<ExitReport> {
    inner.change(|shared| {
        shared.stop_requested = true;
        shared.state.stopping = true;
        shared.state.ready = false;
    });
    if inner.snapshot().running {
        if let Ok(_native) = timeout_at(deadline, inner.native_command.lock()).await {
            let before = inner.snapshot();
            if let Some(stop) = inner.stop.lock().expect("stop state lock").as_mut() {
                stop.before = Some(before);
            }
            let _ = timeout_at(deadline, inner.process.send(b"c_shutdown()\n")).await;
        }
        if timeout_at(deadline, inner.process.wait()).await.is_err() {
            inner
                .process
                .shutdown(Duration::from_secs(10))
                .await
                .map_err(|error| DriverError::new(DriverErrorCode::Transport, error.to_string()))?;
        }
    }
    inner.waited().await
}

async fn pump(inner: Arc<Inner>, finished: watch::Sender<bool>) {
    while let Some(event) = inner.process.next_event().await {
        let outcome = match event.kind {
            EventKind::Line(line) => handle_line(&inner, event.stream, &line),
            EventKind::Oversized
                if matches!(event.stream, StreamKind::Reply | StreamKind::Lifecycle) =>
            {
                Err("mandatory frame exceeded its line limit".into())
            }
            EventKind::Oversized => {
                inner.publish(
                    DriverEventKind::Diagnostic,
                    json!({"error": "oversized_output"}),
                );
                Ok(())
            }
            EventKind::ReadError(error) => Err(error),
        };
        if let Err(message) = outcome {
            inner.fail(message);
        }
    }
    let report = inner.process.wait().await;
    inner.change(|shared| {
        shared.state.running = false;
        shared.state.ready = false;
        match &report {
            Ok(report) => {
                shared.state.returncode = report.returncode();
                shared.state.forced = report.forced;
                shared.state.output_drained = report.output_drained;
                if let Some(error) = &report.protocol_error {
                    shared.state.failure.get_or_insert(error.clone());
                }
                if !report.output_drained {
                    shared
                        .state
                        .failure
                        .get_or_insert("game output did not drain".into());
                }
            }
            Err(error) => {
                shared.state.failure.get_or_insert(error.to_string());
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
    inner.subscribers.lock().expect("subscriber lock").clear();
    finished.send_replace(true);
}

fn handle_line(inner: &Inner, stream: StreamKind, line: &[u8]) -> Result<(), String> {
    let line = native_message(line).trim_ascii_end();
    match stream {
        StreamKind::Reply => {
            if line == b"DST_RemoteCommandDone" || line == b"DST_LuaBusy" {
                native_barrier(inner, line);
                return Ok(());
            }
            if let Some(payload) = line.strip_prefix(b"DST_RPC|") {
                return rpc(inner, payload);
            }
            if let Some(payload) = line.strip_prefix(b"DST_CONTROL|") {
                return control(inner, payload);
            }
        }
        StreamKind::Lifecycle => return lifecycle(inner, line),
        StreamKind::Stdout => {
            if let Some(payload) = line.strip_prefix(b"DST_DRIVER|") {
                return bootstrap(inner, payload);
            }
            if let Some(payload) = line.strip_prefix(b"DST_CONTROL|") {
                return control(inner, payload);
            }
            if let Some(payload) = line.strip_prefix(b"DST_OTEL|") {
                telemetry(inner, payload);
                return Ok(());
            }
        }
        StreamKind::Stderr => (),
    }
    if !line.starts_with(b"DST_Stats|") {
        inner.publish(DriverEventKind::Log, json!({"stream": format!("{stream:?}").to_ascii_lowercase(), "line": String::from_utf8_lossy(line)}));
    }
    Ok(())
}

fn mandatory_record(inner: &Inner, payload: &[u8]) -> Result<Value, String> {
    let value: Value =
        serde_json::from_slice(payload).map_err(|_| "malformed mandatory JSON frame")?;
    if !value.is_object()
        || value["nonce"].as_str() != Some(inner.snapshots.borrow().nonce.as_str())
    {
        return Err("mandatory frame has an invalid nonce or envelope".into());
    }
    Ok(value)
}

fn advance_generation(shared: &mut Shared, generation: u64) -> bool {
    if shared
        .state
        .generation
        .is_some_and(|current| current > generation)
    {
        return false;
    }
    if shared.state.generation == Some(generation) {
        return true;
    }
    if shared.state.generation.is_some() {
        shared.state.generation_changes += 1;
        shared.state.session_id = None;
    }
    shared.state.generation = Some(generation);
    shared.state.stopping = shared.stop_requested;
    shared.state.ready = false;
    shared.state.health = None;
    shared.state.runtime = None;
    shared.state.last_control = None;
    shared.state.load_failure = None;
    shared.state.control_records.clear();
    shared.state.telemetry_sequence = 0;
    if let Some(active) = &mut shared.active
        && active.record.generation != generation
    {
        active.deliver(Err(active.error(
            DriverErrorCode::Unknown,
            "world generation changed while the command was pending",
        )));
        shared.state.recent_request = Some(active.record.clone());
    }
    true
}

fn recompute_ready(state: &mut DriverState) {
    state.ready = state.running
        && !state.stopping
        && state.failure.is_none()
        && state.native_ready
        && state.health.as_ref().is_some_and(|health| {
            safe_integer(&health["generation"]) == state.generation
                && health["protocol"] == 3
                && health["capabilities"]["players"] == "active"
        });
}

fn bootstrap(inner: &Inner, payload: &[u8]) -> Result<(), String> {
    let record = mandatory_record(inner, payload)?;
    if record.get("error").is_some() {
        if safe_integer(&record["generation"])
            .zip(inner.snapshots.borrow().generation)
            .is_some_and(|(received, current)| received < current)
        {
            return Ok(());
        }
        return Err(format!("driver bootstrap failed: {}", record["error"]));
    }
    let health = record.get("health");
    let generation = safe_integer(&health.unwrap_or(&record)["generation"])
        .ok_or("invalid driver generation")?;
    if health.is_some_and(|health| {
        health["protocol"] != 3
            || !health["capabilities"].is_object()
            || !["disabled", "active", "degraded", "failed"]
                .iter()
                .any(|status| health["telemetry_status"] == *status)
    }) {
        return Err("invalid driver health".into());
    }
    inner.change(|shared| {
        if !advance_generation(shared, generation) {
            return;
        }
        if let Some(health) = health {
            shared.state.health = Some(health.clone());
        }
        recompute_ready(&mut shared.state);
    });
    Ok(())
}

fn rpc(inner: &Inner, payload: &[u8]) -> Result<(), String> {
    let response = mandatory_record(inner, payload)?;
    if response["v"] != 1
        || safe_integer(&response["generation"]).is_none()
        || !response["id"].is_string()
    {
        return Err("invalid RPC response header".into());
    }
    inner.change(|shared| {
        let Some(active) = shared
            .active
            .as_mut()
            .filter(|active| response["id"] == active.record.id)
        else {
            shared.state.unmatched_replies += 1;
            return Ok(());
        };
        let rejected = response["result"]["ok"] == false
            && !active.record.accepted
            && ["invalid_request", "not_ready", "stale_generation"]
                .iter()
                .any(|error| response["result"]["error"] == *error);
        if safe_integer(&response["generation"]) != Some(active.record.generation) && !rejected {
            return Err("RPC response generation does not match its request".into());
        }
        active.record.written = true;
        active.synchronized = true;
        if response["accepted"] == true && response.get("result").is_none() {
            active.record.accepted = true;
        } else if let Some(result) = response.get("result") {
            if active.result.is_some() {
                return Err("duplicate RPC result".into());
            }
            if result["ok"] == true && result.get("data").is_some() {
                active.record.accepted = true;
                let data = result["data"].clone();
                if active.record.method == "runtime"
                    && shared.state.generation == Some(active.record.generation)
                {
                    shared.state.runtime = Some(data.clone());
                    shared.state.session_id = data["session_id"].as_str().map(str::to_owned);
                }
                if active.record.method == "health"
                    && shared.state.generation == Some(active.record.generation)
                {
                    shared.state.health = Some(data.clone());
                    recompute_ready(&mut shared.state);
                }
                active.result = Some(Ok(data));
            } else if result["ok"] == false {
                let error = result["error"].as_str().ok_or("invalid RPC error")?;
                if !rejected {
                    active.record.accepted = true;
                }
                let code = match error {
                    "invalid_request" | "rejected" => DriverErrorCode::InvalidRequest,
                    "not_ready" | "stale_generation" => DriverErrorCode::NotReady,
                    "unsupported" => DriverErrorCode::Unsupported,
                    "not_found" => DriverErrorCode::NotFound,
                    "stale_reference" => DriverErrorCode::StaleReference,
                    "indeterminate" => DriverErrorCode::Unknown,
                    "lua_error" | "invalid_json_value" | "invalid_utf8" | "response_too_large" => {
                        DriverErrorCode::Lua
                    }
                    _ => return Err("unknown RPC error code".into()),
                };
                active.result = Some(Err(active.error(code, error)));
            } else {
                return Err("invalid RPC result".into());
            }
        } else {
            return Err("RPC response lacks an acceptance or result".into());
        }
        shared.state.recent_request = Some(active.record.clone());
        Ok(())
    })
}

fn native_barrier(inner: &Inner, line: &[u8]) {
    inner.change(|shared| {
        let Some(mut active) = shared.active.take() else {
            shared.state.unmatched_replies += 1;
            return;
        };
        if !active.synchronized {
            shared.state.unmatched_replies += 1;
            shared.active = Some(active);
            return;
        }
        if line == b"DST_LuaBusy" {
            if active.record.accepted || active.result.is_some() {
                shared.state.unmatched_replies += 1;
                shared.active = Some(active);
                return;
            }
            active.deliver(Err(active.error(
                DriverErrorCode::Busy,
                "native Lua rejected the request before acceptance",
            )));
        } else {
            let result = active.result.take().unwrap_or_else(|| {
                Err(active.error(
                    DriverErrorCode::Unknown,
                    "native command completed without a structured result",
                ))
            });
            active.deliver(result);
        }
        shared.state.recent_request = Some(active.record);
    });
}

fn control(inner: &Inner, payload: &[u8]) -> Result<(), String> {
    let record = mandatory_record(inner, payload)?;
    let generation = safe_integer(&record["generation"]).ok_or("invalid control generation")?;
    if record["v"] != 1 || !record["event"].as_str().is_some_and(valid_method) {
        return Err("invalid control record".into());
    }
    inner.change(|shared| {
        if !advance_generation(shared, generation) {
            return;
        }
        if record["event"] == "world_load_failed" {
            shared.state.load_failure = Some(record.clone());
        }
        shared.state.last_control = Some(record.clone());
        if shared.state.control_records.len() == CONTROL_HISTORY {
            shared.state.control_records.pop_front();
        }
        shared.state.control_records.push_back(record.clone());
    });
    inner.publish(DriverEventKind::Control, record);
    Ok(())
}

fn lifecycle(inner: &Inner, line: &[u8]) -> Result<(), String> {
    if line.starts_with(b"DST_Stats|") {
        return Ok(());
    }
    let text = std::str::from_utf8(line).map_err(|_| "invalid lifecycle UTF-8")?;
    let record = if text == "DST_Master_Ready" || text.starts_with("DST_Master_Ready|") {
        inner.change(|shared| {
            shared.state.native_ready = true;
            recompute_ready(&mut shared.state);
        });
        json!({"event": "ready", "detail": text.split_once('|').map_or("", |(_, detail)| detail)})
    } else if let Some(session) = text.strip_prefix("DST_SessionId|") {
        if session.is_empty() || session.len() > 128 {
            return Err("invalid native session identity".into());
        }
        inner.change(|shared| {
            shared.state.session_id = Some(session.into());
            shared.state.native_ready = true;
            recompute_ready(&mut shared.state);
        });
        json!({"event": "session", "session_id": session})
    } else if text == "DST_Saved" || text.starts_with("DST_Saved|") {
        let path = text.split_once('|').map_or("", |(_, path)| path);
        let snapshot = path
            .rsplit('/')
            .next()
            .and_then(|value| value.parse::<u64>().ok());
        let record = json!({"event": "saved", "path": path, "snapshot": snapshot});
        inner.change(|shared| shared.state.last_native_save = Some(record.clone()));
        record
    } else if text == "DST_Stopping" || text == "DST_Shutdown" {
        inner.change(|shared| {
            shared.state.stopping = true;
            shared.state.ready = false;
        });
        json!({"event": if text == "DST_Stopping" { "stopping" } else { "shutdown" }})
    } else {
        json!({"event": "unknown", "line": text})
    };
    inner.publish(DriverEventKind::Lifecycle, record);
    Ok(())
}

fn telemetry(inner: &Inner, payload: &[u8]) {
    let record = serde_json::from_slice::<Value>(payload)
        .ok()
        .filter(|record| {
            payload.len() + b"DST_OTEL|".len() <= MAX_PROTOCOL_LINE_BYTES
                && record.is_object()
                && record["v"] == 3
                && safe_integer(&record["generation"]).is_some()
                && safe_integer(&record["seq"]).is_some_and(|sequence| sequence > 0)
                && safe_integer(&record["tick"]).is_some()
                && safe_integer(&record["monotonic_ms"]).is_some()
                && (record["cycle"].is_null() || safe_integer(&record["cycle"]).is_some())
                && (record["session_id"].is_null() || record["session_id"].is_string())
                && record["event"]
                    .as_str()
                    .is_some_and(|event| event.starts_with("dst.") && event.len() <= 128)
                && record["data"].is_object()
                && crate::events::validate(record).is_ok()
        });
    let accepted = inner.change(|shared| {
        let Some(record) = &record else {
            shared.state.invalid_telemetry += 1;
            return false;
        };
        if record["nonce"] != shared.state.nonce {
            shared.state.invalid_telemetry += 1;
            return false;
        }
        let sequence = record["seq"].as_u64().expect("validated sequence");
        if record["generation"].as_u64() != shared.state.generation
            || sequence <= shared.state.telemetry_sequence
        {
            shared.state.stale_telemetry += 1;
            return false;
        }
        shared.state.telemetry_gaps += sequence - shared.state.telemetry_sequence - 1;
        shared.state.telemetry_sequence = sequence;
        true
    });
    if accepted {
        inner.publish(DriverEventKind::Telemetry, record.expect("validated event"));
    }
}
