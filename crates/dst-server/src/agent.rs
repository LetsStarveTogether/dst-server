//! The room Agent owns policies and recovery across individual client lifetimes.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    sync::{Semaphore, mpsc, oneshot, watch},
    task::JoinSet,
    time::Instant,
};

use crate::{
    driver::DriverOptions,
    model::{
        self, Envelope, Error, ErrorCode, OperationHistory, OperationStatus, Phase, Request, Target,
    },
    policy::{self, ModPhase, Policy, PolicyState},
    preloader,
    recovery::{self, Decision, Failure, TargetPhase},
    room::Room,
    rpc::{self, EventHub},
};

pub struct Options {
    pub cluster: PathBuf,
    pub executable: PathBuf,
    pub socket: Option<PathBuf>,
    pub driver: DriverOptions,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct RunState {
    active: bool,
    failure: Option<FailureRecord>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum FailureRecord {
    Retryable,
    LoadFailure {
        shard: String,
        session_id: String,
        world_file: String,
    },
    Disk,
    Permission,
    Configuration,
    Mods,
    Unknown,
}

impl From<Failure> for FailureRecord {
    fn from(value: Failure) -> Self {
        match value {
            Failure::Retryable => Self::Retryable,
            Failure::LoadFailure {
                shard,
                session_id,
                world_file,
            } => Self::LoadFailure {
                shard,
                session_id,
                world_file,
            },
            Failure::Disk => Self::Disk,
            Failure::Permission => Self::Permission,
            Failure::Configuration => Self::Configuration,
            Failure::Mods => Self::Mods,
            Failure::Unknown => Self::Unknown,
        }
    }
}

impl From<FailureRecord> for Failure {
    fn from(value: FailureRecord) -> Self {
        match value {
            FailureRecord::Retryable => Self::Retryable,
            FailureRecord::LoadFailure {
                shard,
                session_id,
                world_file,
            } => Self::LoadFailure {
                shard,
                session_id,
                world_file,
            },
            FailureRecord::Disk => Self::Disk,
            FailureRecord::Permission => Self::Permission,
            FailureRecord::Configuration => Self::Configuration,
            FailureRecord::Mods => Self::Mods,
            FailureRecord::Unknown => Self::Unknown,
        }
    }
}

struct Controller {
    room: Arc<Room>,
    started: Instant,
    initialized: AtomicBool,
    closing: AtomicBool,
    mutation: Arc<Semaphore>,
    interrupted: watch::Sender<u64>,
    closing_game: AtomicBool,
    operations: Mutex<OperationHistory>,
    stable: Mutex<Option<Instant>>,
    unhealthy: Mutex<Option<Instant>>,
    error: Mutex<Option<String>>,
}

impl Controller {
    fn policy(&self) -> model::Result<Policy> {
        self.room.with_lock(|lock| Policy::load(lock))
    }
    fn policy_state(&self) -> model::Result<PolicyState> {
        self.room.with_lock(|lock| PolicyState::load(lock))
    }
    fn recovery(&self) -> model::Result<recovery::RecoveryState> {
        self.room.with_lock(|lock| recovery::load(lock))
    }
    fn run_state(&self) -> model::Result<RunState> {
        self.room.with_lock(|lock| {
            Ok(lock
                .read_control()?
                .remove("agent_run")
                .map(serde_json::from_value)
                .transpose()?
                .unwrap_or_default())
        })
    }
    fn save_run(&self, state: RunState) -> model::Result<()> {
        self.room.with_lock(|lock| {
            lock.update_control(|control| {
                control.insert("agent_run".into(), serde_json::to_value(state)?);
                Ok(())
            })
        })
    }
    fn required(&self) -> BTreeSet<String> {
        self.room
            .status()
            .shards
            .into_iter()
            .map(|shard| shard.name)
            .collect()
    }

    async fn initialize(&self) -> model::Result<()> {
        self.room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            state.resume_after_restart(Utc::now())?;
            if Policy::load(lock)?.mod_auto_update
                && state.mods.phase == ModPhase::Idle
                && crate::mods::prepare_shared(lock)?.has_code
            {
                state.mods.phase = ModPhase::Pending;
            }
            state.save(lock)
        })?;
        let previous = self.run_state()?;
        let state = self.recovery()?;
        let pending = state.retry.is_some()
            || state.target.as_ref().is_some_and(|target| {
                matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
            });
        if previous.active || previous.failure.is_some() || pending {
            let catalogs =
                match preloader::catalog(self.room.clone(), Duration::from_secs(120)).await {
                    Ok(catalogs) => catalogs,
                    Err(error) => {
                        self.room.with_lock(|lock| {
                            recovery::on_failure(
                                lock,
                                Failure::Unknown,
                                &self.required(),
                                &BTreeMap::new(),
                                now_ms(),
                            )
                        })?;
                        self.save_run(RunState::default())?;
                        return Err(error);
                    }
                };
            let unstarted = state.retry.as_ref().is_some_and(|retry| !retry.started)
                || state
                    .target
                    .as_ref()
                    .is_some_and(|target| target.phase == TargetPhase::Preparing);
            if pending && (unstarted || previous.failure.is_none()) {
                self.room.with_lock(|lock| {
                    recovery::resume_pending(lock, &self.required(), &catalogs, now_ms())
                })?;
            } else {
                let failure = previous
                    .failure
                    .map(Into::into)
                    .unwrap_or(Failure::Retryable);
                self.room.with_lock(|lock| {
                    recovery::on_failure(lock, failure, &self.required(), &catalogs, now_ms())
                })?;
            }
            self.save_run(RunState::default())?;
        }
        self.initialized.store(true, Ordering::Release);
        Ok(())
    }

