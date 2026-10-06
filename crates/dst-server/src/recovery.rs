//! Persist recovery decisions before restarting a game or changing native snapshots.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use crate::{
    driver::{DriverErrorCode, DriverState},
    files::{RoomLock, state_section},
    lua,
    model::{Error, ErrorCode, SessionIdentity},
    process::{ExitSummary, NativeDiagnostics},
};

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
#[serde(deny_unknown_fields)]
pub struct SavedShard {
    pub session_id: String,
    pub snapshot_id: u64,
    pub world_file: String,
}

pub type SavedRoom = BTreeMap<String, SavedShard>;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailedLoad {
    pub session_id: String,
    pub world_file: String,
}

/// Only construct LoadFailure after identifying the native loader's failed file
/// and excluding disk, permission, configuration and Mod failures.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Failure {
    Retryable,
    LoadFailure {
        shards: BTreeMap<String, FailedLoad>,
    },
    Disk,
    Permission,
    Configuration,
    Mods,
    Unknown,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RunState {
    pub active: bool,
    pub failure: Option<Failure>,
    pub attempt_id: Option<String>,
}

impl RunState {
    fn matches_attempt(&self, id: &str) -> bool {
        self.attempt_id.as_deref() == Some(id)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureStage {
    Prepare,
    Startup,
    Runtime,
    Shutdown,
    Unknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureTriggerKind {
    Operation,
    ProcessExited,
    ForcedStop,
    AgentInterrupted,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailureTrigger {
    pub kind: FailureTriggerKind,
    pub stage: FailureStage,
    pub code: Option<ErrorCode>,
    pub operation_id: Option<String>,
    pub observed_at_ms: u64,
    pub force_requested: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ShardCause {
    LoginTransport,
    Runtime { identity: SessionIdentity },
    LoadFailure { file: FailedLoad },
    Unknown,
}

pub use crate::driver::ControlFailureReason;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlFailure {
    pub code: DriverErrorCode,
    pub reason: ControlFailureReason,
}

fn control_failure(state: &DriverState) -> Option<ControlFailure> {
    let error = state.failure.as_ref()?;
    Some(ControlFailure {
        code: error.code,
        reason: error.reason.unwrap_or(match error.code {
            DriverErrorCode::Transport => ControlFailureReason::Transport,
            _ => ControlFailureReason::InvalidFrame,
        }),
    })
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShardFailure {
    pub process_id: Option<String>,
    pub nonce: Option<String>,
    pub generation: Option<u64>,
    /// Included in the fault scope or observed to fail independently of cleanup.
    pub failed: bool,
    pub cause: ShardCause,
    /// Cleanup may invalidate the original evidence, but cannot create new permission.
    pub evidence_current: bool,
    pub native_diagnostics: NativeDiagnostics,
    pub control_error: Option<ControlFailure>,
    pub exit: Option<ExitSummary>,
}

impl ShardFailure {
    fn capture(state: Option<&DriverState>, failed: bool) -> Self {
        Self {
            process_id: state.map(|state| state.process_id.clone()),
            nonce: state.map(|state| state.nonce.clone()),
            generation: state.and_then(|state| state.generation),
            failed,
            cause: state.map_or(ShardCause::Unknown, shard_cause),
            evidence_current: true,
            native_diagnostics: state
                .map(|state| state.native_diagnostics.clone())
                .unwrap_or_default(),
            control_error: state.and_then(control_failure),
            exit: state.and_then(|state| state.exit.clone()),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureCause {
    LoginTransport,
    LuaError,
    RuntimeFailure,
    LoadFailure,
    OperationFailure,
    AgentInterrupted,
    Mixed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryStep {
    Catalog,
    RetryCatalog,
    PrepareTarget,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryErrorReason {
    MissingSession,
    SessionChanged,
    WorldAlreadyLoaded,
    InvalidCatalog,
    TargetNotFound,
    TargetPathMismatch,
    CurrentNotFound,
    TargetIsNewer,
    TargetNotSelected,
    NativeRecoveryFailed,
    MissingLatest,
    InvalidPath,
    MissingWorldFile,
    UncleanExit,
    InvalidEvidence,
    Deadline,
    Cancelled,
    Unavailable,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryError {
    pub step: RecoveryStep,
    pub code: ErrorCode,
    pub reason: RecoveryErrorReason,
}

impl RecoveryError {
    fn capture(step: RecoveryStep, error: &Error) -> Self {
        let reason = if error.is_shutdown() {
            RecoveryErrorReason::Cancelled
        } else if error.code == ErrorCode::Timeout {
            RecoveryErrorReason::Deadline
        } else {
            serde_json::from_value(error.details["reason"].clone())
                .unwrap_or(RecoveryErrorReason::Unavailable)
        };
        Self {
            step,
            code: error.code,
            reason,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryResult {
    Started,
    Succeeded,
    Interrupted,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailureRecord {
    pub attempt_id: String,
    pub cause: FailureCause,
    pub trigger: FailureTrigger,
    pub shards: BTreeMap<String, ShardFailure>,
    pub complete: bool,
    pub classification: Failure,
    pub catalog_error: Option<RecoveryError>,
    pub decision: Option<Decision>,
    pub restarts_used: u8,
    pub recovery_attempt_id: Option<String>,
    pub result: Option<RecoveryResult>,
}

impl FailureRecord {
    fn merge_observed(&mut self, states: &BTreeMap<String, DriverState>) {
        for (name, shard) in &mut self.shards {
            let source = states.get(name);
            shard.failed |= source.is_none_or(|source| {
                source.failure.is_some()
                    || source.load_failure.is_some()
                    || (!source.stopping
                        && !source.exit.as_ref().is_some_and(|exit| exit.forced)
                        && (!source.ready || !source.running))
                    || source.exit.as_ref().is_some_and(|exit| {
                        !exit.forced && exit.returncode.is_some_and(|code| code != 0)
                    })
            });
            if let Some(source) = source.filter(|source| {
                shard.process_id.as_ref() == Some(&source.process_id)
                    && shard.nonce.as_ref() == Some(&source.nonce)
            }) {
                shard.evidence_current &= match &shard.cause {
                    ShardCause::LoginTransport => source.login_retry_permitted(),
                    ShardCause::Runtime { identity } => {
                        source.generation == Some(identity.generation)
                            && source.native_ready
                            && source.runtime.as_ref().is_some_and(|runtime| {
                                runtime["session_id"].as_str() == Some(&identity.session_id)
                            })
                    }
                    ShardCause::LoadFailure { file } => {
                        source.generation == shard.generation
                            && verified_load_failure(source).is_some_and(|latest| {
                                latest.session_id == file.session_id
                                    && latest.world_file == file.world_file
                            })
                    }
                    ShardCause::Unknown => false,
                };
                shard.exit = source.exit.clone();
                shard.control_error = shard
                    .control_error
                    .take()
                    .or_else(|| control_failure(source));
                shard.native_diagnostics = source.native_diagnostics.clone();
            } else {
                shard.exit = None;
                shard.evidence_current = false;
            }
        }
    }

    fn describe_cause(&mut self) {
        let mut causes =
            self.shards
                .values()
                .filter(|shard| shard.failed)
                .map(|shard| match &shard.cause {
                    ShardCause::LoginTransport => FailureCause::LoginTransport,
                    ShardCause::LoadFailure { .. } => FailureCause::LoadFailure,
                    ShardCause::Runtime { .. } if self.trigger.code == Some(ErrorCode::Lua) => {
                        FailureCause::LuaError
                    }
                    ShardCause::Runtime { .. } => FailureCause::RuntimeFailure,
                    ShardCause::Unknown => FailureCause::OperationFailure,
                });
        let first = causes.next().unwrap_or(FailureCause::OperationFailure);
        self.cause = if causes.all(|cause| cause == first) {
            first
        } else {
            FailureCause::Mixed
        };
    }
}

fn shard_cause(state: &DriverState) -> ShardCause {
    if state.load_failure.is_some() {
        return verified_load_failure(state)
            .map_or(ShardCause::Unknown, |file| ShardCause::LoadFailure { file });
    }
    if state.login_retry_permitted() {
        return ShardCause::LoginTransport;
    }
    if state.native_ready
        && let Some(runtime) = &state.runtime
        && let Some(generation) = state.generation
        && let Some(session) = state
            .session_id
            .as_ref()
            .filter(|session| valid_text(session, 128))
        && runtime["session_id"].as_str() == Some(session)
    {
        return ShardCause::Runtime {
            identity: SessionIdentity {
                session_id: session.clone(),
                process_id: state.process_id.clone(),
                generation,
            },
        };
    }
    ShardCause::Unknown
}

fn verified_load_failure(state: &DriverState) -> Option<FailedLoad> {
    let record = state.load_failure.as_ref()?;
    let session = record["session_id"]
        .as_str()
        .filter(|value| valid_text(value, 128))?;
    let file = record["world_file"].as_str()?;
    if record["event"] != "world_load_failed"
        || record["nonce"] != state.nonce
        || record["generation"].as_u64() != state.generation
        || state.generation.is_none()
        || record["phase"] != "decode"
        || record["read_succeeded"] != true
        || record["callback_entered"] != false
        || record["decoder_unchanged"] != true
        || !record["source_bytes"]
            .as_u64()
            .is_some_and(|bytes| bytes > 0)
        || !matches!(
            record["reason"].as_str(),
            Some("parse_failed" | "decoded_nil" | "decoded_empty")
        )
        || native_path(&state.shard, session, file).is_err()
    {
        return None;
    }
    Some(FailedLoad {
        session_id: session.into(),
        world_file: file.into(),
    })
}

/// Every actual failure must provide its own evidence after whole-room cleanup.
fn classify(shards: &BTreeMap<String, ShardFailure>, trigger: &FailureTrigger) -> Failure {
    if shards.is_empty()
        || shards.values().any(|shard| {
            shard.control_error.is_some()
                || shard
                    .exit
                    .as_ref()
                    .is_none_or(|exit| !exit.output_drained || exit.protocol_error.is_some())
        })
    {
        return Failure::Unknown;
    }
    let mut loads = BTreeMap::new();
    let mut retryable = false;
    for (name, shard) in shards.iter().filter(|(_, shard)| shard.failed) {
        if !shard.evidence_current {
            return Failure::Unknown;
        }
        match &shard.cause {
            ShardCause::LoadFailure { file } => {
                loads.insert(name.clone(), file.clone());
            }
            ShardCause::LoginTransport
                if trigger.stage == FailureStage::Startup
                    && matches!(
                        trigger.kind,
                        FailureTriggerKind::Operation | FailureTriggerKind::ProcessExited
                    ) =>
            {
                retryable = true
            }
            ShardCause::Runtime { .. } => retryable = true,
            ShardCause::LoginTransport => return Failure::Unknown,
            ShardCause::Unknown => return Failure::Unknown,
        }
    }
    if retryable {
        Failure::Retryable
    } else if !loads.is_empty() {
        Failure::LoadFailure { shards: loads }
    } else {
        Failure::Unknown
    }
}

pub(crate) fn native_path(shard: &str, session: &str, file: &str) -> Result<PathBuf> {
    ensure!(
        valid_text(file, 4096) && !file.contains('\\'),
        "invalid native world path"
    );
    let relative = file.strip_prefix("save/").unwrap_or(file);
    let parts: Vec<_> = relative.split('/').collect();
    ensure!(
        parts.len() == 3
            && parts[0] == "session"
            && parts[1] == session
            && parts
                .iter()
                .all(|part| !part.is_empty() && !matches!(*part, "." | "..")),
        "native world path does not belong to its session"
    );
    Ok(Path::new(shard).join("save").join(relative))
}

pub(crate) fn nonempty_world_file(room: &RoomLock, path: &Path) -> bool {
    room.open_regular(path)
        .is_ok_and(|file| file.metadata().is_ok_and(|metadata| metadata.len() > 0))
}

pub(crate) fn require_target_file(
    room: &RoomLock,
    shard: &str,
    session: &str,
    file: &str,
) -> Result<()> {
    let path = native_path(shard, session, file).map_err(|error| {
        Error::new(ErrorCode::Protocol, error.to_string()).with_details(serde_json::json!({
            "shard": shard, "reason": RecoveryErrorReason::InvalidPath,
        }))
    })?;
    ensure!(
        nonempty_world_file(room, &path),
        Error::new(
            ErrorCode::Protocol,
            "recovery target world file is missing, unreadable or empty"
        )
        .with_details(
            serde_json::json!({"shard":shard, "reason":RecoveryErrorReason::MissingWorldFile})
        )
    );
    Ok(())
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ClosedReason {
    Disk,
    Permission,
    Configuration,
    Mods,
    UnknownFailure,
    UnprovenSnapshots,
    SessionChanged,
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
#[serde(deny_unknown_fields)]
pub struct FixedTarget {
    pub snapshot_id: u64,
    pub shards: SavedRoom,
    pub phase: TargetPhase,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CurrentRetry {
    pub not_before_ms: u64,
    pub started: bool,
    pub shards: SavedRoom,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryState {
    pub restarts_used: u8,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_open_window_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub closed: Option<ClosedReason>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry: Option<CurrentRetry>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target: Option<FixedTarget>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_failure: Option<FailureRecord>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
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
            self.restarts_used <= CURRENT_RESTARTS,
            "invalid recovery.restarts_used"
        );
        if let Some(failure) = &self.last_failure {
            ensure!(
                valid_text(&failure.attempt_id, 128),
                "invalid recovery.last_failure.attempt_id"
            );
        }
        if let Some(retry) = &self.retry {
            ensure!(
                self.restarts_used > 0 && !retry.shards.is_empty() && self.closed.is_none(),
                "invalid recovery.retry, its restart budget, or its shards"
            );
            for saved in retry.shards.values() {
                verify_saved(saved)?;
            }
        }
        if let Some(target) = &self.target {
            ensure!(!target.shards.is_empty(), "empty recovery.target.shards");
            for saved in target.shards.values() {
                verify_saved(saved)?;
                ensure!(
                    saved.snapshot_id == target.snapshot_id,
                    "inconsistent recovery.target.snapshot_id"
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
        // An unstarted attempt must still verify its recorded saves in the new window.
        self.retry = self.retry.take().filter(|retry| !retry.started);
        self.restarts_used = u8::from(self.retry.is_some());
        self.closed = None;
        if self
            .target
            .as_ref()
            .is_some_and(|target| target.phase == TargetPhase::Ready)
        {
            self.target = None;
        } else if let Some(target) = &mut self.target {
            // Truncation may have happened before preparation was marked complete.
            // Even a fresh budget must finish this same target instead of selecting an older one.
            target.phase = TargetPhase::Preparing;
        }
    }
}

pub fn load(room: &RoomLock) -> Result<RecoveryState> {
    decode(&room.read_state()?)
}

pub(crate) fn decode(
    document: &serde_json::Map<String, serde_json::Value>,
) -> Result<RecoveryState> {
    let state: RecoveryState = state_section(document, "recovery")?;
    state.validate()?;
    Ok(state)
}

fn update<T>(
    room: &mut RoomLock,
    change: impl FnOnce(&mut RecoveryState, &mut RunState) -> Result<T>,
) -> Result<T> {
    room.update_state(|document| {
        let mut state = decode(document)?;
        let mut run = decode_run(document, &state)?;
        let result = change(&mut state, &mut run)?;
        state.validate()?;
        document.insert("recovery".into(), serde_json::to_value(state)?);
        document.insert("agent_run".into(), serde_json::to_value(run)?);
        // update_state synchronizes the file and directory before returning this decision.
        Ok(result)
    })
}

fn decode_run(
    document: &serde_json::Map<String, serde_json::Value>,
    state: &RecoveryState,
) -> Result<RunState> {
    let run: RunState = state_section(document, "agent_run")?;
    if run.active
        || run.failure.is_some()
        || state.retry.as_ref().is_some_and(|retry| retry.started)
    {
        ensure!(
            run.attempt_id
                .as_deref()
                .is_some_and(|id| valid_text(id, 128)),
            "missing or invalid agent_run.attempt_id"
        );
    }
    if let Some(failure) = &run.failure
        && *failure != Failure::Unknown
    {
        ensure!(
            state
                .last_failure
                .as_ref()
                .is_some_and(|record| run.matches_attempt(&record.attempt_id)
                    && record.complete
                    && record.classification == *failure),
            "agent_run.failure lacks matching evidence in recovery.last_failure"
        );
    }
    Ok(run)
}

pub(crate) fn load_run(room: &RoomLock) -> Result<RunState> {
    Ok(load_with_run(room)?.1)
}

pub(crate) fn load_with_run(room: &RoomLock) -> Result<(RecoveryState, RunState)> {
    let document = room.read_state()?;
    let state = decode(&document)?;
    let run = decode_run(&document, &state)?;
    Ok((state, run))
}

/// The Agent completes its monotonic retry wait and catalog recheck before Room launch.
/// Persist the accepted operation ID before preparation can fail or any child can start.
pub(crate) fn begin_run(room: &mut RoomLock, attempt_id: String) -> Result<()> {
    ensure!(valid_text(&attempt_id, 128), "invalid agent_run.attempt_id");
    update(room, |state, run| {
        ensure!(
            state.closed.is_none() && run.failure.is_none(),
            "previous room failure is unresolved"
        );
        let recovering = state.retry.is_some()
            || state
                .target
                .as_ref()
                .is_some_and(|target| target.phase == TargetPhase::Starting);
        if let Some(retry) = &mut state.retry {
            ensure!(!retry.started, "retry is not ready to start");
            retry.started = true;
        }
        ensure!(
            state
                .target
                .as_ref()
                .is_none_or(|target| target.phase != TargetPhase::Preparing),
            "fixed target has not been prepared"
        );
        if recovering && let Some(failure) = &mut state.last_failure {
            failure.recovery_attempt_id = Some(attempt_id.clone());
            failure.result = Some(RecoveryResult::Started);
        }
        *run = RunState {
            active: true,
            failure: None,
            attempt_id: Some(attempt_id),
        };
        Ok(())
    })
}

/// Preserve the first trigger and causes while retaining later independent failures and force intent.
pub(crate) fn record_failure(
    room: &mut RoomLock,
    attempt_id: &str,
    trigger: FailureTrigger,
    required: &BTreeSet<String>,
    states: &BTreeMap<String, DriverState>,
    failed: &BTreeSet<String>,
) -> Result<()> {
    update(room, |state, run| {
        ensure!(run.matches_attempt(attempt_id), "room run attempt changed");
        if run.failure.is_some()
            && let Some(record) = state
                .last_failure
                .as_mut()
                .filter(|record| run.matches_attempt(&record.attempt_id))
        {
            record.trigger.force_requested |= trigger.force_requested;
            if !record.complete {
                record.merge_observed(states);
            }
            return Ok(());
        }
        let shards = required
            .iter()
            .map(|name| {
                let source = states.get(name);
                let failed = failed.contains(name)
                    || source.is_none_or(|state| {
                        !state.ready
                            || !state.running
                            || state.failure.is_some()
                            || state.load_failure.is_some()
                    });
                (name.clone(), ShardFailure::capture(source, failed))
            })
            .collect();
        let mut record = FailureRecord {
            attempt_id: attempt_id.to_owned(),
            cause: FailureCause::OperationFailure,
            trigger,
            shards,
            complete: false,
            classification: Failure::Unknown,
            catalog_error: None,
            decision: None,
            restarts_used: state.restarts_used,
            recovery_attempt_id: None,
            result: None,
        };
        record.describe_cause();
        state.last_failure = Some(record);
        run.failure = Some(Failure::Unknown);
        Ok(())
    })
}

pub(crate) fn merge_failure(
    room: &mut RoomLock,
    attempt_id: &str,
    states: &BTreeMap<String, DriverState>,
) -> Result<()> {
    update(room, |state, run| {
        ensure!(run.matches_attempt(attempt_id), "room run attempt changed");
        if let Some(record) = state.last_failure.as_mut().filter(|record| {
            run.matches_attempt(&record.attempt_id) && !record.complete && run.failure.is_some()
        }) {
            record.merge_observed(states);
        }
        Ok(())
    })
}

/// Return true when cleanup completed normally and Room can release its attempt.
/// An unfinished recovery retains its inactive identity until the next decision.
pub(crate) fn finish_failure(
    room: &mut RoomLock,
    attempt_id: &str,
    before: Option<&BTreeMap<String, DriverState>>,
    states: &BTreeMap<String, DriverState>,
    evidence_lost: bool,
) -> Result<bool> {
    update(room, |state, run| {
        ensure!(run.matches_attempt(attempt_id), "room run attempt changed");
        if evidence_lost {
            run.failure = Some(Failure::Unknown);
            run.active = false;
        }
        if run.failure.is_none() {
            if state.retry.as_ref().is_some_and(|retry| retry.started)
                || state
                    .target
                    .as_ref()
                    .is_some_and(|target| target.phase == TargetPhase::Starting)
            {
                run.active = false;
                if let Some(record) = state.last_failure.as_mut().filter(|record| {
                    record.recovery_attempt_id == run.attempt_id
                        && matches!(
                            record.result,
                            Some(RecoveryResult::Started | RecoveryResult::Interrupted)
                        )
                }) {
                    record.result = Some(RecoveryResult::Interrupted);
                }
            } else {
                *run = RunState::default();
            }
            return Ok(true);
        }
        if let Some(record) = state
            .last_failure
            .as_mut()
            .filter(|record| run.matches_attempt(&record.attempt_id))
        {
            if !record.complete {
                if let Some(before) = before {
                    record.merge_observed(before);
                }
                record.merge_observed(states);
                record.complete = true;
                record.classification = classify(&record.shards, &record.trigger);
            }
            if evidence_lost {
                record.classification = Failure::Unknown;
            }
            run.failure = Some(record.classification.clone());
        }
        run.active = false;
        Ok(false)
    })
}

/// An interrupted run permits bounded recovery when its failure could not be observed.
pub(crate) fn prepare_recovery(room: &mut RoomLock, now_ms: u64) -> Result<RunState> {
    update(room, |state, run| {
        let known = state.last_failure.as_ref().is_some_and(|record| {
            run.matches_attempt(&record.attempt_id)
                && record.trigger.kind != FailureTriggerKind::AgentInterrupted
                && !record.complete
        });
        if known && (run.active || run.failure.is_some()) {
            run.failure = Some(Failure::Unknown);
        } else if run.failure.is_none()
            && (run.active || state.retry.as_ref().is_some_and(|retry| retry.started))
        {
            if let Some(record) = state.last_failure.as_mut().filter(|record| {
                matches!(
                    record.result,
                    Some(RecoveryResult::Started | RecoveryResult::Interrupted)
                ) && record.recovery_attempt_id.is_some()
                    && record.recovery_attempt_id == run.attempt_id
            }) {
                record.result = Some(RecoveryResult::Interrupted);
            } else if !state.last_failure.as_ref().is_some_and(|record| {
                run.matches_attempt(&record.attempt_id)
                    && record.trigger.kind == FailureTriggerKind::AgentInterrupted
            }) {
                state.last_failure = Some(FailureRecord {
                    attempt_id: run
                        .attempt_id
                        .clone()
                        .context("missing agent_run.attempt_id")?,
                    cause: FailureCause::AgentInterrupted,
                    trigger: FailureTrigger {
                        kind: FailureTriggerKind::AgentInterrupted,
                        stage: FailureStage::Unknown,
                        code: None,
                        operation_id: None,
                        observed_at_ms: now_ms,
                        force_requested: false,
                    },
                    shards: BTreeMap::new(),
                    complete: false,
                    classification: Failure::Retryable,
                    catalog_error: None,
                    decision: None,
                    restarts_used: state.restarts_used,
                    recovery_attempt_id: None,
                    result: None,
                });
            }
        }
        Ok(run.clone())
    })
}

fn related_failure(state: &RecoveryState, run: &RunState) -> bool {
    state.last_failure.as_ref().is_some_and(|failure| {
        if let Some(attempt_id) = &run.attempt_id {
            failure.attempt_id == *attempt_id
                || (failure.recovery_attempt_id.as_ref() == Some(attempt_id)
                    && matches!(
                        failure.result,
                        Some(RecoveryResult::Started | RecoveryResult::Interrupted)
                    ))
        } else {
            state.retry.is_some()
                || state.target.as_ref().is_some_and(|target| {
                    matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
                })
        }
    })
}

fn record_decision(
    state: &mut RecoveryState,
    run: &mut RunState,
    decision: &Decision,
    related: bool,
) {
    if related && let Some(failure) = &mut state.last_failure {
        failure.decision = Some(decision.clone());
        failure.restarts_used = state.restarts_used;
    }
    *run = RunState::default();
}

pub(crate) fn recovery_failed(
    room: &mut RoomLock,
    step: RecoveryStep,
    error: &Error,
) -> Result<Decision> {
    update(room, |state, run| {
        let related = related_failure(state, run);
        if related && let Some(failure) = &mut state.last_failure {
            failure.catalog_error = Some(RecoveryError::capture(step, error));
        }
        let decision = state.close(if step == RecoveryStep::PrepareTarget {
            ClosedReason::RecoveryTargetFailed
        } else {
            ClosedReason::UnprovenSnapshots
        });
        record_decision(state, run, &decision, related);
        Ok(decision)
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
    if let Some(previous) = &state.retry
        && (previous.shards.keys().ne(latest.keys())
            || previous
                .shards
                .iter()
                .any(|(name, saved)| saved.session_id != latest[name].session_id))
    {
        return Ok(state.close(ClosedReason::SessionChanged));
    }
    let failed_snapshot_id = if let Failure::LoadFailure { shards } = failure {
        if shards.is_empty() {
            return Ok(state.close(ClosedReason::UnprovenLoadFailure));
        }
        let mut earliest = u64::MAX;
        for (name, failure) in shards {
            let Some(failed) = latest.get(name).filter(|saved| {
                saved.session_id == failure.session_id && saved.world_file == failure.world_file
            }) else {
                return Ok(state.close(ClosedReason::UnprovenLoadFailure));
            };
            earliest = earliest.min(failed.snapshot_id);
        }
        Some(earliest)
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
    if state.restarts_used < CURRENT_RESTARTS {
        state.restarts_used += 1;
        let not_before_ms = now_ms
            .checked_add(RETRY_DELAY.as_millis() as u64)
            .context("retry deadline overflow")?;
        state.retry = Some(CurrentRetry {
            not_before_ms,
            started: false,
            shards: latest.clone(),
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
    update(room, |state, run| {
        let related = related_failure(state, run);
        let decision = decide(state, &failure, required, catalogs, now_ms)?;
        record_decision(state, run, &decision, related);
        Ok(decision)
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
    update(room, |state, run| {
        let related = related_failure(state, run);
        let decision = resume(state, required, catalogs, now_ms)?;
        if let Some(decision) = &decision {
            record_decision(state, run, decision, related);
        }
        Ok(decision)
    })
}

fn resume(
    state: &mut RecoveryState,
    required: &BTreeSet<String>,
    catalogs: &Catalogs,
    now_ms: u64,
) -> Result<Option<Decision>> {
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
        if latest_room(required, catalogs).ok().as_ref() != Some(&retry.shards) {
            return Ok(Some(state.close(ClosedReason::CurrentSaveChanged)));
        }
        return Ok(Some(Decision::RetryCurrent {
            attempt: state.restarts_used,
            not_before_ms: retry.not_before_ms,
            shards: retry.shards.clone(),
        }));
    }
    Ok(None)
}

pub(crate) fn mark_prepared(room: &mut RoomLock) -> Result<()> {
    update(room, |state, _run| {
        ensure!(state.closed.is_none(), "room is closed after failure");
        let target = state.target.as_mut().context("no recovery target")?;
        ensure!(
            target.phase == TargetPhase::Preparing,
            "recovery is not preparing"
        );
        target.phase = TargetPhase::Starting;
        Ok(())
    })
}

/// Full-room readiness ends this incident, while its budget remains spent until a permitted reset.
pub fn mark_ready(room: &mut RoomLock, attempt_id: &str) -> Result<()> {
    update(room, |state, run| {
        ensure!(
            run.matches_attempt(attempt_id) && run.active && run.failure.is_none(),
            "room run attempt changed or failed"
        );
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
        state.retry = None;
        if let Some(failure) = state.last_failure.as_mut().filter(|failure| {
            failure.recovery_attempt_id.is_some() && failure.recovery_attempt_id == run.attempt_id
        }) {
            failure.result = Some(RecoveryResult::Succeeded);
        }
        Ok(())
    })
}

/// The runner measures continuous full-room readiness with a monotonic clock.
/// It must restart that measurement whenever the Agent process or any shard restarts.
pub fn reset_after_stable(room: &mut RoomLock, continuous_ready: Duration) -> Result<bool> {
    if continuous_ready < STABLE_RESET {
        return Ok(false);
    }
    update(room, |state, _run| {
        ensure!(
            state.closed.is_none() && state.retry.is_none(),
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
    update(room, |state, _run| {
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
    update(room, |state, _run| {
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
            shards: BTreeMap::from([(
                "Master".into(),
                FailedLoad {
                    session_id: catalogs["Master"].session_id.clone(),
                    world_file: catalogs["Master"].latest_world_file.clone(),
                },
            )]),
        }
    }

    #[test]
    fn unfinished_recovery_requires_its_identity_budget_and_fixed_paths() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let (required, catalogs) = catalogs();
        for (key, value, path) in [
            (
                "recovery",
                json!({"closed": "unknown_failure"}),
                "restarts_used",
            ),
            (
                "recovery",
                json!({"restarts_used": 1, "retry": {"started": false, "not_before_ms": 0}}),
                "shards",
            ),
            ("agent_run", json!({"active": true}), "agent_run.attempt_id"),
            (
                "agent_run",
                json!({"active": false, "attempt_id": "failed", "failure": {"kind":"retryable"}}),
                "recovery.last_failure",
            ),
        ] {
            room.update_state(|document| {
                document.clear();
                document.insert(key.into(), value);
                Ok(())
            })
            .unwrap();
            let before = room.read_state().unwrap();
            let error = resume_pending(&mut room, &required, &catalogs, 0).unwrap_err();
            assert!(error.to_string().contains(path), "{error}");
            assert_eq!(before, room.read_state().unwrap());
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
        assert!(!load(&room).unwrap().retry.unwrap().started);
        begin_run(&mut room, ulid::Ulid::generate().to_string()).unwrap();
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
        begin_run(&mut room, ulid::Ulid::generate().to_string()).unwrap();
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
        room.update_state(|control| {
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
            begin_run(&mut room, ulid::Ulid::generate().to_string()).unwrap();
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
        let stored = room.read_state().unwrap();
        assert_eq!(stored["recovery"]["target"]["snapshot_id"], 11);
        assert_eq!(stored["policy"], json!({"paused": false}));
        for catalog in catalogs.values_mut() {
            catalog.latest_world_file = path(&catalog.session_id, 11);
        }
        // Both shards were truncated, but the Agent died before marking preparation complete.
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
        assert_eq!(resumed.phase, TargetPhase::Preparing);
        mark_prepared(&mut room).unwrap();
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
        begin_run(&mut room, ulid::Ulid::generate().to_string()).unwrap();
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
    fn rollback_precedes_every_verified_failed_world() {
        for lagging in ["Master", "Caves"] {
            let (required, mut catalogs) = catalogs();
            let catalog = catalogs.get_mut(lagging).unwrap();
            catalog.latest_world_file = path(&catalog.session_id, 11);
            let shards: BTreeMap<_, _> = catalogs
                .iter()
                .map(|(name, catalog)| {
                    (
                        name.clone(),
                        FailedLoad {
                            session_id: catalog.session_id.clone(),
                            world_file: catalog.latest_world_file.clone(),
                        },
                    )
                })
                .collect();
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            for now in [0, 30_000] {
                let decision = on_failure(
                    &mut room,
                    Failure::LoadFailure {
                        shards: shards.clone(),
                    },
                    &required,
                    &catalogs,
                    now,
                )
                .unwrap();
                assert!(matches!(decision, Decision::RetryCurrent { .. }));
                begin_run(&mut room, ulid::Ulid::generate().to_string()).unwrap();
            }
            let Decision::Restore { target, .. } = on_failure(
                &mut room,
                Failure::LoadFailure { shards },
                &required,
                &catalogs,
                60_001,
            )
            .unwrap() else {
                panic!("expected a shared snapshot before both failed worlds");
            };
            assert_eq!(target.snapshot_id, 10);
        }
    }

    #[test]
    fn every_reported_load_failure_must_match_the_native_catalog() {
        let (required, catalogs) = catalogs();
        for shards in [
            BTreeMap::new(),
            BTreeMap::from([
                (
                    "Master".into(),
                    FailedLoad {
                        session_id: catalogs["Master"].session_id.clone(),
                        world_file: catalogs["Master"].latest_world_file.clone(),
                    },
                ),
                (
                    "Caves".into(),
                    FailedLoad {
                        session_id: "old_session".into(),
                        world_file: catalogs["Caves"].latest_world_file.clone(),
                    },
                ),
            ]),
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            assert!(matches!(
                on_failure(
                    &mut room,
                    Failure::LoadFailure { shards },
                    &required,
                    &catalogs,
                    0
                )
                .unwrap(),
                Decision::StayClosed {
                    reason: ClosedReason::UnprovenLoadFailure
                }
            ));
            assert_eq!(load(&room).unwrap().restarts_used, 0);
        }
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
        begin_run(&mut room, "ready".into()).unwrap();
        mark_ready(&mut room, "ready").unwrap();
        assert!(!reset_after_stable(&mut room, Duration::from_secs(1799)).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 1);
        assert!(reset_after_stable(&mut room, STABLE_RESET).unwrap());
        assert_eq!(load(&room).unwrap().restarts_used, 0);
        assert!(!reset_for_open_window(&mut room, 1_000).unwrap());
        on_failure(&mut room, Failure::Retryable, &required, &catalogs, 40_000).unwrap();
        begin_run(&mut room, "interrupted-retry".into()).unwrap();
        resume_pending(&mut room, &required, &catalogs, 41_000).unwrap();
        assert_eq!(load(&room).unwrap().restarts_used, 2);
        assert!(reset_for_open_window(&mut room, 2_000).unwrap());
        assert!(matches!(
            resume_pending(&mut room, &required, &catalogs, 42_000).unwrap(),
            Some(Decision::RetryCurrent { attempt: 1, .. })
        ));
    }
    fn failed_driver(shard: &str) -> DriverState {
        DriverState {
            shard: shard.into(),
            pid: 1,
            process_id: ulid::Ulid::generate().to_string(),
            nonce: "fixture".into(),
            generation: None,
            generation_changes: 0,
            native_ready: false,
            ready: false,
            running: false,
            stopping: false,
            exit: Some(ExitSummary {
                returncode: Some(1),
                forced: false,
                output_drained: true,
                protocol_error: None,
                stats: Default::default(),
            }),
            native_diagnostics: Default::default(),
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
            telemetry_rejections: Default::default(),
            last_telemetry_rejection: None,
            unmatched_replies: 0,
            recent_request: None,
        }
    }

    fn startup_trigger() -> FailureTrigger {
        FailureTrigger {
            kind: FailureTriggerKind::Operation,
            stage: FailureStage::Startup,
            code: Some(ErrorCode::Timeout),
            operation_id: Some("initial-operation".into()),
            observed_at_ms: 100,
            force_requested: false,
        }
    }

    fn login_driver(shard: &str) -> DriverState {
        let mut state = failed_driver(shard);
        state.native_diagnostics = serde_json::from_value(json!({
            "login_transport":{"kind":"login_transport","observed_at_ms":50,"count":1},
            "lua_error":null,
        }))
        .unwrap();
        state
    }

    #[test]
    fn earlier_lua_traceback_is_retained_as_a_diagnostic_on_a_later_exit() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        begin_run(&mut room, "attempt".into()).unwrap();
        let mut state = failed_driver("Master");
        state.generation = Some(2);
        state.native_ready = true;
        state.session_id = Some("SESSION".into());
        state.runtime = Some(json!({"session_id":"SESSION"}));
        state.native_diagnostics.lua_error = Some(crate::process::NativeDiagnostic {
            kind: crate::process::NativeDiagnosticKind::LuaError,
            observed_at_ms: 1,
            count: 1,
        });
        let failed = BTreeSet::from(["Master".into()]);
        let mut trigger = startup_trigger();
        trigger.kind = FailureTriggerKind::ProcessExited;
        trigger.stage = FailureStage::Runtime;
        trigger.observed_at_ms = 3_600_000;
        record_failure(
            &mut room,
            "attempt",
            trigger,
            &failed,
            &BTreeMap::from([("Master".into(), state)]),
            &failed,
        )
        .unwrap();
        let report = load(&room).unwrap().last_failure.unwrap();
        assert_eq!(report.cause, FailureCause::RuntimeFailure);
        assert_eq!(
            report.shards["Master"]
                .native_diagnostics
                .lua_error
                .as_ref()
                .unwrap()
                .observed_at_ms,
            1
        );
    }

    #[test]
    fn cleanup_cannot_promote_a_cause_or_borrow_a_siblings_permission() {
        for scenario in [
            "healthy_cleanup",
            "unknown_sibling",
            "late_login",
            "new_generation",
            "new_process",
            "driver_control",
            "process_protocol",
            "undrained",
            "unreaped",
            "shutdown_login",
            "mixed_decode_runtime",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            begin_run(&mut room, "initial".into()).unwrap();
            let required = BTreeSet::from(["Master".into(), "Caves".into()]);
            let mut master = login_driver("Master");
            let mut caves = failed_driver("Caves");
            caves.generation = Some(1);
            caves.native_ready = true;
            caves.ready = true;
            caves.running = true;
            caves.session_id = Some("SESSION".into());
            caves.runtime = Some(json!({"session_id":"SESSION"}));
            let mut trigger = startup_trigger();
            match scenario {
                "unknown_sibling" => caves = failed_driver("Caves"),
                "late_login" => master.native_diagnostics = Default::default(),
                "shutdown_login" => trigger.stage = FailureStage::Shutdown,
                "mixed_decode_runtime" => {
                    master.generation = Some(1);
                    master.load_failure = Some(
                        json!({"nonce":"fixture","generation":1,"event":"world_load_failed",
                        "session_id":"SESSION","world_file":"session/SESSION/0000000002","phase":"decode",
                        "reason":"parse_failed","read_succeeded":true,"callback_entered":false,"source_bytes":4,"decoder_unchanged":true}),
                    );
                    caves.running = false;
                }
                _ => {}
            }
            let mut states = BTreeMap::from([("Master".into(), master), ("Caves".into(), caves)]);
            record_failure(
                &mut room,
                "initial",
                trigger,
                &required,
                &states,
                &BTreeSet::from(["Master".into()]),
            )
            .unwrap();
            let first = load(&room).unwrap().last_failure.unwrap();
            assert!(!first.complete && matches!(first.classification, Failure::Unknown));
            assert!(load_run(&room).unwrap().active);
            states.get_mut("Caves").unwrap().running = false;
            states
                .get_mut("Caves")
                .unwrap()
                .exit
                .as_mut()
                .unwrap()
                .forced = true;
            states
                .get_mut("Caves")
                .unwrap()
                .exit
                .as_mut()
                .unwrap()
                .returncode = Some(-9);
            let master = states.get_mut("Master").unwrap();
            match scenario {
                "late_login" => {
                    master.native_diagnostics = login_driver("Master").native_diagnostics
                }
                "new_generation" => master.generation = Some(1),
                "new_process" => master.process_id = "different".into(),
                "driver_control" => {
                    master.failure = Some(crate::driver::DriverError {
                        code: DriverErrorCode::Protocol,
                        message: "diagnostic text can change".into(),
                        reason: Some(ControlFailureReason::Json),
                        request_id: None,
                        written: false,
                        accepted: false,
                    })
                }
                "process_protocol" => {
                    master.exit.as_mut().unwrap().protocol_error = Some("read failed".into())
                }
                "undrained" => master.exit.as_mut().unwrap().output_drained = false,
                "unreaped" => master.exit = None,
                _ => {}
            }
            finish_failure(&mut room, "initial", None, &states, false).unwrap();
            let record = load(&room).unwrap().last_failure.unwrap();
            let retryable = matches!(scenario, "healthy_cleanup" | "mixed_decode_runtime");
            assert_eq!(
                matches!(record.classification, Failure::Retryable),
                retryable,
                "{scenario}"
            );
            assert!(
                !matches!(record.classification, Failure::LoadFailure { .. }),
                "{scenario}"
            );
            assert_eq!(
                record.shards["Caves"].failed, first.shards["Caves"].failed,
                "{scenario}"
            );
            assert!(record.complete && !load_run(&room).unwrap().active);
            assert_eq!(record.cause, first.cause);
            if scenario == "driver_control" {
                assert!(
                    record.shards["Master"]
                        .exit
                        .as_ref()
                        .unwrap()
                        .protocol_error
                        .is_none()
                );
                assert!(matches!(
                    record.shards["Master"]
                        .control_error
                        .as_ref()
                        .unwrap()
                        .reason,
                    ControlFailureReason::Json
                ));
            }
        }
    }

    #[test]
    fn incomplete_failure_closes_and_interrupted_run_can_retry() {
        let (required, catalogs) = catalogs();
        for known in [true, false] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            begin_run(&mut room, "initial".into()).unwrap();
            if known {
                record_failure(
                    &mut room,
                    "initial",
                    startup_trigger(),
                    &required,
                    &BTreeMap::from([("Master".into(), login_driver("Master"))]),
                    &BTreeSet::new(),
                )
                .unwrap();
            }
            drop(room);
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            let run = prepare_recovery(&mut room, 100).unwrap();
            let failure = run.failure.unwrap_or(Failure::Retryable);
            let decision = on_failure(&mut room, failure, &required, &catalogs, 100).unwrap();
            assert_eq!(matches!(decision, Decision::RetryCurrent { .. }), !known);
            assert_eq!(load(&room).unwrap().restarts_used, u8::from(!known));
            let document = room.read_state().unwrap();
            assert_eq!(document["agent_run"]["active"], false);
            assert!(document["agent_run"]["failure"].is_null());
            assert!(document["recovery"]["last_failure"]["decision"].is_object());
        }
    }

    #[test]
    fn interruption_after_ready_is_a_new_incident() {
        let (required, catalogs) = catalogs();
        for ready in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            begin_run(&mut room, "initial".into()).unwrap();
            let states = required
                .iter()
                .map(|name| (name.clone(), login_driver(name)))
                .collect();
            record_failure(
                &mut room,
                "initial",
                startup_trigger(),
                &required,
                &states,
                &required,
            )
            .unwrap();
            finish_failure(&mut room, "initial", None, &states, false).unwrap();
            on_failure(&mut room, Failure::Retryable, &required, &catalogs, 0).unwrap();
            begin_run(&mut room, "retry".into()).unwrap();
            if ready {
                mark_ready(&mut room, "retry").unwrap();
                assert_eq!(
                    load(&room).unwrap().last_failure.unwrap().result,
                    Some(RecoveryResult::Succeeded)
                );
            }
            drop(room);
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            prepare_recovery(&mut room, 1234).unwrap();
            let state = load(&room).unwrap();
            assert_eq!(state.restarts_used, 1);
            let record = state.last_failure.unwrap();
            if ready {
                assert_eq!(record.cause, FailureCause::AgentInterrupted);
                assert_eq!(record.attempt_id, "retry");
                assert_eq!(record.trigger.observed_at_ms, 1234);
                assert_eq!(record.result, None);
                on_failure(&mut room, Failure::Retryable, &required, &catalogs, 1234).unwrap();
            } else {
                assert_eq!(record.cause, FailureCause::LoginTransport);
                assert_eq!(record.result, Some(RecoveryResult::Interrupted));
                resume_pending(&mut room, &required, &catalogs, 1234).unwrap();
            }
            assert_eq!(load(&room).unwrap().restarts_used, 2);
        }
    }

    #[test]
    fn final_exit_can_only_restrict_the_original_evidence() {
        for scenario in [
            "runtime",
            "generation",
            "unknown",
            "forced",
            "control",
            "protocol",
            "undrained",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            begin_run(&mut room, "initial".into()).unwrap();
            let required = BTreeSet::from(["Master".into(), "Caves".into()]);
            let mut master = failed_driver("Master");
            master.generation = Some(1);
            master.load_failure = Some(
                json!({"nonce":"fixture","generation":1,"event":"world_load_failed",
                "session_id":"SESSION","world_file":"session/SESSION/0000000002","phase":"decode",
                "reason":"parse_failed","read_succeeded":true,"callback_entered":false,"source_bytes":4,"decoder_unchanged":true}),
            );
            let mut caves = failed_driver("Caves");
            caves.exit = None;
            caves.generation = Some(1);
            caves.native_ready = true;
            caves.ready = true;
            caves.running = true;
            caves.session_id = Some("SESSION".into());
            caves.health = Some(json!({"generation":1,"control_ready":true}));
            if scenario != "unknown" {
                caves.runtime = Some(json!({"session_id":"SESSION"}));
            }
            let mut states = BTreeMap::from([("Master".into(), master), ("Caves".into(), caves)]);
            record_failure(
                &mut room,
                "initial",
                startup_trigger(),
                &required,
                &states,
                &BTreeSet::from(["Master".into()]),
            )
            .unwrap();
            let original = load(&room).unwrap().last_failure.unwrap();
            let caves = states.get_mut("Caves").unwrap();
            caves.running = false;
            caves.ready = false;
            caves.stopping = true;
            caves.exit = Some(ExitSummary {
                returncode: Some(if scenario == "forced" { -9 } else { 7 }),
                forced: scenario == "forced",
                output_drained: scenario != "undrained",
                protocol_error: (scenario == "protocol").then(|| "malformed frame".into()),
                stats: Default::default(),
            });
            if scenario == "generation" {
                caves.generation = Some(2);
                caves.runtime = None;
                caves.session_id = None;
            }
            if scenario == "control" {
                caves.failure = Some(crate::driver::DriverError {
                    code: DriverErrorCode::Protocol,
                    message: "diagnostic text can change".into(),
                    reason: Some(ControlFailureReason::Json),
                    request_id: None,
                    written: false,
                    accepted: false,
                });
            }
            finish_failure(&mut room, "initial", None, &states, false).unwrap();
            let final_record = load(&room).unwrap().last_failure.unwrap();
            assert_eq!(
                final_record.shards["Caves"].failed,
                scenario != "forced",
                "{scenario}"
            );
            assert_eq!(
                json!(final_record.shards["Caves"].cause),
                json!(original.shards["Caves"].cause)
            );
            assert_eq!(final_record.cause, original.cause);
            match scenario {
                "runtime" => assert!(matches!(final_record.classification, Failure::Retryable)),
                "forced" => assert!(matches!(
                    final_record.classification,
                    Failure::LoadFailure { .. }
                )),
                _ => assert!(
                    matches!(final_record.classification, Failure::Unknown),
                    "{scenario}"
                ),
            }
            finish_failure(&mut room, "initial", None, &BTreeMap::new(), false).unwrap();
            assert_eq!(
                json!(load(&room).unwrap().last_failure.unwrap()),
                json!(final_record)
            );
        }
    }

    #[test]
    fn writes_require_the_owned_attempt_and_preserve_completed_facts() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let (required, catalogs) = catalogs();
        let states = required
            .iter()
            .map(|name| (name.clone(), login_driver(name)))
            .collect();
        begin_run(&mut room, "current".into()).unwrap();
        let before = room.read_state().unwrap();
        assert!(
            record_failure(
                &mut room,
                "stale",
                startup_trigger(),
                &required,
                &states,
                &required
            )
            .is_err()
        );
        assert!(merge_failure(&mut room, "stale", &states).is_err());
        assert!(finish_failure(&mut room, "stale", None, &states, false).is_err());
        assert!(mark_ready(&mut room, "stale").is_err());
        assert_eq!(room.read_state().unwrap(), before);
        record_failure(
            &mut room,
            "current",
            startup_trigger(),
            &required,
            &states,
            &required,
        )
        .unwrap();
        finish_failure(&mut room, "current", None, &states, false).unwrap();
        let mut completed = load(&room).unwrap().last_failure.unwrap();
        let mut force = startup_trigger();
        force.kind = FailureTriggerKind::ForcedStop;
        force.force_requested = true;
        record_failure(
            &mut room,
            "current",
            force,
            &required,
            &BTreeMap::new(),
            &required,
        )
        .unwrap();
        completed.trigger.force_requested = true;
        assert_eq!(
            json!(load(&room).unwrap().last_failure.unwrap()),
            json!(completed)
        );
        on_failure(&mut room, Failure::Retryable, &required, &catalogs, 0).unwrap();
        begin_run(&mut room, "retry".into()).unwrap();
        mark_ready(&mut room, "retry").unwrap();
        let old = json!(load(&room).unwrap().last_failure.unwrap());
        finish_failure(&mut room, "retry", None, &BTreeMap::new(), true).unwrap();
        assert!(matches!(
            load_run(&room).unwrap().failure,
            Some(Failure::Unknown)
        ));
        assert!(!load_run(&room).unwrap().active);
        assert_eq!(json!(load(&room).unwrap().last_failure.unwrap()), old);
        assert_eq!(load(&room).unwrap().restarts_used, 1);
        assert!(matches!(
            on_failure(&mut room, Failure::Unknown, &required, &catalogs, 1000).unwrap(),
            Decision::StayClosed {
                reason: ClosedReason::UnknownFailure
            }
        ));
        assert_eq!(json!(load(&room).unwrap().last_failure.unwrap()), old);
        assert_eq!(load(&room).unwrap().restarts_used, 1);
        assert!(explicit_start(&mut room, true).unwrap());
        begin_run(&mut room, "later-failure".into()).unwrap();
        record_failure(
            &mut room,
            "later-failure",
            startup_trigger(),
            &required,
            &states,
            &required,
        )
        .unwrap();
        finish_failure(&mut room, "later-failure", None, &states, false).unwrap();
        on_failure(&mut room, Failure::Unknown, &required, &catalogs, 2000).unwrap();
        assert!(explicit_start(&mut room, true).unwrap());
        begin_run(&mut room, "unrelated".into()).unwrap();
        let unfinished = json!(load(&room).unwrap().last_failure.unwrap());
        finish_failure(&mut room, "unrelated", None, &states, true).unwrap();
        on_failure(&mut room, Failure::Unknown, &required, &catalogs, 3000).unwrap();
        assert_eq!(
            json!(load(&room).unwrap().last_failure.unwrap()),
            unfinished
        );
        recovery_failed(
            &mut room,
            RecoveryStep::Catalog,
            &Error::new(ErrorCode::Transport, "catalog failed"),
        )
        .unwrap();
        assert_eq!(
            json!(load(&room).unwrap().last_failure.unwrap()),
            unfinished
        );
    }

    #[test]
    fn retry_budget_and_result_follow_one_attempt_across_agent_restarts() {
        let (required, catalogs) = catalogs();
        for outcome in ["ready", "interrupted", "cancelled"] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            begin_run(&mut room, "initial".into()).unwrap();
            let states = required
                .iter()
                .map(|name| (name.clone(), login_driver(name)))
                .collect();
            record_failure(
                &mut room,
                "initial",
                startup_trigger(),
                &required,
                &states,
                &BTreeSet::new(),
            )
            .unwrap();
            finish_failure(&mut room, "initial", None, &states, false).unwrap();
            let failure = load_run(&room).unwrap().failure.unwrap();
            on_failure(&mut room, failure, &required, &catalogs, 100).unwrap();
            drop(room);
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            prepare_recovery(&mut room, 0).unwrap();
            assert!(matches!(
                resume_pending(&mut room, &required, &catalogs, 0).unwrap(),
                Some(Decision::RetryCurrent {
                    attempt: 1,
                    not_before_ms: 30100,
                    ..
                })
            ));
            // The Agent owns the monotonic wait; an earlier UTC clock cannot reject this launch.
            begin_run(&mut room, "retry-1".into()).unwrap();
            let document = room.read_state().unwrap();
            assert_eq!(document["agent_run"]["attempt_id"], "retry-1");
            assert_eq!(document["recovery"]["retry"]["started"], true);
            assert_eq!(document["recovery"]["last_failure"]["result"], "started");
            if outcome == "ready" {
                mark_ready(&mut room, "retry-1").unwrap();
                let record = load(&room).unwrap().last_failure.unwrap();
                assert_eq!(record.cause, FailureCause::LoginTransport);
                assert_eq!(record.result, Some(RecoveryResult::Succeeded));
                finish_failure(&mut room, "retry-1", None, &BTreeMap::new(), false).unwrap();
                begin_run(&mut room, "unrelated-maintenance".into()).unwrap();
                mark_ready(&mut room, "unrelated-maintenance").unwrap();
                finish_failure(
                    &mut room,
                    "unrelated-maintenance",
                    None,
                    &BTreeMap::new(),
                    false,
                )
                .unwrap();
                assert_eq!(
                    load(&room)
                        .unwrap()
                        .last_failure
                        .unwrap()
                        .recovery_attempt_id
                        .as_deref(),
                    Some("retry-1")
                );
                assert!(reset_for_open_window(&mut room, 40000).unwrap());
                assert!(load(&room).unwrap().last_failure.is_some());
            } else {
                for attempt in 1..=2 {
                    let mut expected = json!(load(&room).unwrap().last_failure.unwrap());
                    expected["result"] = json!("interrupted");
                    if outcome == "cancelled" {
                        assert!(
                            finish_failure(
                                &mut room,
                                &format!("retry-{attempt}"),
                                None,
                                &BTreeMap::new(),
                                false,
                            )
                            .unwrap()
                        );
                        assert!(!load_run(&room).unwrap().active);
                        assert_eq!(json!(load(&room).unwrap().last_failure.unwrap()), expected);
                    }
                    drop(room);
                    room = RoomLock::try_acquire(directory.path()).unwrap();
                    for now in [1000, 1001] {
                        prepare_recovery(&mut room, now).unwrap();
                        assert_eq!(json!(load(&room).unwrap().last_failure.unwrap()), expected);
                    }
                    let decision = resume_pending(&mut room, &required, &catalogs, 1000)
                        .unwrap()
                        .unwrap();
                    if attempt == 1 {
                        assert!(matches!(
                            decision,
                            Decision::RetryCurrent { attempt: 2, .. }
                        ));
                        begin_run(&mut room, "retry-2".into()).unwrap();
                    } else {
                        assert!(matches!(
                            decision,
                            Decision::StayClosed {
                                reason: ClosedReason::CurrentRetriesExhausted
                            }
                        ));
                    }
                }
                assert_eq!(load(&room).unwrap().restarts_used, 2);
                assert!(!load_run(&room).unwrap().active);
            }
        }
    }

    #[test]
    fn cancelled_fixed_target_start_preserves_the_report_and_replays_same_target() {
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let (required, mut catalogs) = catalogs();
        let states = catalogs
            .iter()
            .map(|(name, catalog)| {
                let mut state = failed_driver(name);
                state.generation = Some(1);
                state.load_failure = Some(json!({
                    "nonce":"fixture","generation":1,"event":"world_load_failed",
                    "session_id":catalog.session_id,"world_file":catalog.latest_world_file,
                    "phase":"decode","reason":"parse_failed","read_succeeded":true,
                    "callback_entered":false,"source_bytes":4,"decoder_unchanged":true,
                }));
                (name.clone(), state)
            })
            .collect();
        for attempt in 0..=CURRENT_RESTARTS {
            let id = format!("failed-{attempt}");
            begin_run(&mut room, id.clone()).unwrap();
            record_failure(
                &mut room,
                &id,
                startup_trigger(),
                &required,
                &states,
                &required,
            )
            .unwrap();
            finish_failure(&mut room, &id, None, &states, false).unwrap();
            let failure = load_run(&room).unwrap().failure.unwrap();
            assert!(matches!(failure, Failure::LoadFailure { .. }));
            on_failure(
                &mut room,
                failure,
                &required,
                &catalogs,
                u64::from(attempt) * 30_000,
            )
            .unwrap();
        }
        let target = load(&room).unwrap().target.unwrap();
        for (name, catalog) in &mut catalogs {
            catalog.latest_world_file = target.shards[name].world_file.clone();
            catalog
                .snapshots
                .retain(|snapshot| snapshot.snapshot_id <= target.snapshot_id);
        }
        mark_prepared(&mut room).unwrap();
        begin_run(&mut room, "restore-1".into()).unwrap();
        let mut expected = json!(load(&room).unwrap().last_failure.unwrap());
        expected["result"] = json!("interrupted");
        assert!(finish_failure(&mut room, "restore-1", None, &BTreeMap::new(), false).unwrap());
        assert!(!load_run(&room).unwrap().active);
        assert_eq!(json!(load(&room).unwrap().last_failure.unwrap()), expected);
        drop(room);
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        for now in [100_000, 100_001] {
            prepare_recovery(&mut room, now).unwrap();
            assert_eq!(json!(load(&room).unwrap().last_failure.unwrap()), expected);
        }
        let Some(Decision::Restore {
            target: replay,
            resumed: true,
        }) = resume_pending(&mut room, &required, &catalogs, 100_002).unwrap()
        else {
            panic!("expected the same fixed target")
        };
        assert_eq!(json!(replay), json!(target));
        assert_eq!(load(&room).unwrap().restarts_used, CURRENT_RESTARTS);
        mark_prepared(&mut room).unwrap();
        begin_run(&mut room, "restore-2".into()).unwrap();
        mark_ready(&mut room, "restore-2").unwrap();
        assert!(finish_failure(&mut room, "restore-2", None, &BTreeMap::new(), false).unwrap());
        assert!(load_run(&room).unwrap().attempt_id.is_none());
        let record = load(&room).unwrap().last_failure.unwrap();
        assert_eq!(record.result, Some(RecoveryResult::Succeeded));
        assert_eq!(record.recovery_attempt_id.as_deref(), Some("restore-2"));
    }

    #[test]
    fn catalog_failures_preserve_the_original_cause_and_spent_budget() {
        let (required, catalogs) = catalogs();
        for step in [
            RecoveryStep::Catalog,
            RecoveryStep::RetryCatalog,
            RecoveryStep::PrepareTarget,
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            begin_run(&mut room, "initial".into()).unwrap();
            let states = required
                .iter()
                .map(|name| (name.clone(), login_driver(name)))
                .collect();
            record_failure(
                &mut room,
                "initial",
                startup_trigger(),
                &required,
                &states,
                &BTreeSet::new(),
            )
            .unwrap();
            finish_failure(&mut room, "initial", None, &states, false).unwrap();
            if step != RecoveryStep::Catalog {
                on_failure(&mut room, Failure::Retryable, &required, &catalogs, 0).unwrap();
            }
            recovery_failed(
                &mut room,
                step,
                &Error::new(ErrorCode::Conflict, "native recovery refused the request")
                    .with_details(json!({"reason":"missing_session"})),
            )
            .unwrap();
            let state = load(&room).unwrap();
            assert_eq!(state.restarts_used, u8::from(step != RecoveryStep::Catalog));
            assert!(state.retry.is_none() && state.closed.is_some());
            let record = state.last_failure.unwrap();
            assert_eq!(record.cause, FailureCause::LoginTransport);
            assert!(matches!(record.classification, Failure::Retryable));
            assert!(matches!(
                record.catalog_error.unwrap().reason,
                RecoveryErrorReason::MissingSession
            ));
            assert!(!load_run(&room).unwrap().active && load_run(&room).unwrap().failure.is_none());
        }
    }

    #[test]
    fn only_verified_native_decode_failures_allow_rollback() {
        let record = json!({"nonce":"fixture","generation":1,"event":"world_load_failed",
            "session_id":"SESSION","world_file":"session/SESSION/0000000002","phase":"decode",
            "reason":"parse_failed","read_succeeded":true,"callback_entered":false,"source_bytes":4,"decoder_unchanged":true});
        let classify = |states: &[DriverState]| {
            super::classify(
                &states
                    .iter()
                    .map(|state| {
                        (
                            state.shard.clone(),
                            ShardFailure::capture(Some(state), true),
                        )
                    })
                    .collect(),
                &FailureTrigger {
                    kind: FailureTriggerKind::Operation,
                    stage: FailureStage::Startup,
                    code: Some(ErrorCode::Timeout),
                    operation_id: None,
                    observed_at_ms: 0,
                    force_requested: false,
                },
            )
        };
        let mut state = failed_driver("Master");
        state.generation = Some(1);
        state.load_failure = Some(record.clone());
        assert!(
            matches!(classify(std::slice::from_ref(&state)), Failure::LoadFailure {shards} if shards["Master"].world_file == "session/SESSION/0000000002")
        );
        let mut caves = state.clone();
        caves.shard = "Caves".into();
        caves.load_failure.as_mut().unwrap()["world_file"] = json!("session/SESSION/0000000001");
        for states in [[state.clone(), caves.clone()], [caves, state.clone()]] {
            let Failure::LoadFailure { shards } = classify(&states) else {
                panic!("expected both failed world files");
            };
            assert_eq!(shards.len(), 2);
            assert_eq!(shards["Master"].world_file, "session/SESSION/0000000002");
            assert_eq!(shards["Caves"].world_file, "session/SESSION/0000000001");
        }
        for (field, value) in BTreeMap::from([
            ("phase", json!("read")),
            ("read_succeeded", json!(false)),
            ("callback_entered", json!(true)),
            ("source_bytes", json!(0)),
            ("reason", json!("native_error")),
            ("decoder_unchanged", json!(false)),
            ("nonce", json!("stale")),
            ("generation", json!(2)),
            ("world_file", json!("session/SESSION/../outside")),
        ]) {
            let mut changed = state.clone();
            changed.load_failure.as_mut().unwrap()[field] = value;
            assert!(matches!(classify(&[changed]), Failure::Unknown), "{field}");
        }
        let mut ready = state.clone();
        ready.load_failure = None;
        ready.native_ready = true;
        ready.runtime = Some(json!({"session_id":"SESSION"}));
        ready.session_id = Some("SESSION".into());
        assert!(matches!(
            classify(std::slice::from_ref(&ready)),
            Failure::Retryable
        ));
        ready.exit.as_mut().unwrap().output_drained = false;
        assert!(matches!(classify(&[ready]), Failure::Unknown));
    }
}