    fn status(&self) -> model::Result<Value> {
        let mut status = self.room.status();
        let history = self.operations.lock().unwrap().clone();
        status.operations.current = history.current.or(status.operations.current);
        status.operations.last = history
            .last
            .into_iter()
            .chain(status.operations.last)
            .max_by_key(|operation| operation.completed_at_ns);
        let mut value = serde_json::to_value(status).map_err(internal)?;
        let recovery = self.recovery()?;
        let policy = self.policy()?;
        let state = self.policy_state()?;
        value["agent"] = json!({
            "ready":self.initialized.load(Ordering::Acquire),
            "schedule":policy.schedule_at(Utc::now()).map_err(internal)?,
            "policy":policy, "maintenance":state.mods, "recovery":recovery,
            "error":self.error.lock().unwrap().clone(),
            "requires_container_restart":self.room.requires_container_restart(),
            "telemetry":self.room.telemetry_stats(),
        });
        if value["phase"] == "stopped"
            && (recovery.closed.is_some() || matches!(state.mods.phase, ModPhase::Failed))
        {
            value["phase"] = json!("failed");
        }
        Ok(value)
    }

    async fn invoke(self: &Arc<Self>, envelope: Envelope) -> model::Result<Value> {
        envelope.validate()?;
        if matches!(envelope.request, Request::Status {}) && envelope.target == Target::Room {
            return self.status();
        }
        if !envelope.request.mutating()
            || matches!(envelope.request, Request::ReleaseArchive { .. })
        {
            return self.room.invoke_until_complete(envelope).await;
        }
        if self.closing.load(Ordering::Acquire) || !self.initialized.load(Ordering::Acquire) {
            return Err(Error::new(
                ErrorCode::NotReady,
                "Agent is preparing or closing the room",
            ));
        }
        let killing = matches!(envelope.request, Request::Kill {});
        if !killing && self.closing_game.load(Ordering::Acquire) {
            return Err(Error::new(
                ErrorCode::Busy,
                "scheduled room shutdown is pending",
            ));
        }
        if killing {
            self.interrupted
                .send_modify(|revision| *revision = revision.wrapping_add(1));
        }
        let maintenance = self.policy_state()?.mods;
        if !killing
            && !matches!(envelope.request, Request::UpdateMods { .. })
            && !matches!(maintenance.phase, ModPhase::Idle | ModPhase::Failed)
        {
            return Err(Error::new(
                ErrorCode::Busy,
                "room Mod maintenance is pending",
            ));
        }
        let permit =
            if killing {
                None
            } else {
                Some(self.mutation.clone().try_acquire_owned().map_err(|_| {
                    Error::new(ErrorCode::Busy, "another room operation is running")
                })?)
            };
        let wait = envelope.timeout()?;
        let operation = OperationStatus {
            id: ulid::Ulid::new().to_string(),
            method: envelope.request.method().into(),
            target: envelope.target.clone(),
            started_at_ns: now_ns(),
            completed_at_ns: None,
            result: None,
        };
        self.operations.lock().unwrap().current = Some(operation.clone());
        let controller = self.clone();
        let mut interrupted = self.interrupted.subscribe();
        let (reply, response) = oneshot::channel();
        tokio::spawn(async move {
            let _permit = permit;
            let result = tokio::select! {
                biased;
                _ = interrupted.changed(), if !killing => Err(Error::new(ErrorCode::Unknown, "operation wait was interrupted by room shutdown")),
                result = controller.execute(envelope) => result,
            };
            let mut completed = operation;
            completed.completed_at_ns = Some(now_ns());
            completed.result = Some(result.clone().into());
            let mut history = controller.operations.lock().unwrap();
            if history
                .current
                .as_ref()
                .is_some_and(|current| current.id == completed.id)
            {
                history.current = None;
            }
            history.last = Some(completed);
            drop(history);
            drop(_permit);
            let _ = reply.send(result);
        });
        tokio::time::timeout(wait, response)
            .await
            .map_err(|_| {
                Error::new(
                    ErrorCode::Unknown,
                    "caller wait expired; the Agent still owns the operation",
                )
            })?
            .map_err(|_| {
                Error::new(
                    ErrorCode::Unknown,
                    "operation worker ended before confirmation",
                )
            })?
    }

    async fn execute(self: &Arc<Self>, mut envelope: Envelope) -> model::Result<Value> {
        // The retained worker waits for the room; only invoke applies the caller's deadline.
        envelope.timeout = None;
        match &envelope.request {
            Request::Start {} => self.start(true).await,
            Request::Restart { notice } => {
                self.room
                    .invoke_until_complete(Envelope::new(
                        Target::Room,
                        Request::Stop {
                            notice: notice.clone(),
                        },
                    )?)
                    .await?;
                self.record_stop()?;
                self.start(false).await
            }
            Request::Kill {} => {
                let result = self.room.invoke_until_complete(envelope).await;
                if !self.room.requires_container_restart() {
                    self.room
                        .force_fault(Error::new(
                            ErrorCode::Unknown,
                            "room was forcibly stopped during preparation",
                        ))
                        .await?;
                }
                result
            }
            Request::Stop { .. } => {
                let result = self.room.invoke_until_complete(envelope).await;
                if result.is_ok() {
                    self.record_stop()?;
                }
                result
            }
            Request::UpdateMods { restart, notice } => {
                self.manual_mod_update(*restart, notice.clone()).await
            }
            Request::Configure { .. } => {
                let result = self.room.invoke_until_complete(envelope).await?;
                if result["changed"].as_array().is_some_and(|files| {
                    files
                        .iter()
                        .any(|file| file == "mods/dedicated_server_mods_setup.lua")
                }) {
                    self.room.with_lock(|lock| {
                        let mut state = PolicyState::load(lock)?;
                        if Policy::load(lock)?.mod_auto_update
                            && state.mods.phase == ModPhase::Idle
                            && crate::mods::prepare_shared(lock)?.has_code
                        {
                            state.mods.phase = ModPhase::Pending;
                            state.save(lock)?;
                        }
                        Ok(())
                    })?;
                }
                Ok(result)
            }
            _ => self.room.invoke_until_complete(envelope).await,
        }
    }

    async fn start(&self, explicit: bool) -> model::Result<Value> {
        if self.closing.load(Ordering::Acquire) || self.closing_game.load(Ordering::Acquire) {
            return Err(Error::new(ErrorCode::NotReady, "Agent is closing"));
        }
        if self.room.status().phase == Phase::Running {
            return self.status();
        }
        if explicit {
            let policy = self.policy()?;
            let all_day = policy
                .schedule_at(Utc::now())
                .map_err(internal)?
                .next_change
                .is_none();
            self.room
                .with_lock(|lock| recovery::explicit_start(lock, all_day))?;
        }
        let state = self.recovery()?;
        if let Some(reason) = state.closed {
            return Err(
                Error::new(ErrorCode::NotReady, "room recovery is exhausted or unsafe")
                    .with_details(json!({"reason":reason})),
            );
        }
        if let Some(retry) = state.retry {
            // A fresh monotonic wait is conservative across container and wall-clock changes.
            tokio::time::sleep(recovery::RETRY_DELAY).await;
            if !self
                .policy()?
                .schedule_at(Utc::now())
                .map_err(internal)?
                .open
                && !explicit
            {
                return Err(Error::new(
                    ErrorCode::NotReady,
                    "scheduled closing deferred the pending retry",
                ));
            }
            let catalogs = preloader::catalog(self.room.clone(), Duration::from_secs(120)).await?;
            let decision = self.room.with_lock(|lock| {
                recovery::resume_pending(lock, &self.required(), &catalogs, now_ms())
            })?;
            if !matches!(decision, Some(Decision::RetryCurrent { .. })) {
                return Err(
                    Error::new(ErrorCode::NotReady, "saved room changed before the retry")
                        .with_details(json!({"decision":decision})),
                );
            }
            self.room.with_lock(|lock| {
                recovery::begin_current_retry(lock, now_ms().max(retry.not_before_ms))
            })?;
        } else if let Some(target) = state.target
            && matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
        {
            preloader::apply(self.room.clone(), target, Duration::from_secs(120)).await?;
            self.room.with_lock(recovery::begin_recovered_room)?;
        }
        if self.closing.load(Ordering::Acquire) || self.closing_game.load(Ordering::Acquire) {
            return Err(Error::new(
                ErrorCode::NotReady,
                "Agent closed before process launch",
            ));
        }
        self.save_run(RunState {
            active: true,
            failure: None,
        })?;
        let result = self
            .room
            .invoke_until_complete(Envelope::new(Target::Room, Request::Start {})?)
            .await;
        match &result {
            Ok(_) => {
                self.room.with_lock(recovery::mark_ready)?;
                *self.stable.lock().unwrap() = Some(Instant::now());
                *self.error.lock().unwrap() = None;
            }
            Err(error) if !self.room.requires_container_restart() => {
                let _ = self.room.force_fault(error.clone()).await;
            }
            Err(_) => {}
        }
        result
    }

    fn record_stop(&self) -> model::Result<()> {
        *self.stable.lock().unwrap() = None;
        if self.room.requires_container_restart() {
            return Ok(());
        }
        self.room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            let now = Utc::now();
            state.record_clean_shutdown(now);
            if state.mods.phase == ModPhase::Updating {
                state.finish_mod_update(now, Err("Mod update interrupted by shutdown".into()))?;
            }
            lock.update_control(|control| {
                control.insert("policy_state".into(), serde_json::to_value(state)?);
                control.insert(
                    "agent_run".into(),
                    serde_json::to_value(RunState::default())?,
                );
                Ok(())
            })
        })
    }

    fn base_inputs(&self, status: &model::RoomStatus, busy: bool) -> model::Result<policy::Inputs> {
        Ok(policy::Inputs {
            game_running: status
                .shards
                .iter()
                .any(|shard| shard.readiness.process_running),
            all_ready: status.phase == Phase::Running
                && status.shards.iter().all(|shard| shard.readiness.ready()),
            busy,
            writes_in_progress: status.operations.current.as_ref().is_some_and(|operation| {
                matches!(operation.method.as_str(), "update_mods" | "configure")
            }),
            clean_stop_in_progress: status.phase == Phase::Stopping
                && !self.room.requires_container_restart(),
            faulted: self.recovery()?.closed.is_some() || self.room.requires_container_restart(),
            expected_shards: status
                .shards
                .iter()
                .map(|shard| shard.name.clone())
                .collect(),
            monotonic: Some(self.started.elapsed()),
            ..Default::default()
        })
    }

    async fn inputs(
        &self,
        status: &model::RoomStatus,
        busy: bool,
    ) -> model::Result<policy::Inputs> {
        let mut inputs = self.base_inputs(status, busy)?;
        if inputs.all_ready {
            for shard in &status.shards {
                let request = Envelope {
                    target: Target::Shard(shard.name.clone()),
                    request: Request::Presence {},
                    timeout: Some(5.0),
                };
                if let Ok(value) = self.room.invoke_until_complete(request).await {
                    inputs.mods_outdated |= value["outdated_mods"].as_bool().unwrap_or(false)
                        || value["outdated_mods"]
                            .as_array()
                            .is_some_and(|items| !items.is_empty());
                    let fields = [
                        "session_id",
                        "observation",
                        "reliable",
                        "client_count",
                        "player_count",
                        "idle_seconds",
                        "observed_seconds",
                    ];
                    let presence = Value::Object(
                        fields
                            .into_iter()
                            .map(|field| (field.into(), value[field].clone()))
                            .collect(),
                    );
                    if let Ok(presence) = serde_json::from_value(presence) {
                        inputs.presence.insert(shard.name.clone(), presence);
                    }
                }
            }
            if let Ok(world) = self
                .room
                .invoke_until_complete(Envelope {
                    target: Target::Shard(status.master.clone()),
                    request: Request::World {},
                    timeout: Some(5.0),
                })
                .await
            {
                inputs.master_day = world["day"].as_u64();
            }
        }
        Ok(inputs)
    }

    async fn tick(self: &Arc<Self>) -> model::Result<()> {
        if !self.initialized.load(Ordering::Acquire) {
            return Ok(());
        }
        if self.room.requires_container_restart() {
            return Ok(());
        }
        if self.room.status().phase == Phase::Running {
            let healthy = tokio::time::timeout(Duration::from_secs(5), self.room.refresh_health())
                .await
                .is_ok_and(|result| result.is_ok());
            let expired = {
                let mut unhealthy = self.unhealthy.lock().unwrap();
                if healthy {
                    *unhealthy = None;
                    false
                } else {
                    unhealthy.get_or_insert_with(Instant::now).elapsed() >= Duration::from_secs(300)
                }
            };
            if expired {
                self.room
                    .force_fault(Error::new(
                        ErrorCode::NotReady,
                        "required control stayed unhealthy for 300 seconds",
                    ))
                    .await?;
                return Ok(());
            }
            if healthy {
                let stable = self
                    .stable
                    .lock()
                    .unwrap()
                    .get_or_insert_with(Instant::now)
                    .elapsed();
                if stable >= recovery::STABLE_RESET {
                    let state = self.recovery()?;
                    if state.restarts_used > 0 || state.target.is_some() {
                        self.room
                            .with_lock(|lock| recovery::reset_after_stable(lock, stable))?;
                    }
                }
            } else {
                *self.stable.lock().unwrap() = None;
            }
        } else {
            *self.stable.lock().unwrap() = None;
            *self.unhealthy.lock().unwrap() = None;
        }
        let mut interrupted = self.interrupted.subscribe();
        let revision = *interrupted.borrow();
        let history = self.operations.lock().unwrap().clone();
        let before = self.room.status();
        // Native queries can take seconds. Acquire mutation ownership only once
        // they finish, then discard observations crossed by another operation.
        let observed = if history.current.is_some()
            || before.operations.current.is_some()
            || self.mutation.available_permits() == 0
        {
            self.base_inputs(&before, true)?
        } else {
            self.inputs(&before, false).await?
        };
        let permit = self.mutation.clone().try_acquire_owned().ok();
        let current = self.room.status();
        let inputs = if permit.is_some()
            && history.current.is_none()
            && before.operations.current.is_none()
            && history == *self.operations.lock().unwrap()
            && before == current
            && revision == *interrupted.borrow()
        {
            observed
        } else {
            self.base_inputs(&current, true)?
        };
        let plan = self.room.with_lock(|lock| {
            let policy = Policy::load(lock)?;
            let mut state = PolicyState::load(lock)?;
            let plan = policy.plan(&mut state, Utc::now(), &inputs)?;
            if let Some(opening) = plan.new_opening {
                recovery::reset_for_open_window(lock, opening.timestamp_millis().try_into()?)?;
            }
            state.save(lock)?;
            Ok(plan)
        })?;
        if let Some(action) = plan.action {
            let priority_stop = matches!(
                action,
                policy::Action::Stop {
                    reason: policy::StopReason::Schedule,
                    ..
                }
            );
            if permit.is_none() && !priority_stop {
                return Ok(());
            }
            if priority_stop && self.closing_game.swap(true, Ordering::AcqRel) {
                return Ok(());
            }
            if priority_stop {
                self.interrupted
                    .send_modify(|revision| *revision = revision.wrapping_add(1));
                interrupted = self.interrupted.subscribe();
            }
            let controller = self.clone();
            tokio::spawn(async move {
                let _permit = match permit {
                    Some(permit) => permit,
                    None => match controller.mutation.clone().acquire_owned().await {
                        Ok(permit) => permit,
                        Err(_) => return,
                    },
                };
                let result = tokio::select! {
                    biased;
                    _ = interrupted.changed() => Err(Error::new(ErrorCode::Unknown, "scheduled operation was interrupted")),
                    result = controller.action(action) => result,
                };
                if priority_stop {
                    controller.closing_game.store(false, Ordering::Release);
                }
                if let Err(error) = result {
                    *controller.error.lock().unwrap() = Some(error.to_string());
                }
            });
        }
        Ok(())
    }

    async fn action(&self, action: policy::Action) -> model::Result<()> {
        match action {
            policy::Action::Start => {
                self.start(false).await?;
            }
            policy::Action::Stop { message, .. } => {
                if let Some(message) = message {
                    self.announce(message).await?;
                }
                self.room.shutdown_priority().await?;
                self.record_stop()?;
            }
            policy::Action::Announce { message } => {
                self.announce(message).await?;
            }
            policy::Action::UpdateMods { .. } => {
                let request = Envelope::new(
                    Target::Room,
                    Request::UpdateMods {
                        restart: false,
                        notice: None,
                    },
                )?;
                let result = self.room.invoke_until_complete(request).await;
                self.room.with_lock(|lock| {
                    let mut state = PolicyState::load(lock)?;
                    state.finish_mod_update(
                        Utc::now(),
                        result.as_ref().map(|_| ()).map_err(ToString::to_string),
                    )?;
                    state.save(lock)
                })?;
                if let Err(error) = result {
                    *self.error.lock().unwrap() = Some(error.to_string());
                }
            }
            policy::Action::Regenerate { expected_sessions } => {
                let status = self.room.status();
                let sessions: BTreeMap<_, _> = status
                    .shards
                    .iter()
                    .filter_map(|shard| {
                        shard
                            .identity
                            .as_ref()
                            .map(|identity| (shard.name.clone(), identity.session_id.clone()))
                    })
                    .collect();
                if sessions != expected_sessions {
                    return Err(Error::new(
                        ErrorCode::StaleReference,
                        "world changed before idle regeneration",
                    ));
                }
                self.room
                    .invoke_until_complete(Envelope::new(
                        Target::Room,
                        Request::Regenerate {
                            expected_session_id: sessions.get(&status.master).cloned(),
                            require_empty: Some(true),
                        },
                    )?)
                    .await?;
            }
        }
        Ok(())
    }

    async fn announce(&self, message: String) -> model::Result<Value> {
        self.room
            .invoke_until_complete(Envelope::new(
                Target::Room,
                Request::Announce {
                    message,
                    count: 1,
                    interval: 30.0,
                },
            )?)
            .await
    }

    async fn manual_mod_update(
        &self,
        restart: bool,
        notice: Option<model::Countdown>,
    ) -> model::Result<Value> {
        let current = self.policy_state()?.mods;
        if !matches!(current.phase, ModPhase::Idle | ModPhase::Failed) {
            return Err(Error::new(
                ErrorCode::Busy,
                "Mod maintenance is already pending",
            ));
        }
        self.room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            state.request_mod_update()?;
            state.save(lock)
        })?;
        if let Some(notice) = notice
            && self.room.status().phase == Phase::Running
        {
            policy::countdown(&notice, |message| async move {
                if self.closing.load(Ordering::Acquire)
                    || !self.policy()?.schedule_at(Utc::now())?.open
                {
                    return Ok(false);
                }
                self.announce(message).await?;
                Ok(true)
            })
            .await
            .map_err(internal)?;
        }
        self.room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            if self.room.status().phase == Phase::Running {
                state.mods.phase = ModPhase::Stopping;
            }
            state.save(lock)
        })?;
        // This retained worker drives the same persisted policy while its client waits.
        loop {
            if self.closing.load(Ordering::Acquire) || self.room.requires_container_restart() {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "Agent is closing during maintenance",
                ));
            }
            let inputs = self.inputs(&self.room.status(), false).await?;
            let plan = self.room.with_lock(|lock| {
                let mut state = PolicyState::load(lock)?;
                let plan = Policy::load(lock)?.plan(&mut state, Utc::now(), &inputs)?;
                state.save(lock)?;
                Ok(plan)
            })?;
            if let Some(action) = plan.action {
                self.action(action).await?;
            }
            let state = self.policy_state()?;
            match state.mods.phase {
                ModPhase::Idle => {
                    if restart
                        && self
                            .policy()?
                            .schedule_at(Utc::now())
                            .map_err(internal)?
                            .open
                    {
                        self.start(false).await?;
                    }
                    return Ok(
                        json!({"updated":true,"restarted":self.room.status().phase == Phase::Running}),
                    );
                }
                ModPhase::Failed => {
                    return Err(Error::new(
                        ErrorCode::NotReady,
                        "Mod maintenance exhausted three attempts",
                    )
                    .with_details(json!({"maintenance":state.mods})));
                }
                _ => tokio::time::sleep(policy::CHECK_INTERVAL).await,
            }
        }
    }

    async fn persist_failure(&self) -> model::Result<()> {
        let states: Vec<_> = self.room.driver_states().into_values().collect();
        let failure = preloader::classify(&states);
        self.save_run(RunState {
            active: false,
            failure: Some(failure.into()),
        })
    }
}

fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64
}
fn now_ms() -> u64 {
    now_ns() / 1_000_000
}
fn internal(error: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Internal, error.to_string())
}

/// Return 75 only after a failed container's game children have been reaped.
/// The outer LocalSet is shared with the Cap'n Proto server.
pub async fn run(options: Options) -> Result<u8> {
    let mut termination =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    let events = EventHub::default();
    let socket = options
        .socket
        .unwrap_or_else(|| options.cluster.join(".dst-agent.sock"));
    let room = Room::open(
        &options.cluster,
        &options.executable,
        options.driver,
        events.clone(),
    )?;
    let telemetry = crate::telemetry::TelemetryConfig::from_env()?;
    room.set_telemetry_resource(telemetry.resource.clone());
    room.set_observability(Some(Arc::new(crate::observability::Observability::new(
        &telemetry,
    )?)));
    if let Some(config) = telemetry.logs {
        room.set_log_exporter(Some(Arc::new(crate::telemetry::LogExporter::new(
            config,
            &telemetry.resource,
        )?)));
    }
    if let Some(ports) = std::env::var_os(crate::deployment::PUBLISHED_PORTS_ENV) {
        room.set_external_ports(serde_json::from_str(
            ports.to_str().context("published ports are not UTF-8")?,
        )?)?;
    }
    let controller = Arc::new(Controller {
        room: room.clone(),
        started: Instant::now(),
        initialized: AtomicBool::new(false),
        closing: AtomicBool::new(false),
        mutation: Arc::new(Semaphore::new(1)),
        interrupted: watch::channel(0).0,
        closing_game: AtomicBool::new(false),
        operations: Mutex::new(OperationHistory::default()),
        stable: Mutex::new(None),
        unhealthy: Mutex::new(None),
        error: Mutex::new(None),
    });
    let (dispatch, mut requests) = mpsc::channel::<rpc::Incoming>(64);
    let (shutdown, receive_shutdown) = oneshot::channel();
    let server_socket = socket.clone();
    let mut server = tokio::task::spawn_local(async move {
        rpc::serve(&server_socket, dispatch, events, receive_shutdown).await
    });
    let boot = controller.clone();
    let mut boot = tokio::spawn(async move { boot.initialize().await });
    let mut workers = JoinSet::new();
    let mut room_changes = room.watch();
    let mut tick = tokio::time::interval(policy::CHECK_INTERVAL);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut policy_task = None;
    let mut exit = 0;
    let mut terminal_error = None;
    loop {
        tokio::select! {
            result = &mut server => {
                terminal_error = result.map_err(anyhow::Error::from).and_then(|result| result.map_err(Into::into)).err();
                exit = 75;
                break;
            }
            _ = interrupt.recv() => break,
            _ = termination.recv() => break,
            result = &mut boot, if !controller.initialized.load(Ordering::Acquire) => {
                match result {
                    Ok(Err(error)) => {
                        *controller.error.lock().unwrap() = Some(error.to_string());
                        controller.initialized.store(true, Ordering::Release);
                    }
                    Ok(Ok(())) => {},
                    Err(error) => { terminal_error = Some(error.into()); exit = 75; break; }
                }
                tick.reset_immediately();
            }
            _ = room_changes.changed() => {
                if room.requires_container_restart() { exit = 75; break; }
            }
            _ = workers.join_next(), if !workers.is_empty() => {},
            Some(incoming) = requests.recv() => {
                let controller = controller.clone();
                workers.spawn(async move { let result = controller.invoke(incoming.request).await; let _ = incoming.reply.send(result); });
            }
            _ = tick.tick() => {
                if room.requires_container_restart() {
                    exit = 75;
                    break;
                }
                if policy_task.as_ref().is_none_or(tokio::task::JoinHandle::is_finished) {
                    let controller = controller.clone();
                    policy_task = Some(tokio::spawn(async move {
                        if let Err(error) = controller.tick().await { *controller.error.lock().unwrap() = Some(error.to_string()); }
                    }));
                }
            }
        }
    }
    controller.closing.store(true, Ordering::Release);
    if !boot.is_finished() {
        boot.abort();
        let _ = boot.await;
    }
    if let Some(task) = policy_task {
        task.abort();
        let _ = task.await;
    }
    // Accepted file writes and native updates finish before stopping their room.
    let permit = tokio::time::timeout(
        Duration::from_secs(180),
        controller.mutation.clone().acquire_owned(),
    )
    .await;
    if (permit.is_err() || room.shutdown_priority().await.is_err())
        && let Err(error) = room.kill().await
    {
        terminal_error.get_or_insert_with(|| error.into());
    }
    let recorded = if exit == 75 {
        controller.persist_failure().await
    } else {
        controller.record_stop()
    };
    if let Err(error) = recorded {
        terminal_error.get_or_insert_with(|| error.into());
    }
    if let Err(error) = room.close_telemetry().await {
        terminal_error.get_or_insert_with(|| error.into());
    }
    let _ = shutdown.send(());
    if !server.is_finished() {
        let _ = server.await;
    }
    workers.abort_all();
    while workers.join_next().await.is_some() {}
    if let Some(error) = terminal_error {
        return Err(error);
    }
    Ok(exit)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Timelike;
    use std::{fs, os::unix::fs::PermissionsExt};

    async fn running_controller() -> (tempfile::TempDir, PathBuf, Arc<Controller>) {
        let directory = tempfile::tempdir().unwrap();
        let cluster = directory.path().join("cluster");
        fs::create_dir(&cluster).unwrap();
        fs::write(cluster.join("cluster.ini"), "[SHARD]\nshard_enabled=true\ncluster_key=closing-fixture\nmaster_port=19000\nmaster_ip=127.0.0.1\n").unwrap();
        for (name, id, port) in [("Master", 1, 19100), ("Caves", 2, 19200)] {
            fs::create_dir(cluster.join(name)).unwrap();
            fs::write(cluster.join(name).join("server.ini"), format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n", id == 1, port + 1)).unwrap();
        }
        let executable = directory.path().join("game.py");
        fs::write(&executable, include_str!("../tests/fixtures/game.py")).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let room = Room::open(
            &cluster,
            executable,
            DriverOptions::default(),
            EventHub::default(),
        )
        .unwrap();
        let controller = Arc::new(Controller {
            room: room.clone(),
            started: Instant::now(),
            initialized: AtomicBool::new(true),
            closing: AtomicBool::new(false),
            mutation: Arc::new(Semaphore::new(1)),
            interrupted: watch::channel(0).0,
            closing_game: AtomicBool::new(false),
            operations: Mutex::new(OperationHistory::default()),
            stable: Mutex::new(None),
            unhealthy: Mutex::new(None),
            error: Mutex::new(None),
        });
        controller
            .invoke(Envelope::new(Target::Room, Request::Start {}).unwrap())
            .await
            .unwrap();
        (directory, cluster, controller)
    }

    fn enable_idle_regeneration(controller: &Controller) {
        let states = controller.room.driver_states();
        controller
            .room
            .with_lock(|lock| {
                Policy {
                    mod_auto_update: false,
                    idle_regeneration: true,
                    ..Default::default()
                }
                .save(lock)?;
                let mut state = PolicyState::load(lock)?;
                state.activity = Some(policy::Activity {
                    sessions: states
                        .iter()
                        .map(|(name, state)| (name.clone(), state.session_id.clone().unwrap()))
                        .collect(),
                    observations: states
                        .iter()
                        .map(|(name, state)| {
                            (
                                name.clone(),
                                format!("{}:{}", state.nonce, state.generation.unwrap()),
                            )
                        })
                        .collect(),
                    observed_seconds: states.keys().map(|name| (name.clone(), 0.0)).collect(),
                    last_active_at: Utc::now() - chrono::TimeDelta::hours(7),
                    clean_shutdown: false,
                    interrupted: false,
                });
                state.save(lock)
            })
            .unwrap();
    }

    async fn wait_for(condition: impl Fn() -> bool) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !condition() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn changed_mod_downloads_schedule_maintenance_before_reopening() {
        let (_directory, cluster, controller) = running_controller().await;
        controller
            .invoke(Envelope::new(Target::Room, Request::Stop { notice: None }).unwrap())
            .await
            .unwrap();
        fs::write(cluster.join("cluster_token.txt"), "").unwrap();
        let mut configuration = controller
            .invoke(Envelope::new(Target::Room, Request::ReadConfiguration {}).unwrap())
            .await
            .unwrap()["cluster"]
            .clone();
        configuration["downloads"] = json!({"items":[123]});
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::Configure {
                        configuration,
                        replace_permissions: false,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            controller.policy_state().unwrap().mods.phase,
            ModPhase::Pending
        );
        let error = controller
            .invoke(Envelope::new(Target::Room, Request::Start {}).unwrap())
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Busy);
    }

    #[tokio::test]
    async fn policy_queries_allow_mutations_and_discard_the_earlier_idle_observation() {
        let (_directory, cluster, controller) = running_controller().await;
        enable_idle_regeneration(&controller);
        let hold = cluster.join("Master/hold-world");
        fs::write(&hold, "").unwrap();
        let tick = {
            let controller = controller.clone();
            tokio::spawn(async move { controller.tick().await })
        };
        wait_for(|| cluster.join("Master/world-waiting").exists()).await;
        // Presence has already reported an idle room while Master is still
        // answering World. A write to another shard must remain available.
        controller
            .invoke(
                Envelope::new(
                    Target::Shard("Caves".into()),
                    Request::ExecuteJson {
                        source: "return true".into(),
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        fs::remove_file(hold).unwrap();
        tick.await.unwrap().unwrap();
        assert!(!cluster.join("reload-request").exists());
        assert!(
            controller
                .policy_state()
                .unwrap()
                .activity
                .unwrap()
                .interrupted
        );

        // Current observations still trigger the intended idle policy.
        enable_idle_regeneration(&controller);
        let hold = cluster.join("Master/hold-regenerate");
        fs::write(&hold, "").unwrap();
        controller.tick().await.unwrap();
        wait_for(|| cluster.join("Master/regenerate-waiting").exists()).await;
        let current = controller.status().unwrap()["operations"]["current"].clone();
        assert_eq!(current["method"], "regenerate");
        fs::remove_file(hold).unwrap();
        wait_for(|| {
            controller.room.status().phase == Phase::Running
                && controller.room.status().shards.iter().all(|shard| {
                    shard
                        .identity
                        .as_ref()
                        .is_some_and(|identity| identity.session_id.ends_with("_NEW"))
                })
                && controller.mutation.available_permits() == 1
        })
        .await;
        let completed = controller.status().unwrap();
        assert!(completed["operations"]["current"].is_null());
        assert_eq!(completed["operations"]["last"]["id"], current["id"]);
        controller.room.shutdown().await.unwrap();
    }

    #[tokio::test]
    async fn policy_discards_observations_when_a_generation_changes_in_the_same_session() {
        let (_directory, cluster, controller) = running_controller().await;
        enable_idle_regeneration(&controller);
        let hold = cluster.join("Master/hold-world");
        fs::write(&hold, "").unwrap();
        let tick = {
            let controller = controller.clone();
            tokio::spawn(async move { controller.tick().await })
        };
        wait_for(|| cluster.join("Master/world-waiting").exists()).await;
        let reload = json!({"id":1,"regenerate":false,"snapshot_id":4});
        fs::write(cluster.join("reload-request"), reload.to_string()).unwrap();
        wait_for(|| controller.room.driver_states()["Caves"].generation == Some(2)).await;
        let runtime = controller
            .invoke(Envelope::new(Target::Shard("Caves".into()), Request::Runtime {}).unwrap())
            .await
            .unwrap();
        assert_eq!(runtime["session_id"], "S_Caves");
        wait_for(|| {
            controller.room.status().shards.iter().any(|shard| {
                shard.name == "Caves"
                    && shard.identity.as_ref().is_some_and(|identity| {
                        identity.generation == 2 && identity.session_id == "S_Caves"
                    })
            })
        })
        .await;
        fs::remove_file(hold).unwrap();
        tick.await.unwrap().unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(cluster.join("reload-request")).unwrap())
                .unwrap(),
            reload
        );
        assert!(
            controller
                .policy_state()
                .unwrap()
                .activity
                .unwrap()
                .interrupted
        );
        controller.room.shutdown().await.unwrap();
    }

    #[tokio::test]
    async fn scheduled_closing_rejects_a_restart_before_its_stop_worker_acquires_ownership() {
        let (_directory, _cluster, controller) = running_controller().await;
        let room = &controller.room;
        let identities: Vec<_> = room
            .driver_states()
            .into_values()
            .map(|state| (state.pid, state.nonce))
            .collect();
        let now = Utc::now();
        let start = (now.hour() * 60 + now.minute() + 180) % 1440;
        let end = (start + 60) % 1440;
        let policy: Policy = serde_json::from_value(json!({
            "timezone":"UTC", "mod_auto_update":false,
            "schedule":[{"start":format!("{:02}:{:02}", start / 60, start % 60),"end":format!("{:02}:{:02}", end / 60, end % 60)}],
        })).unwrap();
        room.with_lock(|lock| policy.save(lock)).unwrap();

        // The prior operation is still finishing when the policy chooses Stop.
        let prior_operation = controller.mutation.clone().acquire_owned().await.unwrap();
        controller.tick().await.unwrap();
        assert!(controller.closing_game.load(Ordering::Acquire));
        drop(prior_operation);
        // With no yield here, a new request reaches the gate before the spawned
        // Stop worker can acquire the just-released permit on this runtime.
        let rejected = controller
            .invoke(Envelope::new(Target::Room, Request::Restart { notice: None }).unwrap())
            .await
            .unwrap_err();
        assert_eq!(rejected.code, ErrorCode::Busy);
        tokio::time::timeout(Duration::from_secs(5), async {
            while room.status().phase != Phase::Stopped
                || controller.closing_game.load(Ordering::Acquire)
            {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let stopped: Vec<_> = room.driver_states().into_values().collect();
        assert_eq!(
            stopped
                .iter()
                .map(|state| (state.pid, state.nonce.clone()))
                .collect::<Vec<_>>(),
            identities
        );
        assert!(
            stopped
                .iter()
                .all(|state| !state.running && state.output_drained)
        );
        assert!(!controller.run_state().unwrap().active);
        // A canceled policy wait can leave Updating persisted after the room's
        // retained worker has reaped its updater; closing must finish that attempt.
        room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            state.mods.phase = ModPhase::Updating;
            state.mods.attempts = 1;
            state.save(lock)
        })
        .unwrap();
        controller
            .action(policy::Action::Stop {
                reason: policy::StopReason::Schedule,
                message: None,
            })
            .await
            .unwrap();
        let maintenance = controller.policy_state().unwrap().mods;
        assert!(matches!(maintenance.phase, ModPhase::Retry { .. }));
        assert_eq!(maintenance.attempts, 1);
        room.close_telemetry().await.unwrap();
    }
}
