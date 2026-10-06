//! The room Agent owns policies and recovery across individual client lifetimes.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use tokio::{
    sync::{Notify, Semaphore, mpsc, oneshot, watch},
    task::JoinSet,
    time::Instant,
};

use crate::{
    driver::DriverOptions,
    model::{self, Envelope, Error, ErrorCode, OperationHistory, Phase, Request, Target},
    operation::{Layer, OperationContext, Source},
    policy::{self, ModPhase, Policy, PolicyState},
    preloader,
    recovery::{self, Decision, Failure, RecoveryStep, RunState, TargetPhase},
    room::{Room, ShutdownDeadlines, StopReservation, mod_download_outcome},
    rpc::{self, EventHub},
    status::{AgentStatus, ControlStatus, GameStatus},
};

pub struct Options {
    pub cluster: PathBuf,
    pub executable: PathBuf,
    pub socket: Option<PathBuf>,
    pub driver: DriverOptions,
}

struct Controller {
    room: Arc<Room>,
    last_tick: Mutex<Option<(DateTime<Utc>, Instant)>>,
    closing: AtomicBool,
    exit: watch::Sender<Option<u8>>,
    changed: Notify,
    mutation: Arc<Semaphore>,
    interrupted: watch::Sender<u64>,
    closing_game: AtomicBool,
    operations: Mutex<OperationHistory>,
    stable: Mutex<Option<(Vec<model::SessionIdentity>, Instant)>>,
    unhealthy: Mutex<Option<Instant>>,
    error: Mutex<Option<String>>,
    mod_check: Mutex<crate::mods::monitor::Check>,
    mod_check_requested: Notify,
    pending_start: Mutex<Option<OperationContext>>,
    observed_schedule: Mutex<Option<policy::ScheduleState>>,
}

impl Controller {
    fn update_policy<T>(
        &self,
        reason: &str,
        context: Option<&OperationContext>,
        update: impl FnOnce(&mut crate::files::RoomLock, &mut PolicyState) -> anyhow::Result<T>,
    ) -> model::Result<T> {
        let (result, before, after) = self.room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            let before = state.mods.clone();
            let result = update(lock, &mut state)?;
            state.save(lock)?;
            Ok((result, before, state.mods))
        })?;
        self.record_mod_transition(&before, &after, reason, context);
        Ok(result)
    }

    fn record_mod_transition(
        &self,
        before: &policy::ModMaintenance,
        after: &policy::ModMaintenance,
        reason: &str,
        context: Option<&OperationContext>,
    ) {
        let summary = |state: &policy::ModMaintenance| json!({"phase":state.phase,"attempts":state.attempts,"origin":state.origin,"manual":state.manual,"resume_game":state.resume_game,"unavailable":state.phase == ModPhase::Verifying && state.error.is_some()});
        if summary(before) == summary(after) && before.targets == after.targets {
            return;
        }
        let reason = if after.phase == ModPhase::Failed {
            "exhausted"
        } else if matches!(after.phase, ModPhase::Retry { .. }) {
            "retry"
        } else if after.phase == ModPhase::Idle {
            match reason {
                "verification" => "verified",
                "download_completed" => "completed",
                "download_skipped" => "skipped",
                _ => "cancelled",
            }
        } else {
            reason
        };
        self.room.record_management("dst.mod.maintenance", json!({
            "reason":reason,
            "previous_phase":before.phase,"phase":after.phase,"attempts":after.attempts,
            "origin":after.origin.as_ref().or(before.origin.as_ref()),
            "previous_origin":(before.origin != after.origin).then_some(&before.origin),
            "manual":after.manual,"resume_game":after.resume_game,
            "verification_unavailable":after.phase == ModPhase::Verifying && after.error.is_some(),
        }), if after.phase == ModPhase::Failed {"ERROR"} else {"INFO"}, context);
        for id in before
            .targets
            .keys()
            .chain(after.targets.keys())
            .collect::<BTreeSet<_>>()
        {
            let previous = before.targets.get(id);
            let current = after.targets.get(id);
            if previous == current {
                continue;
            }
            for shard in previous
                .into_iter()
                .flat_map(|target| target.shards.iter())
                .chain(current.into_iter().flat_map(|target| target.shards.iter()))
                .collect::<BTreeSet<_>>()
            {
                self.room.record_management("dst.mod.maintenance", json!({
                    "reason":"target_changed","origin":after.origin.as_ref().or(before.origin.as_ref()),
                    "mod_id":id,"shard":shard,
                    "previous_version":previous.filter(|target| target.shards.contains(shard)).map(|target| &target.version),
                    "version":current.filter(|target| target.shards.contains(shard)).map(|target| &target.version),
                }), "INFO", context);
            }
        }
    }

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
        self.room.with_lock(|lock| recovery::load_run(lock))
    }
    fn required(&self) -> BTreeSet<String> {
        self.room
            .status()
            .shards
            .into_iter()
            .map(|shard| shard.name)
            .collect()
    }

    fn request_exit(&self, code: u8) {
        self.closing.store(true, Ordering::Release);
        self.exit.send_if_modified(|current| {
            if current.is_none() {
                *current = Some(code);
                true
            } else {
                false
            }
        });
    }

    fn initialize(&self) -> model::Result<()> {
        let policy = self.policy()?;
        self.update_policy("agent_started", None, |lock, state| {
            state.resume_after_restart(Utc::now())?;
            if policy.mod_auto_update && recovery::load(lock)?.closed.is_none() {
                Self::inspect_mod_setup(lock, &mut state.mods);
            }
            Ok(())
        })
    }

    fn inspect_mod_setup(lock: &mut crate::files::RoomLock, mods: &mut policy::ModMaintenance) {
        let pending_setup = mods.phase == ModPhase::Pending
            && !mods.manual
            && mods.attempts == 0
            && mods.targets.is_empty()
            && mods.error.is_some();
        if mods.phase != ModPhase::Idle && !pending_setup {
            return;
        }
        match crate::mods::prepare_shared(lock) {
            Ok(setup) if !setup.has_code => *mods = Default::default(),
            result => {
                if !pending_setup {
                    mods.enter_pending();
                }
                mods.error = result.err().map(|error| error.to_string());
            }
        }
    }

    async fn recover(&self, context: Option<&OperationContext>) -> model::Result<()> {
        self.check_start_allowed()?;
        let (state, previous) = self.room.with_lock(|lock| recovery::load_with_run(lock))?;
        let pending = state.retry.is_some()
            || state.target.as_ref().is_some_and(|target| {
                matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
            });
        if previous.active || previous.failure.is_some() || pending {
            if state.closed.is_some() {
                self.room
                    .update_recovery("recovery_decided", context, |lock| {
                        recovery::on_failure(
                            lock,
                            previous.failure.unwrap_or(Failure::Unknown),
                            &self.required(),
                            &BTreeMap::new(),
                            now_ms(),
                        )
                    })?;
                return Ok(());
            }
            let previous = self
                .room
                .update_recovery("recovered_state", context, |lock| {
                    recovery::prepare_recovery(lock, now_ms())
                })?;
            if previous.failure.as_ref().is_some_and(|failure| {
                !matches!(failure, Failure::Retryable | Failure::LoadFailure { .. })
            }) {
                self.room
                    .update_recovery("recovery_decided", context, |lock| {
                        recovery::on_failure(
                            lock,
                            previous.failure.unwrap_or(Failure::Unknown),
                            &self.required(),
                            &BTreeMap::new(),
                            now_ms(),
                        )
                    })?;
                return Ok(());
            }
            let catalogs =
                match preloader::catalog(self.room.clone(), Duration::from_secs(120)).await {
                    Ok(catalogs) => catalogs,
                    Err(error) if error.is_shutdown() => {
                        return Err(Error::shutdown(ErrorCode::Unknown));
                    }
                    Err(error) => {
                        self.room
                            .update_recovery("recovery_decided", context, |lock| {
                                recovery::recovery_failed(lock, RecoveryStep::Catalog, &error)
                            })?;
                        return Err(error);
                    }
                };
            self.check_start_allowed()?;
            let unstarted = state.retry.as_ref().is_some_and(|retry| !retry.started)
                || state
                    .target
                    .as_ref()
                    .is_some_and(|target| target.phase == TargetPhase::Preparing);
            if pending && (unstarted || previous.failure.is_none()) {
                self.room
                    .update_recovery("recovery_decided", context, |lock| {
                        recovery::resume_pending(lock, &self.required(), &catalogs, now_ms())
                    })?;
            } else {
                let failure = previous.failure.unwrap_or(Failure::Retryable);
                self.room
                    .update_recovery("recovery_decided", context, |lock| {
                        recovery::on_failure(lock, failure, &self.required(), &catalogs, now_ms())
                    })?;
            }
        }
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
        let control = self
            .room
            .with_lock(|lock| ControlStatus::from_document(lock.read_state()?))?;
        if status.phase == Phase::Stopped
            && (control.recovery.closed.is_some() || control.maintenance.phase == ModPhase::Failed)
        {
            status.phase = Phase::Failed;
        }
        serde_json::to_value(GameStatus {
            room: status,
            agent: AgentStatus {
                control,
                ready: !self.closing.load(Ordering::Acquire),
                requires_container_restart: self.room.requires_container_restart(),
                error: self.error.lock().unwrap().clone(),
                mod_check: self.mod_check.lock().unwrap().clone(),
                telemetry: self.room.telemetry_stats(),
            },
        })
        .map_err(internal)
    }

    async fn invoke(self: &Arc<Self>, envelope: Envelope) -> model::Result<Value> {
        envelope.validate()?;
        let mut interrupted = self.interrupted.subscribe();
        let revision = *interrupted.borrow();
        if matches!(envelope.request, Request::Status {}) {
            return if envelope.target == Target::Room {
                self.status()
            } else {
                self.room.invoke_until_complete(envelope).await
            };
        }
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::new(ErrorCode::NotReady, "Agent is closing the room"));
        }
        if !envelope.request.mutating() {
            return self.room.invoke_until_complete(envelope).await;
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
        let configuration_edit = match &envelope.request {
            Request::SetPolicy { .. } => true,
            Request::Configure { .. } | Request::EditConfiguration { .. }
                if maintenance.phase != ModPhase::Updating
                    && self.room.require_stopped().is_ok() =>
            {
                true
            }
            Request::EditConfiguration { changes, unset } => changes
                .iter()
                .map(|(pointer, _)| pointer)
                .chain(unset)
                .all(|pointer| pointer == "/policy" || pointer.starts_with("/policy/")),
            _ => false,
        };
        match &envelope.request {
            Request::UpdateMods { .. } => maintenance.require_manual_update()?,
            Request::Restart { .. } => maintenance.require_game_start()?,
            Request::Start {} | Request::Kill {} => {}
            Request::Stop { .. } if maintenance.phase != ModPhase::Updating => {}
            _ if !configuration_edit
                && !matches!(
                    maintenance.phase,
                    ModPhase::Idle | ModPhase::Verifying | ModPhase::Failed
                ) =>
            {
                return Err(Error::new(
                    ErrorCode::Busy,
                    "room Mod maintenance is pending",
                ));
            }
            _ => {}
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
        let mut context = OperationContext::new(Source::Rpc, Layer::Request, None)
            .with_request_players(&envelope.request);
        let operation = context.status(envelope.request.method(), envelope.target.clone());
        let (reservation, observation) = {
            let mut history = self.operations.lock().unwrap();
            if self.closing.load(Ordering::Acquire) || !killing && *interrupted.borrow() != revision
            {
                return Err(Error::new(
                    ErrorCode::Unknown,
                    "Agent closed before operation admission",
                ));
            }
            let reservation = match &envelope.request {
                Request::Stop { notice }
                | Request::Restart { notice }
                | Request::UpdateMods { notice, .. } => {
                    Some(self.room.reserve_stop(&context, notice)?)
                }
                _ => None,
            };
            if matches!(
                envelope.request,
                Request::Stop { .. } | Request::Restart { .. } | Request::Kill {}
            ) {
                self.pending_start.lock().unwrap().take();
            }
            let observation = self
                .room
                .begin_operation(&operation, &mut context, "accepted");
            history.current = Some(operation.clone());
            (reservation, observation)
        };
        let controller = self.clone();
        let wake_policy = configuration_edit
            || reservation.is_some()
            || matches!(envelope.request, Request::Start {});
        let check_mods = matches!(
            envelope.request,
            Request::Restart { .. } | Request::SetPolicy { .. }
        );
        let (reply, response) = oneshot::channel();
        tokio::spawn(async move {
            let _permit = permit;
            let result = tokio::select! {
                biased;
                _ = interrupted.changed(), if !killing => Err(Error::new(ErrorCode::Unknown, "operation wait was interrupted by room shutdown")),
                result = controller.execute(envelope, &context, reservation) => result,
            };
            controller.room.finish_operation(
                &operation,
                &context,
                observation,
                result.as_ref().map(|(_, outcome)| *outcome),
            );
            let result = result.map(|(value, _)| value);
            let mut completed = operation;
            completed.complete(&result);
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
            if wake_policy {
                if result
                    .as_ref()
                    .is_ok_and(|value| check_mods || value["policy_changed"] == true)
                {
                    controller.mod_check_requested.notify_one();
                }
                controller.changed.notify_one();
            }
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

    async fn execute(
        self: &Arc<Self>,
        mut envelope: Envelope,
        context: &OperationContext,
        reservation: Option<StopReservation>,
    ) -> model::Result<(Value, &'static str)> {
        // The retained worker waits for the room; only invoke applies the caller's deadline.
        envelope.timeout = None;
        let mut outcome = "completed";
        let value = match &envelope.request {
            Request::Start {} => {
                if self.closing.load(Ordering::Acquire) {
                    return Err(Error::new(
                        ErrorCode::Unknown,
                        "Agent closed before Start authorization",
                    ));
                }
                self.room.update_recovery_if_changed(
                    "reset_explicit_start",
                    Some(context),
                    |lock| {
                        let always_open = Policy::load(lock)?
                            .schedule_at(Utc::now())?
                            .next_change
                            .is_none();
                        Ok(((), recovery::explicit_start(lock, always_open)?))
                    },
                )?;
                if self.room.status().phase == Phase::Running {
                    outcome = "already_running";
                } else if self
                    .policy()?
                    .schedule_at(Utc::now())
                    .map_err(internal)?
                    .open
                    && self.policy_state()?.mods.require_game_start().is_ok()
                {
                    *self.pending_start.lock().unwrap() = Some(context.clone());
                    outcome = "authorized";
                } else {
                    outcome = "deferred";
                }
                *self.error.lock().unwrap() = None;
                self.status()
            }
            Request::Restart { notice } => {
                let result = self
                    .stop(notice.clone(), Some(context), reservation)
                    .await?;
                let status = self.start(Some(context)).await?;
                Ok(json!({"stopped": result, "restart": status["phase"] == "running"}))
            }
            Request::Kill {} => {
                let result = self.room.invoke_in_context(envelope, context).await;
                if !self.room.requires_container_restart() {
                    self.room
                        .force_fault_in_context(
                            Error::new(
                                ErrorCode::Unknown,
                                "room was forcibly stopped during preparation",
                            ),
                            &BTreeSet::new(),
                            Some(context),
                        )
                        .await?;
                }
                result
            }
            Request::Stop { notice } => {
                let result = self.stop(notice.clone(), Some(context), reservation).await;
                if result.is_ok() {
                    self.request_exit(0);
                }
                result
            }
            Request::UpdateMods { restart, notice } => {
                outcome = "queued";
                self.manual_mod_update(*restart, notice.clone(), Some(context), reservation)
                    .await
            }
            Request::Configure { .. } | Request::EditConfiguration { .. } => {
                let before = self.policy_state()?.mods;
                let result = self.room.invoke_in_context(envelope, context).await;
                self.pending_start.lock().unwrap().take();
                *self.mod_check.lock().unwrap() = Default::default();
                let observed = self.policy_state().map(|state| {
                    self.record_mod_transition(
                        &before,
                        &state.mods,
                        "configuration_changed",
                        Some(context),
                    );
                });
                let result = result?;
                observed?;
                if result["changed"].as_array().is_some_and(|files| {
                    files
                        .iter()
                        .any(|file| file == "mods/dedicated_server_mods_setup.lua")
                }) {
                    self.update_policy("configuration_changed", Some(context), |lock, state| {
                        if Policy::load(lock)?.mod_auto_update {
                            Self::inspect_mod_setup(lock, &mut state.mods);
                        }
                        Ok(())
                    })?;
                }
                Ok(result)
            }
            Request::SetPolicy { .. } => {
                let result = self.room.invoke_in_context(envelope, context).await?;
                self.pending_start.lock().unwrap().take();
                *self.mod_check.lock().unwrap() = Default::default();
                Ok(result)
            }
            _ => self.room.invoke_in_context(envelope, context).await,
        }?;
        Ok((value, outcome))
    }

    fn check_start_allowed(&self) -> model::Result<()> {
        if self.closing.load(Ordering::Acquire) || self.closing_game.load(Ordering::Acquire) {
            return Err(Error::shutdown(ErrorCode::Unknown));
        }
        self.policy_state()?.mods.require_game_start()
    }

    async fn start(&self, context: Option<&OperationContext>) -> model::Result<Value> {
        self.check_start_allowed()?;
        if self.room.status().phase == Phase::Running {
            return self.status();
        }
        if !self
            .policy()?
            .schedule_at(Utc::now())
            .map_err(internal)?
            .open
        {
            return self.status();
        }
        self.recover(context).await?;
        self.check_start_allowed()?;
        let state = self.recovery()?;
        if let Some(reason) = state.closed {
            return Err(
                Error::new(ErrorCode::NotReady, "room recovery is exhausted or unsafe")
                    .with_details(json!({"reason":reason})),
            );
        }
        let retry = state.retry;
        if retry.is_some() {
            // A fresh monotonic wait is conservative across container and wall-clock changes.
            tokio::time::sleep(recovery::RETRY_DELAY).await;
            self.check_start_allowed()?;
            if !self
                .policy()?
                .schedule_at(Utc::now())
                .map_err(internal)?
                .open
            {
                return Err(Error::new(
                    ErrorCode::NotReady,
                    "scheduled closing deferred the pending retry",
                ));
            }
            let catalogs =
                match preloader::catalog(self.room.clone(), Duration::from_secs(120)).await {
                    Ok(catalogs) => catalogs,
                    Err(error) if error.is_shutdown() => {
                        return Err(Error::shutdown(ErrorCode::Unknown));
                    }
                    Err(error) => {
                        self.room
                            .update_recovery("recovery_decided", context, |lock| {
                                recovery::recovery_failed(lock, RecoveryStep::RetryCatalog, &error)
                            })?;
                        return Err(error);
                    }
                };
            self.check_start_allowed()?;
            let decision = self
                .room
                .update_recovery("recovery_decided", context, |lock| {
                    recovery::resume_pending(lock, &self.required(), &catalogs, now_ms())
                })?;
            if !matches!(decision, Some(Decision::RetryCurrent { .. })) {
                return Err(
                    Error::new(ErrorCode::NotReady, "saved room changed before the retry")
                        .with_details(json!({"decision":decision})),
                );
            }
        } else if let Some(target) = state.target
            && matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
            && let Err(error) =
                preloader::apply(self.room.clone(), target, Duration::from_secs(120)).await
        {
            if error.is_shutdown() {
                return Err(Error::shutdown(ErrorCode::Unknown));
            }
            self.room
                .update_recovery("recovery_decided", context, |lock| {
                    recovery::recovery_failed(lock, RecoveryStep::PrepareTarget, &error)
                })?;
            return Err(error);
        }
        self.check_start_allowed()?;
        if !self
            .policy()?
            .schedule_at(Utc::now())
            .map_err(internal)?
            .open
        {
            return Err(Error::new(
                ErrorCode::NotReady,
                "Agent closed before process launch",
            ));
        }
        *self.mod_check.lock().unwrap() = Default::default();
        let fallback = OperationContext::new(Source::Recovery, Layer::Policy, None);
        let result = self
            .room
            .invoke_in_context(
                Envelope::new(Target::Room, Request::Start {})?,
                context.unwrap_or(&fallback),
            )
            .await;
        match &result {
            Ok(_) => {
                self.observe_ready(&self.room.status());
                *self.error.lock().unwrap() = None;
            }
            Err(error)
                if !self.room.requires_container_restart()
                    && !self.closing.load(Ordering::Acquire) =>
            {
                let _ = self
                    .room
                    .force_fault_in_context(error.clone(), &BTreeSet::new(), context)
                    .await;
            }
            Err(_) => {}
        }
        result
    }

    async fn stop(
        &self,
        notice: Option<model::Countdown>,
        context: Option<&OperationContext>,
        reservation: Option<StopReservation>,
    ) -> model::Result<Value> {
        let fallback = OperationContext::new(Source::AgentShutdown, Layer::Policy, None);
        let context = context.unwrap_or(&fallback);
        let result = if let Some(reservation) = reservation {
            reservation.stop(notice, context).await?
        } else {
            self.room
                .invoke_in_context(
                    Envelope::new(Target::Room, Request::Stop { notice })?,
                    context,
                )
                .await?
        };
        self.record_stop()?;
        Ok(result)
    }

    fn record_stop(&self) -> model::Result<()> {
        *self.stable.lock().unwrap() = None;
        if self.room.requires_container_restart() {
            return Ok(());
        }
        self.update_policy("shutdown", None, |_, state| {
            state.record_clean_shutdown();
            if state.mods.phase == ModPhase::Updating {
                state.finish_mod_update(
                    Utc::now(),
                    Err("Mod update interrupted by shutdown".into()),
                )?;
            }
            Ok(())
        })
    }

    fn observe_ready(&self, status: &model::RoomStatus) -> Duration {
        let identities: Option<Vec<_>> = status
            .shards
            .iter()
            .map(|shard| shard.identity.clone())
            .collect();
        let mut stable = self.stable.lock().unwrap();
        if let Some((previous, since)) = stable.as_ref()
            && Some(previous) == identities.as_ref()
        {
            return since.elapsed();
        }
        *stable = identities.map(|worlds| (worlds, Instant::now()));
        Duration::ZERO
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
            inputs.mod_targets = self.mod_check.lock().unwrap().targets.clone();
            for shard in &status.shards {
                let request = Envelope {
                    target: Target::Shard(shard.name.clone()),
                    request: Request::Presence {},
                    timeout: Some(5.0),
                };
                if let Ok(value) = self.room.invoke_until_complete(request).await {
                    if shard.is_master {
                        inputs.master_day = value["day"].as_u64().filter(|day| *day > 0);
                    }
                    if let Ok(presence) = serde_json::from_value(value) {
                        inputs.presence.insert(shard.name.clone(), presence);
                    }
                }
                self.room
                    .observe_presence(shard, inputs.presence.get(&shard.name));
            }
        }
        Ok(inputs)
    }

    fn record_mod_check(
        &self,
        before: &crate::mods::monitor::Check,
        after: &crate::mods::monitor::Check,
    ) {
        if before.error.is_some() != after.error.is_some() {
            self.room.record_management(
                "dst.mod.maintenance",
                json!({"reason":"check_availability","available":after.error.is_none()}),
                if after.error.is_some() {
                    "WARN"
                } else {
                    "INFO"
                },
                None,
            );
        }
        let ids: BTreeSet<_> = before
            .versions
            .keys()
            .chain(after.versions.keys())
            .chain(before.targets.keys())
            .chain(after.targets.keys())
            .chain(before.unavailable.keys())
            .chain(after.unavailable.keys())
            .collect();
        for id in ids {
            let shards: BTreeSet<_> = before
                .versions
                .get(id)
                .into_iter()
                .chain(after.versions.get(id))
                .flat_map(|versions| versions.loaded.keys())
                .map(|shard| Some(shard.as_str()))
                .chain(std::iter::once(None))
                .collect();
            for shard in shards {
                let summary = |report: &crate::mods::monitor::Check| {
                    let versions = report.versions.get(id);
                    json!({
                        "declared":versions.and_then(|versions| versions.declared.as_ref()).filter(|value| crate::mods::valid_version(value)),
                        "loaded":shard.and_then(|shard| versions.and_then(|versions| versions.loaded.get(shard))).filter(|value| crate::mods::valid_version(value)),
                        "target":report.targets.get(id).map(|target| &target.version),
                        "available":!report.unavailable.contains_key(id) && report.error.is_none(),
                    })
                };
                let current = summary(after);
                if summary(before) != current {
                    self.room.record_management("dst.mod.maintenance", json!({"reason":"check_changed","mod_id":id,"shard":shard,"comparison":current}), "INFO", None);
                }
            }
        }
    }

    async fn check_mods(
        &self,
        client: &std::result::Result<crate::steam::Client, crate::steam::Error>,
    ) {
        let before = self.room.status();
        let history = self.operations.lock().unwrap().clone();
        let Ok(policy) = self.policy() else {
            return;
        };
        let Ok(maintenance) = self.policy_state().map(|state| state.mods) else {
            return;
        };
        if !(policy.mod_auto_update || maintenance.manual)
            || before.phase != Phase::Running
            || history.current.is_some()
            || before.operations.current.is_some()
            || !matches!(maintenance.phase, ModPhase::Idle | ModPhase::Verifying)
        {
            return;
        }
        let result = crate::mods::monitor::check(&self.room, client, &maintenance.targets).await;
        let Ok(permit) = self.mutation.clone().try_acquire_owned() else {
            return;
        };
        if before != self.room.status()
            || history != *self.operations.lock().unwrap()
            || !self.policy().is_ok_and(|current| current == policy)
            || self.closing.load(Ordering::Acquire)
            || !self
                .policy_state()
                .is_ok_and(|state| state.mods == maintenance)
        {
            return;
        }
        let report = result.unwrap_or_else(|error| crate::mods::monitor::Check {
            checked_at: Some(Utc::now()),
            error: Some(error.to_string()),
            ..Default::default()
        });
        if maintenance.phase == ModPhase::Verifying
            && let Err(error) = self.update_policy("verification", None, |_, state| {
                state.verify_mod_update(Utc::now(), report.verification(&maintenance.targets))?;
                Ok(())
            })
        {
            *self.error.lock().unwrap() = Some(error.to_string());
        }
        let previous = std::mem::replace(&mut *self.mod_check.lock().unwrap(), report.clone());
        self.record_mod_check(&previous, &report);
        drop(permit);
        if maintenance.phase == ModPhase::Verifying || previous.targets != report.targets {
            self.changed.notify_one();
        }
    }

    async fn tick(self: &Arc<Self>) -> model::Result<()> {
        if self.closing.load(Ordering::Acquire) {
            return Ok(());
        }
        if self.room.requires_container_restart() {
            return Ok(());
        }
        let healthy = if self.room.status().phase == Phase::Running {
            let healthy = tokio::time::timeout(Duration::from_secs(5), self.room.refresh_health())
                .await
                .is_ok_and(|result| result.is_ok());
            let (expired, changed) = {
                let mut unhealthy = self.unhealthy.lock().unwrap();
                let changed = healthy == unhealthy.is_some();
                if healthy {
                    *unhealthy = None;
                    (false, changed)
                } else {
                    (
                        unhealthy.get_or_insert_with(Instant::now).elapsed()
                            >= Duration::from_secs(300),
                        changed,
                    )
                }
            };
            if changed {
                self.room.record_management(
                    "dst.policy.decision",
                    json!({"reason":"control_health","healthy":healthy}),
                    if healthy { "INFO" } else { "WARN" },
                    None,
                );
            }
            if expired {
                let controller = self.clone();
                return tokio::spawn(async move {
                    let mut context = OperationContext::new(Source::Health, Layer::Policy, None);
                    let operation = context.status("fault_cleanup", Target::Room);
                    let observation =
                        controller
                            .room
                            .begin_operation(&operation, &mut context, "accepted");
                    controller.room.record_management(
                        "dst.policy.decision",
                        json!({"reason":"control_unhealthy","duration_seconds":300}),
                        "ERROR",
                        Some(&context),
                    );
                    let result = controller
                        .room
                        .force_fault_in_context(
                            Error::new(
                                ErrorCode::NotReady,
                                "required control stayed unhealthy for 300 seconds",
                            ),
                            &controller.required(),
                            Some(&context),
                        )
                        .await;
                    controller.room.finish_operation(
                        &operation,
                        &context,
                        observation,
                        result.as_ref().map(|_| "completed"),
                    );
                    result?;
                    Ok(())
                })
                .await
                .map_err(|_| Error::new(ErrorCode::Internal, "health cleanup worker stopped"))?;
            }
            healthy
        } else {
            *self.unhealthy.lock().unwrap() = None;
            false
        };
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
        let mut inputs = if permit.is_some()
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
        if healthy && inputs.all_ready {
            let stable = self.observe_ready(&current);
            if stable >= recovery::STABLE_RESET {
                let state = self.recovery()?;
                if state.restarts_used > 0 || state.target.is_some() {
                    self.room
                        .update_recovery_if_changed("reset_stable", None, |lock| {
                            Ok(((), recovery::reset_after_stable(lock, stable)?))
                        })?;
                }
            }
        } else {
            *self.stable.lock().unwrap() = None;
        }
        let (plan, before_mods, after_mods) =
            self.room
                .update_recovery_if_changed("reset_open_window", None, |lock| {
                    let policy = Policy::load(lock)?;
                    let mut state = PolicyState::load(lock)?;
                    let before_mods = state.mods.clone();
                    let wall = Utc::now();
                    let monotonic = Instant::now();
                    let mut previous = self.last_tick.lock().unwrap();
                    inputs.previous_tick = previous.map(|(before, instant)| {
                        (before, monotonic.saturating_duration_since(instant))
                    });
                    let plan = policy.plan(&mut state, wall, &inputs)?;
                    let reset = if let Some(opening) = plan.new_opening {
                        let reset = recovery::reset_for_open_window(
                            lock,
                            opening.timestamp_millis().try_into()?,
                        )?;
                        self.changed.notify_one();
                        reset
                    } else {
                        false
                    };
                    state.save(lock)?;
                    *previous = Some((wall, monotonic));
                    Ok(((plan, before_mods, state.mods), reset))
                })?;
        self.record_mod_transition(&before_mods, &after_mods, "policy", None);
        let schedule_changed = {
            let mut previous = self.observed_schedule.lock().unwrap();
            let changed = previous.as_ref() != Some(&plan.schedule);
            *previous = Some(plan.schedule.clone());
            changed
        };
        if schedule_changed {
            self.room.record_management(
                "dst.policy.decision",
                json!({"reason":"schedule_changed","schedule":plan.schedule}),
                "INFO",
                None,
            );
        }
        if let Some(action) = plan.action {
            let check_mods = matches!(action, policy::Action::Start);
            let wake_policy = !matches!(action, policy::Action::Announce { .. });
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
            if priority_stop {
                let history = self.operations.lock().unwrap();
                if let Some(operation) = &history.current
                    && matches!(
                        operation.method.as_str(),
                        "stop" | "restart" | "update_mods"
                    )
                    && (self.room.finish_stop_notice() || operation.method == "stop")
                {
                    return Ok(());
                }
                if self.closing_game.swap(true, Ordering::AcqRel) {
                    return Ok(());
                }
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
                let result = controller.action(action, interrupted).await;
                if priority_stop {
                    controller.closing_game.store(false, Ordering::Release);
                }
                if let Err(error) = &result {
                    *controller.error.lock().unwrap() = Some(error.to_string());
                }
                drop(_permit);
                // Failed actions retry on the regular tick.
                if wake_policy && result.is_ok() {
                    if check_mods {
                        controller.mod_check_requested.notify_one();
                    }
                    controller.changed.notify_one();
                }
            });
        }
        Ok(())
    }

    async fn action(
        &self,
        action: policy::Action,
        mut interrupted: watch::Receiver<u64>,
    ) -> model::Result<()> {
        let recovering = if matches!(action, policy::Action::Start) {
            let state = self.recovery()?;
            let run = self.run_state()?;
            state.retry.is_some()
                || state.target.as_ref().is_some_and(|target| {
                    matches!(target.phase, TargetPhase::Preparing | TargetPhase::Starting)
                })
                || run.failure.is_some()
                || run.active
        } else {
            false
        };
        let (method, source, body) = match &action {
            policy::Action::Start => (
                "start",
                if recovering {
                    Source::Recovery
                } else {
                    Source::Schedule
                },
                json!({"action":"start"}),
            ),
            policy::Action::Stop { reason, .. } => (
                "stop",
                if *reason == policy::StopReason::Schedule {
                    Source::Schedule
                } else {
                    Source::ModAuto
                },
                json!({"action":"stop","reason":reason}),
            ),
            policy::Action::Announce { purpose, .. } => (
                "announce",
                if *purpose == policy::AnnouncementPurpose::Schedule {
                    Source::Schedule
                } else {
                    Source::ModAuto
                },
                json!({"action":"announce","purpose":purpose}),
            ),
            policy::Action::UpdateMods { attempt } => (
                "update_mods",
                Source::ModAuto,
                json!({"action":"update_mods","attempt":attempt}),
            ),
            policy::Action::Regenerate {
                expected_sessions,
                idle_since,
                world_day,
                retention_seconds,
            } => (
                "regenerate",
                Source::IdleRegeneration,
                json!({"action":"regenerate","idle_since":idle_since,"world_day":world_day,"retention_seconds":retention_seconds,"expected_shard_count":expected_sessions.len()}),
            ),
        };
        let pending = if method == "start" {
            self.pending_start.lock().unwrap().take()
        } else {
            if matches!(
                action,
                policy::Action::Stop { .. } | policy::Action::UpdateMods { .. }
            ) {
                self.pending_start.lock().unwrap().take();
            }
            None
        };
        let mut context = pending.as_ref().map_or_else(
            || OperationContext::new(source, Layer::Policy, None),
            |parent| parent.child(Layer::Policy),
        );
        if recovering {
            context.source = Source::Recovery;
        }
        if matches!(source, Source::ModAuto)
            && let Some(origin) = self.policy_state()?.mods.origin
        {
            context.parent_id = Some(origin.operation_id);
            context.source = match origin.source {
                policy::ModSource::Rpc => Source::Rpc,
                policy::ModSource::Manual => Source::Manual,
                policy::ModSource::ModAuto => Source::ModAuto,
            };
        }
        let operation = context.status(method, Target::Room);
        let observation = self
            .room
            .begin_operation(&operation, &mut context, "accepted");
        self.room
            .record_management("dst.policy.decision", body, "INFO", Some(&context));
        if let policy::Action::Regenerate {
            expected_sessions, ..
        } = &action
        {
            for (shard, session) in expected_sessions {
                self.room.record_management(
                    "dst.policy.decision",
                    json!({"action":"regenerate","shard":shard,"expected_session":session}),
                    "INFO",
                    Some(&context),
                );
            }
        }
        let result = tokio::select! {
            biased;
            _ = interrupted.changed() => Err(Error::new(ErrorCode::Unknown, "scheduled operation was interrupted")),
            result = self.perform_action(action, &context) => result,
        };
        self.room
            .finish_operation(&operation, &context, observation, result.as_ref().copied());
        result.map(|_| ())
    }

    async fn perform_action(
        &self,
        action: policy::Action,
        context: &OperationContext,
    ) -> model::Result<&'static str> {
        match action {
            policy::Action::Start => {
                self.start(Some(context)).await?;
            }
            policy::Action::Stop { reason, message } => {
                if let Some(message) = message {
                    self.room.stop_announcement(message, context).await;
                }
                if reason == policy::StopReason::Schedule {
                    self.room
                        .shutdown_priority_in_context(Some(context))
                        .await?;
                    self.record_stop()?;
                } else {
                    self.stop(None, Some(context), None).await?;
                }
            }
            policy::Action::Announce { message, .. } => {
                self.announce(message, context).await?;
            }
            policy::Action::UpdateMods { .. } => {
                let request = Envelope::new(
                    Target::Room,
                    Request::UpdateMods {
                        restart: false,
                        notice: None,
                    },
                )?;
                let result = self.room.invoke_in_context(request, context).await;
                *self.mod_check.lock().unwrap() = Default::default();
                let outcome = mod_download_outcome(&result);
                let resume_game = self.update_policy(outcome, Some(context), |_, state| {
                    let resume_game = state.mods.resume_game;
                    state.finish_mod_update(
                        Utc::now(),
                        result.as_ref().map(|_| ()).map_err(ToString::to_string),
                    )?;
                    Ok(resume_game)
                })?;
                result?;
                if !resume_game {
                    self.request_exit(0);
                }
                return Ok(outcome);
            }
            policy::Action::Regenerate {
                expected_sessions, ..
            } => {
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
                    .invoke_in_context(
                        Envelope::new(
                            Target::Room,
                            Request::Regenerate {
                                expected_session_id: sessions.get(&status.master).cloned(),
                                require_empty: Some(true),
                            },
                        )?,
                        context,
                    )
                    .await?;
            }
        }
        Ok("completed")
    }

    async fn announce(&self, message: String, context: &OperationContext) -> model::Result<Value> {
        self.room
            .invoke_in_context(
                Envelope::new(
                    Target::Room,
                    Request::Announce {
                        message,
                        count: 1,
                        interval: 30.0,
                    },
                )?,
                context,
            )
            .await
    }

    async fn manual_mod_update(
        &self,
        restart: bool,
        notice: Option<model::Countdown>,
        context: Option<&OperationContext>,
        reservation: Option<StopReservation>,
    ) -> model::Result<Value> {
        self.update_policy("manual_request", context, |_, state| {
            state
                .mods
                .request(restart, context.map(|ctx| ctx.id.as_str()))
        })?;
        self.pending_start.lock().unwrap().take();
        self.stop(notice, context, reservation).await?;
        Ok(json!({"queued": true, "restart": restart}))
    }

    async fn persist_failure(&self, context: &OperationContext) -> model::Result<()> {
        if self.run_state()?.failure.is_none() {
            self.room
                .force_fault_in_context(
                    Error::new(
                        ErrorCode::Internal,
                        "Agent exited after an unreported failure",
                    ),
                    &BTreeSet::new(),
                    Some(context),
                )
                .await?;
        }
        self.room.finish_failure()
    }

    fn service_stop_plan(
        &self,
        wall: chrono::DateTime<Utc>,
        at: Instant,
    ) -> model::Result<Option<(model::Countdown, Instant)>> {
        let schedule = self.policy()?.schedule_at(wall).map_err(internal)?;
        let scheduled = if schedule.open {
            schedule.next_change
        } else {
            Some(wall)
        };
        let maintenance = match self.policy_state()?.mods.phase {
            ModPhase::Announcing { deadline, .. } => Some(deadline),
            ModPhase::Stopping => Some(wall),
            _ => None,
        };
        Ok(scheduled
            .into_iter()
            .chain(maintenance)
            .min()
            .map(|deadline| {
                let remaining = (deadline - wall).to_std().unwrap_or_default();
                (
                    model::shutdown_notice().expect("default shutdown notice"),
                    at + remaining,
                )
            }))
    }

    fn expedite_shutdown(&self, reason: &str, context: &OperationContext) {
        if self.room.expedite_service_shutdown(reason, context) {
            self.interrupted
                .send_modify(|revision| *revision = revision.wrapping_add(1));
        }
    }

    async fn shutdown_game(
        &self,
        service: Option<ShutdownDeadlines>,
        context: &OperationContext,
    ) -> model::Result<()> {
        let handoff = service.map_or_else(
            || Instant::now() + Duration::from_secs(180),
            |deadlines| deadlines.handoff,
        );
        let permit = self.mutation.clone().acquire_owned();
        tokio::pin!(permit);
        let permit = match tokio::time::timeout_at(handoff, &mut permit).await {
            Ok(result) => result,
            Err(_) => {
                self.expedite_shutdown("handoff_timeout", context);
                permit.await
            }
        }
        .map_err(|_| Error::new(ErrorCode::Internal, "Agent operation queue closed"))?;
        let stopped = if let Some(deadlines) = service {
            self.room.shutdown_service(deadlines, context).await
        } else {
            self.room.shutdown_priority_in_context(Some(context)).await
        };
        drop(permit);
        let faults = self.room.wait_fault_cleanup().await;
        stopped?;
        faults?;
        Ok(())
    }
}

fn now_ms() -> u64 {
    crate::operation::timestamp() / 1_000_000
}
fn internal(error: impl std::fmt::Display) -> Error {
    Error::new(ErrorCode::Internal, error.to_string())
}

/// Return 1 only after a failed container's game children have been reaped.
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
        crate::telemetry::TelemetryConfig::for_agent(|name| std::env::var(name).ok())?,
    )?;
    if let Some(ports) = std::env::var_os(crate::deployment::PUBLISHED_PORTS_ENV) {
        room.set_external_ports(serde_json::from_str(
            ports.to_str().context("published ports are not UTF-8")?,
        )?)?;
    }
    let controller = Arc::new(Controller {
        room: room.clone(),
        last_tick: Mutex::new(None),
        closing: AtomicBool::new(false),
        exit: watch::channel(None).0,
        changed: Notify::new(),
        mutation: Arc::new(Semaphore::new(1)),
        interrupted: watch::channel(0).0,
        closing_game: AtomicBool::new(false),
        operations: Mutex::new(OperationHistory::default()),
        stable: Mutex::new(None),
        unhealthy: Mutex::new(None),
        error: Mutex::new(None),
        mod_check: Mutex::new(Default::default()),
        mod_check_requested: Notify::new(),
        pending_start: Mutex::new(None),
        observed_schedule: Mutex::new(None),
    });
    controller.initialize()?;
    let monitor_controller = controller.clone();
    let mut monitor_task = tokio::spawn(async move {
        let client = crate::mods::proxy_from_env()
            .map_err(|_| {
                crate::steam::Error::Transport(crate::steam::TransportError::Configuration)
            })
            .and_then(|proxy| crate::steam::Client::with_explicit_proxy(proxy.as_deref()));
        let mut interval = tokio::time::interval(crate::mods::monitor::INTERVAL);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                _ = interval.tick() => {},
                _ = monitor_controller.mod_check_requested.notified() => {},
            }
            monitor_controller.check_mods(&client).await;
        }
    });
    let (dispatch, mut requests) = mpsc::channel::<rpc::Incoming>(64);
    let (shutdown, receive_shutdown) = oneshot::channel();
    let server_socket = socket.clone();
    let mut server = tokio::task::spawn_local(async move {
        rpc::serve(&server_socket, dispatch, events, receive_shutdown).await
    });
    let mut room_changes = room.watch();
    let mut requested_exit = controller.exit.subscribe();
    let mut workers = JoinSet::new();
    let mut tick = tokio::time::interval(policy::CHECK_INTERVAL);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut policy_task = None;
    let mut policy_pending = true;
    let mut exit = 0;
    let mut terminal_error = None;
    let mut shutdown_source = Source::AgentShutdown;
    let mut signal_started = None;
    loop {
        if policy_pending && policy_task.is_none() {
            let controller = controller.clone();
            policy_task = Some(tokio::spawn(async move { controller.tick().await }));
            policy_pending = false;
        }
        tokio::select! {
            _ = requested_exit.changed() => {
                exit = requested_exit.borrow().unwrap_or(0);
                break;
            }
            result = &mut server => {
                terminal_error = result.map_err(anyhow::Error::from).and_then(|result| result.map_err(Into::into)).err();
                exit = 1;
                break;
            }
            _ = interrupt.recv() => { signal_started = Some((Instant::now(), Utc::now())); shutdown_source = Source::Signal; break; },
            _ = termination.recv() => { signal_started = Some((Instant::now(), Utc::now())); shutdown_source = Source::Signal; break; },
            result = &mut monitor_task => {
                terminal_error = Some(match result {
                    Err(error) => error.into(),
                    Ok(()) => anyhow::anyhow!("Workshop monitor stopped unexpectedly"),
                });
                exit = 1;
                break;
            }
            result = async { policy_task.as_mut().unwrap().await }, if policy_task.is_some() => {
                policy_task = None;
                match result {
                    Ok(Err(error)) => {
                        *controller.error.lock().unwrap() = Some(error.to_string());
                    }
                    Ok(Ok(())) => {},
                    Err(error) => { terminal_error = Some(error.into()); exit = 1; break; }
                }
            }
            _ = controller.changed.notified() => policy_pending = true,
            _ = room_changes.changed() => {
                if room.requires_container_restart() { exit = 1; break; }
            }
            _ = workers.join_next(), if !workers.is_empty() => {},
            Some(incoming) = requests.recv() => {
                let controller = controller.clone();
                workers.spawn(async move {
                    let mut reply = incoming.reply;
                    let query = !incoming.request.request.mutating();
                    let result = tokio::select! {
                        biased;
                        _ = reply.closed(), if query => return,
                        result = controller.invoke(incoming.request) => result,
                    };
                    let _ = reply.send(result);
                });
            }
            _ = tick.tick() => {
                if room.requires_container_restart() {
                    exit = 1;
                    break;
                }
                policy_pending = true;
            }
        }
    }
    controller.closing.store(true, Ordering::Release);
    controller.pending_start.lock().unwrap().take();
    let mut shutdown_context = OperationContext::new(shutdown_source, Layer::Policy, None);
    let shutdown_operation = shutdown_context.status("agent_shutdown", Target::Room);
    let shutdown_observation =
        room.begin_operation(&shutdown_operation, &mut shutdown_context, "accepted");
    let service = signal_started.map(|(start, wall)| {
        let deadlines = ShutdownDeadlines::new(start);
        room.begin_service_shutdown(deadlines);
        let plan = match controller.service_stop_plan(wall, start) {
            Ok(plan) => plan,
            Err(error) => {
                terminal_error.get_or_insert_with(|| error.into());
                None
            }
        };
        room.set_service_stop_plan(plan, &shutdown_context);
        deadlines
    });
    let finish = async {
        if !monitor_task.is_finished() {
            monitor_task.abort();
            let _ = monitor_task.await;
        }
        if let Some(task) = policy_task {
            task.abort();
            let _ = task.await;
        }
        let stopped = controller.shutdown_game(service, &shutdown_context).await;
        if let Err(error) = stopped {
            exit = 1;
            terminal_error.get_or_insert_with(|| error.into());
            let _ = room
                .invoke_in_context(
                    Envelope::new(Target::Room, Request::Kill {})?,
                    &shutdown_context,
                )
                .await;
        }
        if room.requires_container_restart() {
            exit = 1;
        }
        let recorded = if exit == 1 {
            controller.persist_failure(&shutdown_context).await
        } else if !room.driver_states().is_empty() {
            controller.record_stop()
        } else {
            Ok(())
        };
        if let Err(error) = recorded {
            terminal_error.get_or_insert_with(|| error.into());
        }
        let _ = tokio::time::timeout(Duration::from_secs(2), async {
            while workers.join_next().await.is_some() {}
        })
        .await;
        let shutdown_error = (terminal_error.is_some() || exit != 0)
            .then(|| Error::new(ErrorCode::Internal, "Agent shutdown failed"));
        room.finish_operation(
            &shutdown_operation,
            &shutdown_context,
            shutdown_observation,
            shutdown_error.as_ref().map_or(Ok("completed"), Err),
        );
        if let Err(error) = room.close_telemetry().await {
            eprintln!("warning: {error}: {}", error.details);
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
    };
    tokio::pin!(finish);
    let enforce = async {
        if let Some(deadlines) = service {
            room.enforce_service_deadlines(deadlines, &shutdown_context)
                .await;
        }
        std::future::pending::<()>().await;
    };
    tokio::pin!(enforce);
    let mut expedited = false;
    loop {
        tokio::select! {
            result = &mut finish => return result,
            _ = &mut enforce => unreachable!(),
            _ = termination.recv() => {
                if !expedited { controller.expedite_shutdown("subsequent_signal", &shutdown_context); expedited = true; }
            }
            _ = interrupt.recv() => {
                if !expedited { controller.expedite_shutdown("subsequent_signal", &shutdown_context); expedited = true; }
            }
            Some(incoming) = requests.recv() => {
                let _ = incoming.reply.send(controller.invoke(incoming.request).await);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt},
    };

    #[tokio::test]
    async fn request_history_survives_wait_timeout_and_keeps_start_parent_and_privacy() {
        for traces in [false, true] {
            let telemetry = crate::telemetry::TelemetryConfig {
                traces: traces.then(|| crate::telemetry::ExportConfig {
                    endpoint: "http://127.0.0.1:1".into(),
                    export_timeout: Duration::from_millis(30),
                    ..Default::default()
                }),
                ..Default::default()
            };
            let (_directory, _cluster, controller) = running_controller_with(telemetry).await;
            let mut events = controller
                .room
                .local_log()
                .events
                .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
            let error = controller
                .invoke(Envelope {
                    target: Target::Shard("Master".into()),
                    request: Request::Evaluate {
                        source: "slow".into(),
                    },
                    timeout: Some(0.01),
                })
                .await
                .unwrap_err();
            assert_eq!(error.code, ErrorCode::Unknown);
            wait_for(|| controller.operations.lock().unwrap().current.is_none()).await;
            let records = events.drain();
            let operations: Vec<_> = records
                .iter()
                .filter(|record| {
                    record["event_name"] == "dst.server.operation"
                        && record["body"]["method"] == "evaluate"
                })
                .collect();
            assert_eq!(operations.len(), 4);
            let parent = &operations[0]["attributes"]["dst.operation.id"];
            let child = &operations[1]["attributes"]["dst.operation.id"];
            assert_ne!(parent, child);
            assert_eq!(
                operations[1]["attributes"]["dst.operation.parent_id"],
                *parent
            );
            assert_eq!(operations[2]["attributes"]["dst.operation.id"], *child);
            assert_eq!(operations[3]["attributes"]["dst.operation.id"], *parent);
            assert!(
                operations[2..]
                    .iter()
                    .all(|record| record["body"]["outcome"] == "completed")
            );
            let uids: BTreeSet<_> = operations
                .iter()
                .map(|record| record["attributes"]["log.record.uid"].as_str().unwrap())
                .collect();
            assert_eq!(uids.len(), 4);
            for record in &operations {
                assert_eq!(record.get("trace_id").is_some(), traces);
                let log: crate::telemetry::LogEvent =
                    serde_json::from_value((*record).clone()).unwrap();
                let native = log.to_otlp().unwrap();
                assert_eq!(native.trace_id.len(), if traces { 16 } else { 0 });
                assert_eq!(native.span_id.len(), if traces { 8 } else { 0 });
            }
            if traces {
                assert_eq!(operations[0]["trace_id"], operations[1]["trace_id"]);
                assert_ne!(operations[0]["span_id"], operations[1]["span_id"]);
            }
            controller
                .invoke(
                    Envelope::new(
                        Target::Shard("Master".into()),
                        Request::Evaluate {
                            source: "PRIVATE_CODE_TOKEN".into(),
                        },
                    )
                    .unwrap(),
                )
                .await
                .unwrap();
            controller.stop(None, None, None).await.unwrap();
            let mut configuration = controller
                .room
                .with_lock(|lock| Ok(crate::settings::ClusterConfig::load(lock)?.into_value()))
                .unwrap();
            configuration["settings"]["cluster_name"] = json!("PRIVATE_CONFIGURATION_TOKEN");
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
            controller
                .invoke(Envelope::new(Target::Room, Request::Start {}).unwrap())
                .await
                .unwrap();
            let start_id = controller
                .pending_start
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .id
                .clone();
            controller
                .invoke(
                    Envelope::new(
                        Target::Room,
                        Request::Ban {
                            userid: "KU_TEST".into(),
                            seconds: None,
                        },
                    )
                    .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(
                controller
                    .pending_start
                    .lock()
                    .unwrap()
                    .as_ref()
                    .unwrap()
                    .id,
                start_id
            );
            let records = events.drain();
            let safe: Vec<_> = records
                .iter()
                .filter(|record| record["event_name"] == "dst.server.operation")
                .collect();
            assert!(!json!(safe).to_string().contains("PRIVATE_"));
            let player_records: Vec<_> = safe
                .iter()
                .filter(|record| record["body"].get("target_userid").is_some())
                .collect();
            assert_eq!(player_records.len(), 4);
            assert!(
                player_records
                    .iter()
                    .all(|record| record["body"]["method"] == "ban"
                        && record["body"]["target_userid"] == "KU_TEST")
            );
            assert!(safe.iter().any(
                |record| record["attributes"]["dst.operation.id"] == start_id
                    && record["body"]["outcome"] == "authorized"
            ));
            controller
                .action(policy::Action::Start, controller.interrupted.subscribe())
                .await
                .unwrap();
            let records = events.drain();
            let policy = records
                .iter()
                .find(|record| {
                    record["event_name"] == "dst.server.operation"
                        && record["attributes"]["dst.operation.layer"] == "policy"
                })
                .unwrap();
            assert_eq!(policy["attributes"]["dst.operation.parent_id"], start_id);
            assert!(
                records
                    .iter()
                    .any(|record| record["attributes"]["dst.operation.parent_id"]
                        == policy["attributes"]["dst.operation.id"]
                        && record["body"]["outcome"] == "ready")
            );
            for expected in [true, false] {
                assert_eq!(
                    controller
                        .invoke(
                            Envelope::new(
                                Target::Room,
                                Request::Kick {
                                    userid: "KU_TEST".into()
                                }
                            )
                            .unwrap()
                        )
                        .await
                        .unwrap(),
                    expected
                );
                assert!(
                    events
                        .drain()
                        .iter()
                        .any(|record| record["event_name"] == "dst.admin.operation"
                            && record["body"]["result"] == expected)
                );
            }
            controller.room.shutdown_priority().await.unwrap();
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn policy_interruption_finishes_its_parent_before_the_retained_room_result() {
        let (_directory, _cluster, controller) = running_controller().await;
        let mut events = controller
            .room
            .local_log()
            .events
            .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
        let action = policy::Action::Regenerate {
            expected_sessions: controller
                .room
                .status()
                .shards
                .into_iter()
                .map(|shard| (shard.name, shard.identity.unwrap().session_id))
                .collect(),
            idle_since: Utc::now(),
            world_day: 3,
            retention_seconds: 3600,
        };
        let running = controller.clone();
        let interrupted = controller.interrupted.subscribe();
        let task = tokio::spawn(async move { running.action(action, interrupted).await });
        wait_for(|| {
            controller
                .room
                .status()
                .operations
                .current
                .as_ref()
                .is_some_and(|operation| operation.method == "regenerate")
        })
        .await;
        controller
            .interrupted
            .send_modify(|revision| *revision += 1);
        assert_eq!(task.await.unwrap().unwrap_err().code, ErrorCode::Unknown);
        wait_for(|| controller.room.status().operations.current.is_none()).await;
        let records = events.drain();
        let finished: Vec<_> = records
            .iter()
            .filter(|record| {
                record["event_name"] == "dst.server.operation"
                    && record["body"]["phase"] == "finished"
            })
            .collect();
        assert_eq!(finished.len(), 2);
        assert_eq!(finished[0]["attributes"]["dst.operation.layer"], "policy");
        assert_eq!(finished[0]["body"]["outcome"], "unknown");
        assert_eq!(
            finished[1]["attributes"]["dst.operation.parent_id"],
            finished[0]["attributes"]["dst.operation.id"]
        );
        assert_eq!(finished[1]["body"]["outcome"], "completed");
        controller.room.shutdown_priority().await.unwrap();
    }

    #[tokio::test]
    async fn health_cleanup_keeps_its_parent_and_first_trigger_when_tick_is_aborted() {
        let (_directory, cluster, controller) = running_controller().await;
        fs::write(cluster.join("unhealthy"), "").unwrap();
        *controller.unhealthy.lock().unwrap() = Some(Instant::now() - Duration::from_secs(301));
        let mut events = controller
            .room
            .local_log()
            .events
            .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
        let running = controller.clone();
        let tick = tokio::spawn(async move { running.tick().await });
        let mut records = Vec::new();
        let parent = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                records.extend(events.drain());
                if let Some(record) = records.iter().find(|record| {
                    record["event_name"] == "dst.server.operation"
                        && record["attributes"]["dst.operation.source"] == "health"
                        && record["attributes"]["dst.operation.layer"] == "policy"
                }) {
                    break record["attributes"]["dst.operation.id"].clone();
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        tick.abort();
        let _ = tick.await;
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                records.extend(events.drain());
                if records.iter().any(|record| {
                    record["attributes"]["dst.operation.id"] == parent
                        && record["body"]["phase"] == "finished"
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let child = records
            .iter()
            .find(|record| {
                record["event_name"] == "dst.server.operation"
                    && record["attributes"]["dst.operation.parent_id"] == parent
            })
            .unwrap();
        let failure = controller.recovery().unwrap().last_failure.unwrap();
        assert_eq!(
            failure.trigger.operation_id.as_deref(),
            child["attributes"]["dst.operation.id"].as_str()
        );
        assert!(failure.complete);
        assert!(
            controller
                .room
                .driver_states()
                .values()
                .all(|state| !state.running)
        );
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn health_timeout_uses_the_room_scope_and_retains_cleanup() {
        for scenario in ["runtime", "missing_runtime", "protocol"] {
            let (_directory, cluster, controller) = running_controller().await;
            let hold = cluster.join("Caves/hold-health");
            fs::write(&hold, "").unwrap();
            if scenario == "missing_runtime" {
                fs::write(cluster.join("Master/reload-during-health"), "").unwrap();
            } else if scenario == "protocol" {
                write_marker(&cluster.join("Master/shutdown-mode"), "protocol");
            }
            *controller.unhealthy.lock().unwrap() = Some(Instant::now() - Duration::from_secs(301));
            let running = controller.clone();
            let tick = tokio::spawn(async move { running.tick().await });
            wait_for(|| {
                cluster.join("Caves/health-waiting").exists()
                    && controller
                        .room
                        .driver_states()
                        .values()
                        .all(|state| state.ready)
            })
            .await;
            if scenario == "missing_runtime" {
                assert!(controller.room.driver_states()["Master"].runtime.is_none());
            }
            tokio::time::timeout(Duration::from_secs(7), async {
                while controller.recovery().unwrap().last_failure.is_none() {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            })
            .await
            .unwrap();
            tick.abort();
            assert!(tick.await.unwrap_err().is_cancelled());
            fs::remove_file(hold).unwrap();
            let cleanup = controller.room.wait_fault_cleanup().await;
            assert_eq!(cleanup.is_ok(), scenario != "protocol");
            let failure = controller.recovery().unwrap().last_failure.unwrap();
            assert!(failure.complete);
            assert!(failure.shards.values().all(|shard| shard.failed));
            assert_eq!(
                matches!(failure.classification, Failure::Retryable),
                scenario == "runtime",
                "{scenario}"
            );
            if scenario != "runtime" {
                assert!(matches!(failure.classification, Failure::Unknown));
            }
            assert_eq!(controller.recovery().unwrap().restarts_used, 0);
            assert!(
                controller
                    .room
                    .driver_states()
                    .values()
                    .all(|state| !state.running)
            );
            controller.room.close_telemetry().await.unwrap();
        }
    }

    async fn reopened_controller(retryable: bool) -> (tempfile::TempDir, PathBuf, Arc<Controller>) {
        let (directory, cluster, controller) = running_controller().await;
        if retryable {
            write_marker(&cluster.join("Caves/fault-mode"), "exit");
            wait_for(|| controller.room.requires_container_restart()).await;
            let _ = controller.room.wait_fault_cleanup().await;
            assert!(matches!(
                controller.run_state().unwrap().failure,
                Some(Failure::Retryable)
            ));
        }
        for name in ["Master", "Caves"] {
            let saved = cluster.join(name).join(format!("save/session/S_{name}"));
            fs::create_dir_all(&saved).unwrap();
            fs::write(saved.join("0000000001"), "world").unwrap();
        }
        if retryable {
            controller.room.close_telemetry().await.unwrap();
        }
        drop(controller);
        wait_for(|| crate::files::RoomLock::try_acquire(&cluster).is_ok()).await;
        let room = Room::open(
            &cluster,
            directory.path().join("install/bin64/game.py"),
            DriverOptions::default(),
            EventHub::default(),
            Default::default(),
        )
        .unwrap();
        assert!(room.driver_states().is_empty());
        (directory, cluster, test_controller(room))
    }

    #[tokio::test]
    async fn new_room_maintenance_and_stop_preserve_the_previous_run() {
        for retryable in [false, true] {
            for operation in ["mods", "stop", "service", "kill", "persist", "begin-failed"] {
                let (_directory, cluster, controller) = reopened_controller(retryable).await;
                let before = controller.room.with_lock(|lock| lock.read_state()).unwrap();
                let context = OperationContext::new(Source::Signal, Layer::Policy, None);
                match operation {
                    "mods" => {
                        fs::write(
                            cluster.join("mods/dedicated_server_mods_setup.lua"),
                            "print('setup')\n",
                        )
                        .unwrap();
                        controller.initialize().unwrap();
                        assert_eq!(
                            controller.policy_state().unwrap().mods.phase,
                            ModPhase::Pending
                        );
                        controller.tick().await.unwrap();
                        wait_for(|| controller.mutation.available_permits() == 1).await;
                        assert_eq!(
                            fs::read_to_string(cluster.join("mods/downloads")).unwrap(),
                            "download\n"
                        );
                    }
                    "stop" => {
                        controller.stop(None, None, None).await.unwrap();
                    }
                    "service" => {
                        let deadlines = ShutdownDeadlines::new(Instant::now());
                        controller.closing.store(true, Ordering::Release);
                        controller.room.begin_service_shutdown(deadlines);
                        controller
                            .shutdown_game(Some(deadlines), &context)
                            .await
                            .unwrap();
                    }
                    "kill" => {
                        controller
                            .invoke(Envelope::new(Target::Room, Request::Kill {}).unwrap())
                            .await
                            .unwrap();
                        assert!(controller.room.requires_container_restart());
                    }
                    "persist" => {
                        controller.persist_failure(&context).await.unwrap();
                    }
                    "begin-failed" => {
                        let blocked_write = cluster.join(".dst-config-write.tmp");
                        fs::create_dir(&blocked_write).unwrap();
                        assert!(
                            controller
                                .room
                                .invoke(Envelope::new(Target::Room, Request::Start {}).unwrap())
                                .await
                                .is_err()
                        );
                        fs::remove_dir(blocked_write).unwrap();
                        controller.persist_failure(&context).await.unwrap();
                    }
                    _ => unreachable!(),
                }
                let after = controller.room.with_lock(|lock| lock.read_state()).unwrap();
                assert_eq!(
                    after["agent_run"], before["agent_run"],
                    "{operation}, retryable={retryable}"
                );
                assert_eq!(
                    after["recovery"], before["recovery"],
                    "{operation}, retryable={retryable}"
                );
                controller
                    .room
                    .with_lock(|lock| {
                        let run = recovery::prepare_recovery(lock, 1234)?;
                        assert!(matches!(
                            run.failure.unwrap_or(Failure::Retryable),
                            Failure::Retryable
                        ));
                        Ok(())
                    })
                    .unwrap();
                if operation == "mods" {
                    controller.recover(None).await.unwrap();
                    assert_eq!(controller.recovery().unwrap().restarts_used, 1);
                    assert!(!controller.recovery().unwrap().retry.unwrap().started);
                }
                controller.room.close_telemetry().await.unwrap();
            }
        }
    }

    #[tokio::test]
    async fn catalog_cancellation_preserves_unowned_run() {
        for retryable in [false, true] {
            let (_directory, cluster, controller) = reopened_controller(retryable).await;
            fs::write(cluster.join("Master/hold-catalog"), "").unwrap();
            let copy = controller.clone();
            let starting = tokio::spawn(async move { copy.start(None).await });
            let waiting = cluster.join("Master/catalog-waiting");
            wait_for(|| waiting.exists()).await;
            let pid: i32 = fs::read_to_string(waiting).unwrap().parse().unwrap();
            let before = controller.room.with_lock(|lock| lock.read_state()).unwrap();
            controller.closing.store(true, Ordering::Release);
            let mut deadlines = ShutdownDeadlines::new(Instant::now());
            deadlines.handoff = Instant::now() + Duration::from_millis(80);
            controller.room.begin_service_shutdown(deadlines);
            let context = OperationContext::new(Source::Signal, Layer::Policy, None);
            tokio::time::timeout(
                Duration::from_secs(3),
                controller.shutdown_game(Some(deadlines), &context),
            )
            .await
            .unwrap()
            .unwrap();
            assert!(starting.await.unwrap().unwrap_err().is_shutdown());
            assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
            let after = controller.room.with_lock(|lock| lock.read_state()).unwrap();
            assert_eq!(after["agent_run"], before["agent_run"]);
            assert_eq!(after["recovery"], before["recovery"]);
            controller.room.close_telemetry().await.unwrap();
        }
    }

    async fn failed_controller() -> (tempfile::TempDir, PathBuf, Arc<Controller>) {
        let (directory, cluster, controller) = running_controller().await;
        controller.stop(None, None, None).await.unwrap();
        for name in ["Master", "Caves"] {
            let saved = cluster.join(name).join(format!("save/session/S_{name}"));
            fs::create_dir_all(&saved).unwrap();
            fs::write(saved.join("0000000001"), "world").unwrap();
        }
        controller
            .room
            .with_lock(|lock| {
                recovery::begin_run(lock, "failed-attempt".into())?;
                recovery::record_failure(
                    lock,
                    "failed-attempt",
                    recovery::FailureTrigger {
                        kind: recovery::FailureTriggerKind::ProcessExited,
                        stage: recovery::FailureStage::Runtime,
                        code: Some(ErrorCode::Transport),
                        operation_id: None,
                        observed_at_ms: now_ms(),
                        force_requested: false,
                    },
                    &controller.required(),
                    &controller.room.driver_states(),
                    &controller.required(),
                )?;
                recovery::finish_failure(
                    lock,
                    "failed-attempt",
                    None,
                    &controller.room.driver_states(),
                    false,
                )
                .map(|_| ())
            })
            .unwrap();
        (directory, cluster, controller)
    }

    #[tokio::test]
    async fn fatal_or_closed_recovery_skips_catalog_and_catalog_errors_retain_cause() {
        for scenario in ["unknown", "closed", "catalog"] {
            let (_directory, cluster, controller) = failed_controller().await;
            if scenario != "catalog" {
                controller
                    .room
                    .with_lock(|lock| {
                        lock.update_state(|document| {
                            if scenario == "unknown" {
                                document.get_mut("agent_run").unwrap()["failure"] =
                                    json!({"kind":"unknown"});
                            } else {
                                document.get_mut("recovery").unwrap()["closed"] =
                                    json!("unknown_failure");
                            }
                            Ok(())
                        })
                    })
                    .unwrap();
            }
            fs::write(cluster.join("catalog-fail-after"), "0").unwrap();
            let result = controller.recover(None).await;
            let recovery = controller.recovery().unwrap();
            let record = recovery.last_failure.unwrap();
            assert_eq!(record.cause, recovery::FailureCause::RuntimeFailure);
            assert_eq!(recovery.restarts_used, 0);
            assert!(!controller.run_state().unwrap().active);
            if scenario == "catalog" {
                assert!(result.is_err());
                assert_eq!(
                    recovery.closed,
                    Some(recovery::ClosedReason::UnprovenSnapshots)
                );
                assert!(matches!(
                    record.catalog_error.unwrap().reason,
                    recovery::RecoveryErrorReason::MissingSession
                ));
                assert_eq!(
                    fs::read_to_string(cluster.join("Master/catalog-calls")).unwrap(),
                    "1"
                );
            } else {
                result.unwrap();
                assert!(!cluster.join("Master/catalog-calls").exists());
                assert!(!cluster.join("Caves/catalog-calls").exists());
            }
        }
    }

    #[tokio::test]
    async fn retry_rechecks_catalog_after_monotonic_wait_and_correlates_success() {
        for succeeds in [false, true] {
            let (_directory, cluster, controller) = failed_controller().await;
            if succeeds {
                controller.recover(None).await.unwrap();
                controller
                    .room
                    .with_lock(|lock| {
                        lock.update_state(|document| {
                            document.get_mut("recovery").unwrap()["retry"]["not_before_ms"] =
                                json!(now_ms() + 86_400_000);
                            Ok(())
                        })
                    })
                    .unwrap();
            } else {
                fs::write(cluster.join("catalog-fail-after"), "1").unwrap();
            }
            let started = Instant::now();
            let result = controller.start(None).await;
            assert!(started.elapsed() >= recovery::RETRY_DELAY);
            let recovery = controller.recovery().unwrap();
            assert_eq!(recovery.restarts_used, 1);
            let record = recovery.last_failure.unwrap();
            assert_eq!(record.cause, recovery::FailureCause::RuntimeFailure);
            if succeeds {
                result.unwrap();
                assert_eq!(record.result, Some(recovery::RecoveryResult::Succeeded));
                assert_eq!(
                    record.recovery_attempt_id,
                    controller.run_state().unwrap().attempt_id
                );
                assert!(recovery.retry.is_none());
                controller.stop(None, None, None).await.unwrap();
                assert_eq!(
                    controller.recovery().unwrap().last_failure.unwrap().result,
                    Some(recovery::RecoveryResult::Succeeded)
                );
            } else {
                assert!(result.is_err());
                assert_eq!(
                    record.catalog_error.unwrap().step,
                    RecoveryStep::RetryCatalog
                );
                assert_eq!(
                    recovery.closed,
                    Some(recovery::ClosedReason::UnprovenSnapshots)
                );
                assert!(!controller.run_state().unwrap().active);
                assert_eq!(
                    fs::read_to_string(cluster.join("Master/catalog-calls")).unwrap(),
                    "2"
                );
            }
        }
    }

    #[tokio::test]
    async fn next_opening_rechecks_the_saves_of_a_deferred_retry() {
        let (_directory, cluster, controller) = failed_controller().await;
        controller.recover(None).await.unwrap();
        controller
            .room
            .with_lock(|lock| one_hour_window(180).save(lock))
            .unwrap();
        controller.tick().await.unwrap();
        assert!(!controller.recovery().unwrap().retry.unwrap().started);

        fs::write(
            cluster.join("Caves/save/session/S_Caves/0000000002"),
            "new save",
        )
        .unwrap();
        controller
            .room
            .with_lock(|lock| one_hour_window(-30).save(lock))
            .unwrap();
        controller.tick().await.unwrap();
        wait_for(|| controller.mutation.available_permits() == 1).await;
        let state = controller.recovery().unwrap();
        assert_eq!(
            state.closed,
            Some(recovery::ClosedReason::CurrentSaveChanged)
        );
        assert_eq!(state.restarts_used, 1);
        assert!(!controller.run_state().unwrap().active);
        for shard in ["Master", "Caves"] {
            assert_eq!(
                fs::read_to_string(cluster.join(shard).join("native-lifecycle")).unwrap(),
                "start\nshutdown\nshutdown_save\n"
            );
        }
    }

    #[tokio::test]
    async fn service_closing_during_retry_does_not_launch_or_close_recovery() {
        let (_directory, cluster, controller) = failed_controller().await;
        let copy = controller.clone();
        let starting = tokio::spawn(async move { copy.start(None).await });
        wait_for(|| controller.recovery().unwrap().retry.is_some()).await;
        let before = serde_json::to_value(controller.recovery().unwrap()).unwrap();
        controller.closing.store(true, Ordering::Release);
        controller
            .room
            .begin_service_shutdown(ShutdownDeadlines::new(Instant::now()));
        tokio::time::pause();
        tokio::time::advance(recovery::RETRY_DELAY).await;
        let error = starting.await.unwrap().unwrap_err();
        tokio::time::resume();
        assert_eq!(error.code, ErrorCode::Unknown);
        assert!(error.is_shutdown());
        assert_eq!(
            serde_json::to_value(controller.recovery().unwrap()).unwrap(),
            before
        );
        for name in ["Master", "Caves"] {
            assert_eq!(
                fs::read_to_string(cluster.join(name).join("catalog-calls")).unwrap(),
                "1"
            );
            assert_eq!(
                fs::read_to_string(cluster.join(name).join("native-lifecycle")).unwrap(),
                "start\nshutdown\nshutdown_save\n"
            );
        }
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn service_cancellation_after_retry_launch_retains_failure_across_reopen() {
        let (directory, cluster, controller) = failed_controller().await;
        let held = cluster.join("Caves/hold-runtime");
        fs::write(&held, "").unwrap();
        let copy = controller.clone();
        let starting = tokio::spawn(async move { copy.start(None).await });
        tokio::time::timeout(recovery::RETRY_DELAY + Duration::from_secs(10), async {
            while !cluster.join("Caves/runtime-waiting").exists() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(controller.room.status().phase, Phase::Starting);
        assert!(controller.recovery().unwrap().retry.unwrap().started);
        let mut expected = json!(controller.recovery().unwrap().last_failure.unwrap());
        expected["result"] = json!("interrupted");

        let mut deadlines = ShutdownDeadlines::new(Instant::now());
        deadlines.handoff = Instant::now() + Duration::from_millis(80);
        controller.closing.store(true, Ordering::Release);
        controller.room.begin_service_shutdown(deadlines);
        let copy = controller.clone();
        let shutting = tokio::spawn(async move {
            let context = OperationContext::new(Source::Signal, Layer::Policy, None);
            copy.shutdown_game(Some(deadlines), &context).await
        });
        wait_for(|| controller.room.status().phase == Phase::Stopping).await;
        fs::remove_file(held).unwrap();
        let error = tokio::time::timeout(Duration::from_secs(3), starting)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Unknown);
        tokio::time::timeout(Duration::from_secs(3), shutting)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(!controller.room.requires_container_restart());
        assert!(!controller.run_state().unwrap().active);
        assert_eq!(
            json!(controller.recovery().unwrap().last_failure.unwrap()),
            expected
        );
        controller.room.close_telemetry().await.unwrap();
        drop(controller);
        wait_for(|| crate::files::RoomLock::try_acquire(&cluster).is_ok()).await;
        let room = Room::open(
            &cluster,
            directory.path().join("install/bin64/game.py"),
            DriverOptions::default(),
            EventHub::default(),
            Default::default(),
        )
        .unwrap();
        let controller = test_controller(room);
        controller.recover(None).await.unwrap();
        let state = controller.recovery().unwrap();
        assert_eq!(state.restarts_used, 2);
        assert!(!state.retry.unwrap().started);
        let record = state.last_failure.unwrap();
        assert!(matches!(
            record.decision,
            Some(Decision::RetryCurrent { attempt: 2, .. })
        ));
        let actual = json!(record);
        expected["restarts_used"] = json!(2);
        expected["decision"] = actual["decision"].clone();
        assert_eq!(actual, expected);
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn accepted_stop_intents_survive_service_shutdown_before_the_worker_runs() {
        for request in stopping_requests(None) {
            let (_directory, cluster, controller) = running_controller().await;
            let updating = matches!(request, Request::UpdateMods { .. });
            let mut records = controller
                .room
                .local_log()
                .events
                .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
            let mut invocation =
                Box::pin(controller.invoke(Envelope::new(Target::Room, request).unwrap()));
            assert!(futures::poll!(&mut invocation).is_pending());
            let accepted = controller
                .operations
                .lock()
                .unwrap()
                .current
                .clone()
                .unwrap();
            assert!(
                records
                    .drain()
                    .iter()
                    .any(|record| record["body"]["phase"] == "accepted"
                        && record["attributes"]["dst.operation.id"] == accepted.id)
            );
            controller.closing.store(true, Ordering::Release);
            controller.pending_start.lock().unwrap().take();
            let mut deadlines = ShutdownDeadlines::new(Instant::now());
            deadlines.notice =
                Instant::now() + model::NOTICE_SEND_TIMEOUT + Duration::from_millis(80);
            controller.room.begin_service_shutdown(deadlines);
            let error = tokio::time::timeout(Duration::from_secs(2), &mut invocation)
                .await
                .unwrap()
                .unwrap_err();
            assert_eq!(error.code, ErrorCode::Unknown);
            let context = OperationContext::new(Source::Signal, Layer::Policy, None);
            tokio::time::timeout(
                Duration::from_secs(2),
                controller.shutdown_game(Some(deadlines), &context),
            )
            .await
            .unwrap()
            .unwrap();
            assert!(
                !cluster.join("announcements").exists(),
                "accepted None must survive the Controller-to-Room handoff"
            );
            for shard in ["Master", "Caves"] {
                assert_eq!(
                    fs::read_to_string(cluster.join(shard).join("native-lifecycle")).unwrap(),
                    "start\nshutdown\nshutdown_save\n"
                );
            }
            let completed = controller.operations.lock().unwrap().last.clone().unwrap();
            assert_eq!(completed.id, accepted.id);
            assert_eq!(completed.error, Some(ErrorCode::Unknown));
            let records = records.drain();
            assert!(
                records
                    .iter()
                    .any(|record| record["body"]["reason"] == "explicit_none")
            );
            assert!(
                records
                    .iter()
                    .any(|record| record["body"]["phase"] == "finished"
                        && record["attributes"]["dst.operation.id"] == accepted.id
                        && record["body"]["error_code"] == "unknown")
            );
            assert_eq!(controller.policy_state().unwrap().mods.manual, updating);
            if updating {
                assert_eq!(
                    controller.policy_state().unwrap().mods.phase,
                    ModPhase::Pending
                );
            }
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn closing_allows_room_and_shard_status_and_preserves_other_rejections() {
        let (_directory, _cluster, controller) = running_controller().await;
        controller.closing.store(true, Ordering::Release);
        controller
            .room
            .begin_service_shutdown(ShutdownDeadlines::new(Instant::now()));
        let room = controller
            .invoke(Envelope::new(Target::Room, Request::Status {}).unwrap())
            .await
            .unwrap();
        assert_eq!(room["agent"]["ready"], false);
        let shard = controller
            .invoke(Envelope::new(Target::Shard("Master".into()), Request::Status {}).unwrap())
            .await
            .unwrap();
        assert_eq!(shard["name"], "Master");
        assert_eq!(
            controller
                .invoke(Envelope::new(Target::Shard("Missing".into()), Request::Status {}).unwrap())
                .await
                .unwrap_err()
                .code,
            ErrorCode::NotFound
        );
        for (target, request) in [
            (Target::Room, Request::ReadConfiguration {}),
            (Target::Shard("Master".into()), Request::Presence {}),
            (Target::Room, Request::Save {}),
            (Target::Room, Request::Kill {}),
        ] {
            assert_eq!(
                controller
                    .invoke(Envelope::new(target, request).unwrap())
                    .await
                    .unwrap_err()
                    .code,
                ErrorCode::NotReady
            );
        }
        assert_eq!(
            controller
                .invoke(Envelope {
                    target: Target::Shard(String::new()),
                    request: Request::Status {},
                    timeout: None
                })
                .await
                .unwrap_err()
                .code,
            ErrorCode::Invalid
        );
        controller.room.shutdown_priority().await.unwrap();
        controller.room.close_telemetry().await.unwrap();
    }

    fn stopping_requests(notice: Option<model::Countdown>) -> [Request; 3] {
        [
            Request::Stop {
                notice: notice.clone(),
            },
            Request::Restart {
                notice: notice.clone(),
            },
            Request::UpdateMods {
                notice,
                restart: true,
            },
        ]
    }

    #[tokio::test]
    async fn accepted_stop_requests_own_room_resources_before_child_admission() {
        for request in stopping_requests(None) {
            let (_directory, _cluster, controller) = running_controller().await;
            let restart = matches!(request, Request::Restart { .. });
            let mut invocation =
                Box::pin(controller.invoke(Envelope::new(Target::Room, request).unwrap()));
            assert!(futures::poll!(&mut invocation).is_pending());
            let mut competing = Box::pin(
                controller.room.invoke(
                    Envelope::new(
                        Target::Shard("Master".into()),
                        Request::Evaluate {
                            source: "true".into(),
                        },
                    )
                    .unwrap(),
                ),
            );
            let std::task::Poll::Ready(Err(error)) = futures::poll!(&mut competing) else {
                panic!("a retained parent must already own Room mutation")
            };
            assert_eq!(error.code, ErrorCode::Busy);
            drop(competing);
            tokio::time::timeout(Duration::from_secs(3), invocation)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(
                controller.room.status().phase,
                if restart {
                    Phase::Running
                } else {
                    Phase::Stopped
                }
            );
            assert_eq!(controller.mutation.available_permits(), 1);
            if restart {
                controller.room.shutdown_priority().await.unwrap();
            }
            let stopped =
                tokio::time::timeout(Duration::from_secs(1), controller.room.while_stopped())
                    .await
                    .unwrap()
                    .unwrap();
            drop(stopped);
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn direct_room_kill_interrupts_a_stop_reserved_before_child_admission() {
        for request in stopping_requests(None) {
            let (_directory, _cluster, controller) = running_controller().await;
            let mut records = controller
                .room
                .local_log()
                .events
                .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
            let mut invocation =
                Box::pin(controller.invoke(Envelope::new(Target::Room, request).unwrap()));
            assert!(futures::poll!(&mut invocation).is_pending());
            let parent = controller
                .operations
                .lock()
                .unwrap()
                .current
                .clone()
                .unwrap();
            let mut killing = Box::pin(
                controller
                    .room
                    .invoke(Envelope::new(Target::Room, Request::Kill {}).unwrap()),
            );
            assert!(futures::poll!(&mut killing).is_pending());
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(3), invocation)
                    .await
                    .unwrap()
                    .unwrap_err()
                    .code,
                ErrorCode::Unknown
            );
            tokio::time::timeout(Duration::from_secs(3), &mut killing)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(controller.mutation.available_permits(), 1);
            assert!(
                !records
                    .drain()
                    .iter()
                    .any(|record| record["body"]["method"] == "stop"
                        && record["body"]["phase"] == "started"
                        && record["attributes"]["dst.operation.parent_id"] == parent.id),
                "a new watch subscription must not forget the Kill revision"
            );
            assert!(
                controller
                    .room
                    .driver_states()
                    .values()
                    .all(|state| !state.running)
            );
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn rejected_stop_requests_do_not_publish_acceptance_or_clear_pending_start() {
        for scenario in [
            "controller_busy",
            "room_busy",
            "pending",
            "updating",
            "gate",
            "notice",
            "scope",
            "timeout",
        ] {
            let (_directory, _cluster, controller) = running_controller().await;
            let pending = OperationContext::new(Source::Rpc, Layer::Request, None);
            *controller.pending_start.lock().unwrap() = Some(pending.clone());
            let previous = controller.operations.lock().unwrap().clone();
            let mut records = controller
                .room
                .local_log()
                .events
                .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
            let permit = (scenario == "controller_busy")
                .then(|| controller.mutation.clone().try_acquire_owned().unwrap());
            let mut room_work = if scenario == "room_busy" {
                let mut call = Box::pin(
                    controller.room.invoke(
                        Envelope::new(
                            Target::Shard("Master".into()),
                            Request::Evaluate {
                                source: "slow".into(),
                            },
                        )
                        .unwrap(),
                    ),
                );
                assert!(futures::poll!(&mut call).is_pending());
                Some(call)
            } else {
                None
            };
            if scenario == "pending" || scenario == "updating" {
                controller
                    .update_policy("test_pending", None, |_, state| {
                        state.queue_mod_update(true)?;
                        if scenario == "updating" {
                            state.mods.phase = ModPhase::Updating;
                            state.mods.attempts = 1;
                        }
                        Ok(())
                    })
                    .unwrap();
            } else if scenario == "gate" {
                controller
                    .room
                    .begin_service_shutdown(ShutdownDeadlines::new(Instant::now()));
            }
            let mut envelope = Envelope::new(
                Target::Room,
                if scenario == "pending" || scenario == "updating" {
                    Request::UpdateMods {
                        notice: None,
                        restart: true,
                    }
                } else {
                    Request::Stop { notice: None }
                },
            )
            .unwrap();
            let expected = match scenario {
                "notice" => {
                    envelope.request = Request::Stop {
                        notice: Some(model::Countdown {
                            template: "closing".into(),
                            delay: -1.0,
                            interval: 1.0,
                            parameters: Default::default(),
                        }),
                    };
                    ErrorCode::Invalid
                }
                "scope" => {
                    envelope.target = Target::Shard("Master".into());
                    envelope.request = Request::UpdateMods {
                        notice: None,
                        restart: true,
                    };
                    ErrorCode::Unsupported
                }
                "timeout" => {
                    envelope.timeout = Some(0.0);
                    ErrorCode::Invalid
                }
                "gate" => ErrorCode::NotReady,
                _ => ErrorCode::Busy,
            };
            let mut invocation = Box::pin(controller.invoke(envelope));
            let std::task::Poll::Ready(Err(error)) = futures::poll!(&mut invocation) else {
                panic!("{scenario} must fail before acceptance")
            };
            assert_eq!(error.code, expected, "{scenario}");
            drop(invocation);
            assert_eq!(*controller.operations.lock().unwrap(), previous);
            assert_eq!(
                controller
                    .pending_start
                    .lock()
                    .unwrap()
                    .as_ref()
                    .unwrap()
                    .id,
                pending.id
            );
            assert!(!records.drain().iter().any(
                |record| record["attributes"]["dst.operation.layer"] == "request"
                    && record["body"]["phase"] == "accepted"
            ));
            drop(permit);
            if let Some(call) = room_work.as_mut() {
                call.await.unwrap();
            }
            drop(room_work);
            controller.room.shutdown_priority().await.unwrap();
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn busy_controller_requests_preserve_an_existing_room_stop_notice() {
        let (_directory, cluster, controller) = running_controller().await;
        let hold = cluster.join("Master/hold-announce");
        fs::write(&hold, "").unwrap();
        let mut records = controller
            .room
            .local_log()
            .events
            .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
        let notice = model::Countdown {
            template: "original {remaining}".into(),
            delay: 0.15,
            interval: 30.0,
            parameters: Default::default(),
        };
        let mut stopping = Box::pin(
            controller.room.invoke(
                Envelope::new(
                    Target::Room,
                    Request::Stop {
                        notice: Some(notice),
                    },
                )
                .unwrap(),
            ),
        );
        assert!(futures::poll!(&mut stopping).is_pending());
        wait_for(|| cluster.join("Master/announce-waiting").exists()).await;
        let original = controller.room.status().operations.current.unwrap();
        let previous = controller.operations.lock().unwrap().clone();
        let pending = OperationContext::new(Source::Rpc, Layer::Request, None);
        *controller.pending_start.lock().unwrap() = Some(pending.clone());
        for request in stopping_requests(None) {
            let mut invocation =
                Box::pin(controller.invoke(Envelope::new(Target::Room, request).unwrap()));
            let std::task::Poll::Ready(Err(error)) = futures::poll!(&mut invocation) else {
                panic!("a running Room stop must reject the Controller before acceptance")
            };
            assert_eq!(error.code, ErrorCode::Busy);
            assert_eq!(*controller.operations.lock().unwrap(), previous);
            assert_eq!(
                controller.room.status().operations.current.as_ref(),
                Some(&original)
            );
            assert_eq!(
                controller
                    .pending_start
                    .lock()
                    .unwrap()
                    .as_ref()
                    .unwrap()
                    .id,
                pending.id
            );
        }
        assert_eq!(
            controller.policy_state().unwrap().mods.phase,
            ModPhase::Idle
        );
        fs::remove_file(hold).unwrap();
        tokio::time::timeout(Duration::from_secs(2), &mut stopping)
            .await
            .unwrap()
            .unwrap();
        let completed = controller.room.status().operations.last.unwrap();
        assert_eq!(completed.id, original.id);
        assert_eq!(completed.error, None);
        let announcements = fs::read_to_string(cluster.join("announcements")).unwrap();
        assert_eq!(announcements.lines().count(), 2);
        for line in announcements.lines() {
            let announcement: Value = serde_json::from_str(line).unwrap();
            assert!(
                announcement["message"]
                    .as_str()
                    .unwrap()
                    .starts_with("original ")
            );
        }
        let records = records.drain();
        assert!(
            !records.iter().any(
                |record| record["attributes"]["dst.operation.layer"] == "request"
                    && record["body"]["phase"] == "accepted"
            )
        );
        for stage in ["first", "final"] {
            assert_eq!(
                records
                    .iter()
                    .filter(
                        |record| record["event_name"] == "dst.server.shutdown_notice"
                            && record["attributes"]["dst.operation.id"] == original.id
                            && record["body"]["stage"] == stage
                            && record["body"]["outcome"] == "accepted"
                    )
                    .count(),
                1
            );
        }
        assert_eq!(controller.mutation.available_permits(), 1);
        drop(controller.room.while_stopped().await.unwrap());
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn room_request_limit_rejects_stop_before_controller_acceptance() {
        let (_directory, cluster, controller) = running_controller().await;
        fs::write(cluster.join("Master/hold-presence"), "").unwrap();
        let mut held = Vec::new();
        for _ in 0..=64 {
            let mut query = Box::pin(controller.room.invoke(
                Envelope::new(Target::Shard("Master".into()), Request::Presence {}).unwrap(),
            ));
            match futures::poll!(&mut query) {
                std::task::Poll::Pending => held.push(query),
                std::task::Poll::Ready(Err(error)) if error.code == ErrorCode::Busy => break,
                result => panic!("unexpected held query result: {result:?}"),
            }
        }
        assert_eq!(held.len(), 64);
        let previous = controller.operations.lock().unwrap().clone();
        for request in stopping_requests(None) {
            let mut invocation =
                Box::pin(controller.invoke(Envelope::new(Target::Room, request).unwrap()));
            let std::task::Poll::Ready(Err(error)) = futures::poll!(&mut invocation) else {
                panic!("the parent must reserve a Room request slot before acceptance")
            };
            assert_eq!(error.code, ErrorCode::Busy);
            assert_eq!(*controller.operations.lock().unwrap(), previous);
        }
        drop(held);
        fs::remove_file(cluster.join("Master/hold-presence")).unwrap();
        controller.room.shutdown_priority().await.unwrap();
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn dropping_the_caller_keeps_the_accepted_stop_owned_through_handoff() {
        let (_directory, _cluster, controller) = running_controller().await;
        let mut invocation = Box::pin(
            controller.invoke(Envelope::new(Target::Room, Request::Stop { notice: None }).unwrap()),
        );
        assert!(futures::poll!(&mut invocation).is_pending());
        let parent = controller
            .operations
            .lock()
            .unwrap()
            .current
            .clone()
            .unwrap();
        drop(invocation);
        let mut competing = Box::pin(
            controller.room.invoke(
                Envelope::new(
                    Target::Shard("Master".into()),
                    Request::Evaluate {
                        source: "true".into(),
                    },
                )
                .unwrap(),
            ),
        );
        let std::task::Poll::Ready(Err(error)) = futures::poll!(&mut competing) else {
            panic!("caller cancellation must not release the retained reservation")
        };
        assert_eq!(error.code, ErrorCode::Busy);
        drop(competing);
        wait_for(|| {
            controller.mutation.available_permits() == 1
                && controller
                    .operations
                    .lock()
                    .unwrap()
                    .last
                    .as_ref()
                    .is_some_and(|operation| operation.id == parent.id)
        })
        .await;
        assert_eq!(controller.room.status().phase, Phase::Stopped);
        assert_eq!(
            controller
                .operations
                .lock()
                .unwrap()
                .last
                .as_ref()
                .unwrap()
                .error,
            None
        );
        drop(controller.room.while_stopped().await.unwrap());
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn failure_before_stop_handoff_releases_resources_and_discards_only_its_intent() {
        for queue_failure in [true, false] {
            let (_directory, cluster, controller) = running_controller().await;
            let blocked = cluster.join(".dst-config-write.tmp");
            if queue_failure {
                fs::create_dir(&blocked).unwrap();
            }
            let request = if queue_failure {
                Request::UpdateMods {
                    notice: None,
                    restart: true,
                }
            } else {
                Request::Stop { notice: None }
            };
            let mut invocation =
                Box::pin(controller.invoke(Envelope::new(Target::Room, request).unwrap()));
            assert!(futures::poll!(&mut invocation).is_pending());
            if !queue_failure {
                controller
                    .interrupted
                    .send_modify(|revision| *revision += 1);
            }
            let error = tokio::time::timeout(Duration::from_secs(2), invocation)
                .await
                .unwrap()
                .unwrap_err();
            assert_eq!(
                error.code,
                if queue_failure {
                    ErrorCode::Internal
                } else {
                    ErrorCode::Unknown
                }
            );
            if queue_failure {
                fs::remove_dir(blocked).unwrap();
            }
            assert_eq!(controller.mutation.available_permits(), 1);
            assert_eq!(
                controller.policy_state().unwrap().mods.phase,
                ModPhase::Idle
            );
            assert_eq!(controller.room.status().phase, Phase::Running);
            controller.closing.store(true, Ordering::Release);
            let mut deadlines = ShutdownDeadlines::new(Instant::now());
            deadlines.notice =
                Instant::now() + model::NOTICE_SEND_TIMEOUT + Duration::from_millis(80);
            controller.room.begin_service_shutdown(deadlines);
            let context = OperationContext::new(Source::Signal, Layer::Policy, None);
            tokio::time::timeout(
                Duration::from_secs(2),
                controller.shutdown_game(Some(deadlines), &context),
            )
            .await
            .unwrap()
            .unwrap();
            assert_eq!(
                fs::read_to_string(cluster.join("announcements"))
                    .unwrap()
                    .lines()
                    .count(),
                2,
                "the failed parent's None must not suppress a later service notice"
            );
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn cancelling_parent_wait_after_handoff_preserves_child_ownership_and_notice() {
        let (_directory, cluster, controller) = running_controller().await;
        let hold = cluster.join("Master/hold-announce");
        fs::write(&hold, "").unwrap();
        let notice = model::Countdown {
            template: "existing {remaining}".into(),
            delay: 0.15,
            interval: 30.0,
            parameters: Default::default(),
        };
        let mut invocation = Box::pin(
            controller.invoke(
                Envelope::new(
                    Target::Room,
                    Request::Stop {
                        notice: Some(notice),
                    },
                )
                .unwrap(),
            ),
        );
        assert!(futures::poll!(&mut invocation).is_pending());
        let parent = controller
            .operations
            .lock()
            .unwrap()
            .current
            .clone()
            .unwrap();
        wait_for(|| cluster.join("Master/announce-waiting").exists()).await;
        let child = controller.room.status().operations.current.unwrap();
        assert_ne!(child.id, parent.id);
        controller
            .interrupted
            .send_modify(|revision| *revision += 1);
        assert_eq!(invocation.await.unwrap_err().code, ErrorCode::Unknown);
        let mut competing = Box::pin(
            controller.room.invoke(
                Envelope::new(
                    Target::Shard("Master".into()),
                    Request::Evaluate {
                        source: "true".into(),
                    },
                )
                .unwrap(),
            ),
        );
        let std::task::Poll::Ready(Err(error)) = futures::poll!(&mut competing) else {
            panic!("the child must retain its own Room permit")
        };
        assert_eq!(error.code, ErrorCode::Busy);
        drop(competing);
        controller.closing.store(true, Ordering::Release);
        let deadlines = ShutdownDeadlines::new(Instant::now());
        controller.room.begin_service_shutdown(deadlines);
        fs::remove_file(hold).unwrap();
        let context = OperationContext::new(Source::Signal, Layer::Policy, None);
        tokio::time::timeout(
            Duration::from_secs(2),
            controller.shutdown_game(Some(deadlines), &context),
        )
        .await
        .unwrap()
        .unwrap();
        let completed = controller.room.status().operations.last.unwrap();
        assert_eq!(completed.id, child.id);
        assert_eq!(completed.error, None);
        assert_eq!(
            controller
                .operations
                .lock()
                .unwrap()
                .last
                .as_ref()
                .unwrap()
                .error,
            Some(ErrorCode::Unknown)
        );
        let announcements = fs::read_to_string(cluster.join("announcements")).unwrap();
        assert_eq!(announcements.lines().count(), 2);
        for line in announcements.lines() {
            let announcement: Value = serde_json::from_str(line).unwrap();
            assert!(
                announcement["message"]
                    .as_str()
                    .unwrap()
                    .starts_with("existing ")
            );
        }
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn controller_and_room_share_handoff_and_reap_a_cancelled_updater() {
        let (_directory, cluster, controller) = running_controller().await;
        fs::write(
            cluster.join("mods/dedicated_server_mods_setup.lua"),
            "ServerModSetup(1)\n",
        )
        .unwrap();
        fs::write(cluster.join("mods/hold-download"), "").unwrap();
        let room = controller.room.clone();
        let updating = tokio::spawn(async move {
            room.invoke(
                Envelope::new(
                    Target::Room,
                    Request::UpdateMods {
                        notice: None,
                        restart: true,
                    },
                )
                .unwrap(),
            )
            .await
        });
        let pid_path = cluster.join("mods/download-pid");
        wait_for(|| pid_path.exists()).await;
        let pid: i32 = fs::read_to_string(pid_path).unwrap().parse().unwrap();
        updating.abort();
        assert!(updating.await.unwrap_err().is_cancelled());
        let permit = controller.mutation.clone().acquire_owned().await.unwrap();
        let start = Instant::now();
        let mut deadlines = ShutdownDeadlines::new(start);
        deadlines.handoff = start + Duration::from_millis(220);
        controller.closing.store(true, Ordering::Release);
        controller.room.begin_service_shutdown(deadlines);
        let mut records = controller
            .room
            .local_log()
            .events
            .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
        let copy = controller.clone();
        let shutting = tokio::spawn(async move {
            let context = OperationContext::new(Source::Signal, Layer::Policy, None);
            copy.shutdown_game(Some(deadlines), &context).await
        });
        tokio::time::sleep(Duration::from_millis(140)).await;
        drop(permit);
        tokio::time::timeout(Duration::from_secs(3), shutting)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(start.elapsed() >= Duration::from_millis(220));
        assert!(start.elapsed() < Duration::from_millis(350));
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        let last = controller.room.status().operations.last.unwrap();
        assert_eq!(last.error, Some(ErrorCode::Unknown));
        assert!(!controller.room.requires_container_restart());
        assert!(
            records
                .drain()
                .iter()
                .any(|record| record["body"]["reason"] == "handoff_timeout")
        );
        for name in ["Master", "Caves"] {
            assert_eq!(
                fs::read_to_string(cluster.join(name).join("native-lifecycle")).unwrap(),
                "start\nshutdown\nshutdown_save\n"
            );
        }
        controller.room.close_telemetry().await.unwrap();
    }

    async fn running_controller() -> (tempfile::TempDir, PathBuf, Arc<Controller>) {
        running_controller_with(Default::default()).await
    }

    async fn running_controller_with(
        telemetry: crate::telemetry::TelemetryConfig,
    ) -> (tempfile::TempDir, PathBuf, Arc<Controller>) {
        let directory = tempfile::tempdir().unwrap();
        let cluster = directory.path().join("cluster");
        fs::create_dir(&cluster).unwrap();
        fs::write(cluster.join("cluster.ini"), "[SHARD]\nshard_enabled=true\ncluster_key=closing-fixture\nmaster_port=19000\nmaster_ip=127.0.0.1\n").unwrap();
        for (name, id, port) in [("Master", 1, 19100), ("Caves", 2, 19200)] {
            fs::create_dir(cluster.join(name)).unwrap();
            fs::write(cluster.join(name).join("server.ini"), format!("[NETWORK]\nserver_port={port}\n[SHARD]\nis_master={}\nid={id}\nname={name}\n[STEAM]\nmaster_server_port={}\n", id == 1, port + 1)).unwrap();
        }
        let executable = directory.path().join("install/bin64/game.py");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::create_dir(cluster.join("mods")).unwrap();
        std::os::unix::fs::symlink(cluster.join("mods"), directory.path().join("install/mods"))
            .unwrap();
        let downloader = r#"#!/usr/bin/env python3
import os, pathlib, sys, time
if '-only_update_server_mods' in sys.argv:
    mods = pathlib.Path(__file__).resolve().parents[1] / 'mods'
    (mods / 'download-pid.tmp').write_text(str(os.getpid()))
    (mods / 'download-pid.tmp').replace(mods / 'download-pid')
    while (mods / 'hold-download').exists(): time.sleep(0.005)
    with (mods / 'downloads').open('a') as output:
        output.write('download\n')
    print('FinishDownloadingServerMods Complete! Process trying to quit nicely..', flush=True)
    raise SystemExit(0)
"#;
        fs::write(
            &executable,
            format!("{downloader}{}", include_str!("../tests/fixtures/game.py")),
        )
        .unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let room = Room::open(
            &cluster,
            executable,
            DriverOptions::default(),
            EventHub::default(),
            telemetry,
        )
        .unwrap();
        let controller = test_controller(room);
        controller.start(None).await.unwrap();
        (directory, cluster, controller)
    }

    fn test_controller(room: Arc<Room>) -> Arc<Controller> {
        Arc::new(Controller {
            room,
            last_tick: Mutex::new(None),
            closing: AtomicBool::new(false),
            exit: watch::channel(None).0,
            changed: Notify::new(),
            mutation: Arc::new(Semaphore::new(1)),
            interrupted: watch::channel(0).0,
            closing_game: AtomicBool::new(false),
            operations: Mutex::new(OperationHistory::default()),
            stable: Mutex::new(None),
            unhealthy: Mutex::new(None),
            error: Mutex::new(None),
            mod_check: Mutex::new(Default::default()),
            mod_check_requested: Notify::new(),
            pending_start: Mutex::new(None),
            observed_schedule: Mutex::new(None),
        })
    }

    fn loaded_mod_fixture(cluster: &std::path::Path) -> Vec<u8> {
        fs::write(
            cluster.join("mods/dedicated_server_mods_setup.lua"),
            "ServerModSetup('2189004162')\n",
        )
        .unwrap();
        for name in ["Master", "Caves"] {
            fs::write(
                cluster.join(name).join("enabled-mods.json"),
                r#"[{"id":"workshop-2189004162","version":"1"},{"id":"local-mod","version":"local"}]"#,
            )
            .unwrap();
        }
        let fixture: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/steam/published_file_details.json"
        ))
        .unwrap();
        let mut item = fixture["response"]["publishedfiledetails"][0].clone();
        item["hcontent_file"] = json!("1");
        item["tags"] = json!([{"tag":"version:2"}]);
        serde_json::to_vec(
            &json!({"response":{"result":1,"resultcount":1,"publishedfiledetails":[item]}}),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn equal_declared_versions_ignore_content_metadata_changes() {
        use crate::steam::tests::{Server, response};
        let (_directory, cluster, controller) = running_controller().await;
        let mut body: Value = serde_json::from_slice(&loaded_mod_fixture(&cluster)).unwrap();
        body["response"]["publishedfiledetails"][0]["tags"] = json!([{"tag":"version:1.0.50"}]);
        for shard in ["Master", "Caves"] {
            fs::write(
                cluster.join(shard).join("enabled-mods.json"),
                r#"[{"id":"workshop-2189004162","version":"1.0.50"}]"#,
            )
            .unwrap();
        }
        let server =
            Server::start(vec![response(200, "", &serde_json::to_vec(&body).unwrap())]).await;
        controller.check_mods(&Ok(server.client())).await;
        let outdated = controller.mod_check.lock().unwrap().targets.clone();
        controller.room.shutdown_priority().await.unwrap();
        assert!(
            outdated.is_empty(),
            "equal game versions must not restart the room"
        );
    }

    #[tokio::test]
    async fn workshop_checks_feed_maintenance_and_report_network_failures_without_stopping_games() {
        use crate::steam::tests::{Server, response};
        let (_directory, cluster, controller) = running_controller().await;
        controller
            .check_mods(&Err(crate::steam::Error::Timeout))
            .await;
        {
            let report = controller.mod_check.lock().unwrap();
            assert!(report.checked_at.is_some());
            assert!(report.error.is_none());
            assert!(report.versions.is_empty());
        }
        let queries =
            |shard: &str| fs::read_to_string(cluster.join(shard).join("mod-queries")).unwrap();
        for shard in ["Master", "Caves"] {
            assert_eq!(queries(shard), "mods\n");
        }
        let body = loaded_mod_fixture(&cluster);
        let server = Server::start(vec![response(200, "", &body)]).await;
        controller.check_mods(&Ok(server.client())).await;
        for shard in ["Master", "Caves"] {
            assert_eq!(queries(shard), "mods\nmods\nmods\n");
        }
        assert_eq!(
            server.count(),
            1,
            "the same Mod on two shards is queried once"
        );
        assert_eq!(
            controller
                .mod_check
                .lock()
                .unwrap()
                .targets
                .keys()
                .copied()
                .collect::<BTreeSet<_>>(),
            BTreeSet::from([2189004162])
        );
        let inputs = controller
            .inputs(&controller.room.status(), false)
            .await
            .unwrap();
        let mut state = controller.policy_state().unwrap();
        let plan = controller
            .policy()
            .unwrap()
            .plan(&mut state, Utc::now(), &inputs)
            .unwrap();
        assert!(matches!(plan.action, Some(policy::Action::Announce { .. })));
        let failed = Server::start(vec![response(403, "", b"private upstream text")]).await;
        controller.check_mods(&Ok(failed.client())).await;
        assert!(controller.mod_check.lock().unwrap().targets.is_empty());
        assert!(
            controller.status().unwrap()["agent"]["mod_check"]["error"]
                .as_str()
                .unwrap()
                .contains("403")
        );
        assert_eq!(controller.room.status().phase, Phase::Running);
        assert_eq!(*controller.exit.borrow(), None);
        controller.room.shutdown_priority().await.unwrap();
    }

    #[tokio::test]
    async fn workshop_network_waits_do_not_hold_room_mutation_ownership() {
        let (_directory, cluster, controller) = running_controller().await;
        loaded_mod_fixture(&cluster);
        let server = crate::steam::tests::Server::start(vec![Vec::new()]).await;
        let checking = controller.clone();
        let client = Ok(server.client());
        let task = tokio::spawn(async move { checking.check_mods(&client).await });
        wait_for(|| server.count() == 1).await;
        tokio::time::timeout(Duration::from_secs(5), controller.stop(None, None, None))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(controller.room.status().phase, Phase::Stopped);
        task.abort();
        let _ = task.await;
    }

    #[tokio::test]
    async fn disabling_automatic_maintenance_discards_a_pending_workshop_observation() {
        use crate::steam::tests::{Server, response};
        let (_directory, cluster, controller) = running_controller().await;
        let body = loaded_mod_fixture(&cluster);
        let hold = cluster.join("Master/hold-mods");
        fs::write(&hold, "").unwrap();
        let server = Server::start(vec![response(200, "", &body)]).await;
        let checking = controller.clone();
        let client = Ok(server.client());
        let task = tokio::spawn(async move { checking.check_mods(&client).await });
        wait_for(|| cluster.join("Master/mods-waiting").exists()).await;
        controller
            .room
            .with_lock(|lock| {
                Policy {
                    mod_auto_update: false,
                    ..Default::default()
                }
                .save(lock)
            })
            .unwrap();
        fs::remove_file(hold).unwrap();
        task.await.unwrap();
        assert!(controller.mod_check.lock().unwrap().checked_at.is_none());
        controller.room.shutdown_priority().await.unwrap();
    }

    fn target(version: &str) -> crate::mods::UpdateTargets {
        [(
            2189004162,
            crate::mods::UpdateTarget {
                version: version.into(),
                shards: ["Master".into(), "Caves".into()].into(),
            },
        )]
        .into()
    }

    #[tokio::test]
    async fn unchanged_downloads_exhaust_one_persistent_budget_without_stopping_on_uncertainty() {
        use crate::steam::tests::{Server, response};
        let (_directory, cluster, controller) = running_controller().await;
        let body = loaded_mod_fixture(&cluster);
        let server = Server::start(vec![response(200, "", &body); 4]).await;
        let client = Ok(server.client());
        controller.check_mods(&client).await;
        let targets = controller.mod_check.lock().unwrap().targets.clone();
        controller
            .room
            .with_lock(|lock| {
                let mut state = PolicyState::load(lock)?;
                state.mods.enter_pending();
                state.mods.targets = targets;
                state.save(lock)
            })
            .unwrap();
        for attempt in 1..=3 {
            controller.stop(None, None, None).await.unwrap();
            let action = controller
                .room
                .with_lock(|lock| {
                    let mut state = PolicyState::load(lock)?;
                    let plan = Policy::load(lock)?.plan(
                        &mut state,
                        Utc::now() + chrono::TimeDelta::seconds(301),
                        &policy::Inputs::default(),
                    )?;
                    state.save(lock)?;
                    Ok(plan.action.unwrap())
                })
                .unwrap();
            assert_eq!(action, policy::Action::UpdateMods { attempt });
            controller
                .action(action, controller.interrupted.subscribe())
                .await
                .unwrap();
            assert_eq!(
                controller.policy_state().unwrap().mods.phase,
                ModPhase::Verifying
            );
            controller.initialize().unwrap();
            assert_eq!(controller.policy_state().unwrap().mods.attempts, attempt);
            controller.start(None).await.unwrap();
            controller
                .check_mods(&Err(crate::steam::Error::Transport(
                    crate::steam::TransportError::Connection,
                )))
                .await;
            assert_eq!(
                controller.policy_state().unwrap().mods.phase,
                ModPhase::Verifying
            );
            assert_eq!(controller.room.status().phase, Phase::Running);
            assert_eq!(controller.policy_state().unwrap().mods.attempts, attempt);
            controller.check_mods(&client).await;
            assert_eq!(controller.policy_state().unwrap().mods.attempts, attempt);
        }
        assert_eq!(
            controller.policy_state().unwrap().mods.phase,
            ModPhase::Failed
        );
        assert_eq!(
            fs::read_to_string(cluster.join("mods/downloads")).unwrap(),
            "download\ndownload\ndownload\n"
        );
        controller.tick().await.unwrap();
        wait_for(|| {
            controller.room.status().phase == Phase::Stopped
                && controller.mutation.available_permits() == 1
        })
        .await;
        let pids: Vec<_> = controller
            .room
            .driver_states()
            .into_values()
            .map(|shard| shard.pid)
            .collect();
        let rejected = controller
            .invoke(Envelope::new(Target::Room, Request::Restart { notice: None }).unwrap())
            .await
            .unwrap_err();
        assert_eq!(rejected.code, ErrorCode::NotReady);
        assert_eq!(
            controller
                .room
                .driver_states()
                .into_values()
                .map(|shard| shard.pid)
                .collect::<Vec<_>>(),
            pids
        );
        let queued = controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::UpdateMods {
                        restart: true,
                        notice: None,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(queued["queued"], true);
        assert_eq!(controller.policy_state().unwrap().mods.attempts, 0);
        controller.room.shutdown_priority().await.unwrap();
    }

    #[tokio::test]
    async fn verification_keeps_missing_participants_and_finishes_locally_without_steam() {
        use crate::steam::tests::{Server, response};
        let (_directory, cluster, controller) = running_controller().await;
        let body = loaded_mod_fixture(&cluster);
        controller
            .room
            .with_lock(|lock| {
                let mut state = PolicyState::load(lock)?;
                state.mods.enter_pending();
                state.mods.phase = ModPhase::Verifying;
                state.mods.attempts = 2;
                state.mods.targets = target("2");
                state.save(lock)
            })
            .unwrap();
        fs::write(
            cluster.join("Master/enabled-mods.json"),
            r#"[{"id":"workshop-2189004162","version":"2"}]"#,
        )
        .unwrap();
        fs::write(cluster.join("Caves/enabled-mods.json"), "[]").unwrap();
        let server = Server::start(vec![response(200, "", &body)]).await;
        controller.check_mods(&Ok(server.client())).await;
        let state = controller.policy_state().unwrap();
        assert_eq!(state.mods.phase, ModPhase::Verifying);
        assert_eq!(state.mods.targets, target("2"));
        assert_eq!(state.mods.attempts, 2);
        assert!(state.mods.error.is_some());
        controller.initialize().unwrap();
        assert_eq!(controller.policy_state().unwrap().mods.targets, target("2"));
        fs::write(
            cluster.join("Caves/enabled-mods.json"),
            r#"[{"id":"workshop-2189004162","version":"2"}]"#,
        )
        .unwrap();
        controller
            .check_mods(&Err(crate::steam::Error::Transport(
                crate::steam::TransportError::Connection,
            )))
            .await;
        assert_eq!(controller.policy_state().unwrap().mods, Default::default());
        controller.room.shutdown_priority().await.unwrap();
    }

    #[tokio::test]
    async fn a_fresh_closed_agent_can_download_without_repeating_forbidden_starts() {
        let (directory, cluster, previous) = running_controller().await;
        loaded_mod_fixture(&cluster);
        previous.stop(None, None, None).await.unwrap();
        previous
            .room
            .with_lock(|lock| {
                let mut state = PolicyState::load(lock)?;
                state.mods.enter_pending();
                state.mods.phase = ModPhase::Verifying;
                state.mods.attempts = 2;
                state.mods.targets = target("2");
                state.save(lock)?;
                recovery::on_failure(
                    lock,
                    Failure::Unknown,
                    &Default::default(),
                    &Default::default(),
                    0,
                )?;
                Ok(())
            })
            .unwrap();
        previous.room.close_telemetry().await.unwrap();
        drop(previous);
        wait_for(|| crate::files::RoomLock::try_acquire(&cluster).is_ok()).await;
        let room = Room::open(
            &cluster,
            directory.path().join("install/bin64/game.py"),
            DriverOptions::default(),
            EventHub::default(),
            Default::default(),
        )
        .unwrap();
        let controller = test_controller(room);
        assert!(!controller.room.requires_container_restart());
        assert!(controller.room.driver_states().is_empty());
        controller.initialize().unwrap();
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::UpdateMods {
                        restart: true,
                        notice: None,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        controller.tick().await.unwrap();
        wait_for(|| {
            controller.mutation.available_permits() == 1
                && controller.policy_state().unwrap().mods.phase == ModPhase::Verifying
        })
        .await;
        let maintenance = controller.policy_state().unwrap().mods;
        assert!(maintenance.manual);
        assert_eq!(maintenance.attempts, 1);
        assert_eq!(maintenance.targets, target("2"));
        assert_eq!(
            fs::read_to_string(cluster.join("mods/downloads")).unwrap(),
            "download\n"
        );
        let mut events = controller
            .room
            .local_log()
            .events
            .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
        let control = cluster.join(crate::files::STATE_FILE);
        let saved = fs::File::open(&control).unwrap();
        for _ in 0..3 {
            controller.tick().await.unwrap();
            wait_for(|| controller.mutation.available_permits() == 1).await;
            assert!(
                !events
                    .drain()
                    .iter()
                    .any(|record| record["event_name"] == "dst.server.operation"
                        && record["body"]["method"] == "start")
            );
            assert_eq!(controller.policy_state().unwrap().mods, maintenance);
            assert_eq!(
                fs::metadata(&control).unwrap().ino(),
                saved.metadata().unwrap().ino()
            );
        }
        assert!(controller.recovery().unwrap().closed.is_some());
        controller
            .invoke(Envelope::new(Target::Room, Request::Start {}).unwrap())
            .await
            .unwrap();
        controller.tick().await.unwrap();
        wait_for(|| {
            controller.mutation.available_permits() == 1
                && controller.room.status().phase == Phase::Running
        })
        .await;
        assert!(controller.recovery().unwrap().closed.is_none());
        controller.room.shutdown_priority().await.unwrap();
    }

    #[tokio::test]
    async fn partial_verification_preserves_all_targets_across_downloads_and_publications() {
        use crate::steam::tests::{Server, response};
        for first_version in ["2", "3"] {
            let (_directory, cluster, controller) = running_controller().await;
            let first = 2189004162;
            let second = first + 1;
            let mut body: Value = serde_json::from_slice(&loaded_mod_fixture(&cluster)).unwrap();
            body["response"]["publishedfiledetails"][0]["tags"] =
                json!([{"tag":format!("version:{first_version}")}]);
            let mut other = body["response"]["publishedfiledetails"][0].clone();
            other["publishedfileid"] = json!(second.to_string());
            other["tags"] = json!([{"tag":"version:2"}]);
            body["response"]["publishedfiledetails"]
                .as_array_mut()
                .unwrap()
                .push(other);
            body["response"]["resultcount"] = json!(2);
            let server = Server::start(vec![
                response(200, "", &serde_json::to_vec(&body).unwrap());
                2
            ])
            .await;
            let client = Ok(server.client());
            controller
                .update_policy("test_targets", None, |_, state| {
                    state.mods.targets = target("2");
                    state
                        .mods
                        .targets
                        .insert(second, state.mods.targets[&first].clone());
                    state.queue_mod_update(true)
                })
                .unwrap();
            let origin = controller.policy_state().unwrap().mods.origin;
            for attempt in 1..=2 {
                controller.stop(None, None, None).await.unwrap();
                let action = controller
                    .update_policy("test_plan", None, |lock, state| {
                        Ok(Policy::load(lock)?
                            .plan(
                                state,
                                Utc::now() + chrono::TimeDelta::seconds(301),
                                &policy::Inputs::default(),
                            )?
                            .action)
                    })
                    .unwrap()
                    .unwrap();
                assert_eq!(action, policy::Action::UpdateMods { attempt });
                controller
                    .action(action, controller.interrupted.subscribe())
                    .await
                    .unwrap();
                for shard in ["Master", "Caves"] {
                    let mut loaded = vec![
                        json!({"id":format!("workshop-{second}"),"version":if attempt == 1 {"1"} else {"2"}}),
                    ];
                    if attempt == 1 || shard == "Master" {
                        loaded.push(
                            json!({"id":format!("workshop-{first}"),"version":first_version}),
                        );
                    }
                    write_marker(
                        &cluster.join(shard).join("enabled-mods.json"),
                        &serde_json::to_string(&loaded).unwrap(),
                    );
                }
                controller.start(None).await.unwrap();
                controller.check_mods(&client).await;
                let persisted = controller.policy_state().unwrap().mods;
                assert_eq!(persisted.attempts, attempt);
                assert_eq!(persisted.origin, origin);
                assert!(persisted.manual);
                assert_eq!(persisted.targets.len(), 2);
                assert_eq!(persisted.targets[&first].version, first_version);
                assert_eq!(
                    persisted.targets[&first].shards,
                    ["Master".into(), "Caves".into()].into()
                );
                if attempt == 1 {
                    assert!(matches!(persisted.phase, ModPhase::Retry { .. }));
                } else {
                    assert_eq!(persisted.phase, ModPhase::Verifying);
                    assert!(persisted.error.is_some());
                    controller.tick().await.unwrap();
                    assert_eq!(controller.room.status().phase, Phase::Running);
                }
                controller.initialize().unwrap();
                assert_eq!(controller.policy_state().unwrap().mods, persisted);
            }
            write_marker(
                &cluster.join("Caves/enabled-mods.json"),
                &json!([
                    {"id":format!("workshop-{first}"),"version":first_version},
                    {"id":format!("workshop-{second}"),"version":"2"}
                ])
                .to_string(),
            );
            controller
                .check_mods(&Err(crate::steam::Error::Transport(
                    crate::steam::TransportError::Connection,
                )))
                .await;
            assert_eq!(controller.policy_state().unwrap().mods, Default::default());
            assert_eq!(
                fs::read_to_string(cluster.join("mods/downloads")).unwrap(),
                "download\ndownload\n"
            );
            assert_eq!(server.count(), 2);
            controller.room.shutdown_priority().await.unwrap();
        }
    }

    #[tokio::test]
    async fn direct_room_configuration_cancels_targets_and_partial_agent_errors_clear_observations()
    {
        for partial in [false, true] {
            let (_directory, cluster, controller) = running_controller().await;
            loaded_mod_fixture(&cluster);
            controller.stop(None, None, None).await.unwrap();
            for shard in ["Master", "Caves"] {
                write_marker(
                    &cluster.join(shard).join("modoverrides.lua"),
                    "return {['workshop-2189004162']={enabled=true}}\n",
                );
            }
            controller
                .update_policy("test_targets", None, |_, state| {
                    state.mods.enter_pending();
                    state.mods.phase = ModPhase::Verifying;
                    state.mods.attempts = 1;
                    state.mods.targets = target("2");
                    Ok(())
                })
                .unwrap();
            let mut configuration = controller
                .room
                .with_lock(|lock| Ok(crate::settings::ClusterConfig::load(lock)?.into_value()))
                .unwrap();
            configuration["shards"]["Caves"]["mods"] = json!({});
            configuration["shards"]["Master"]["mods"] = json!({});
            let request = Envelope::new(
                Target::Room,
                Request::Configure {
                    configuration,
                    replace_permissions: false,
                },
            )
            .unwrap();
            if partial {
                controller.mod_check.lock().unwrap().targets = target("2");
                *controller.pending_start.lock().unwrap() =
                    Some(OperationContext::new(Source::Rpc, Layer::Policy, None));
                let mut events = controller
                    .room
                    .local_log()
                    .events
                    .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
                fs::create_dir(cluster.join("Master/.dst-config-write.tmp")).unwrap();
                let error = controller.invoke(request).await.unwrap_err();
                assert!(error.to_string().contains("Master/modoverrides.lua"));
                assert!(controller.pending_start.lock().unwrap().is_none());
                assert!(controller.mod_check.lock().unwrap().targets.is_empty());
                assert_eq!(
                    controller.policy_state().unwrap().mods.targets[&2189004162].shards,
                    ["Master".into()].into()
                );
                let records = events.drain();
                let accepted = records
                    .iter()
                    .find(|r| {
                        r["event_name"] == "dst.server.operation"
                            && r["body"]["method"] == "configure"
                            && r["body"]["phase"] == "accepted"
                    })
                    .unwrap();
                assert!(
                    records
                        .iter()
                        .any(|r| r["body"]["reason"] == "configuration_changed"
                            && r["attributes"]["dst.operation.id"]
                                == accepted["attributes"]["dst.operation.id"])
                );
                fs::remove_dir(cluster.join("Master/.dst-config-write.tmp")).unwrap();
            } else {
                controller
                    .room
                    .invoke_until_complete(request)
                    .await
                    .unwrap();
                assert_eq!(controller.policy_state().unwrap().mods, Default::default());
            }
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn manual_retry_keeps_known_targets_and_honors_restart() {
        use crate::steam::tests::{Server, response};
        for restart in [false, true] {
            let (_directory, cluster, controller) = running_controller().await;
            let workshop = loaded_mod_fixture(&cluster);
            controller
                .room
                .with_lock(|lock| {
                    let mut state = PolicyState::load(lock)?;
                    state.mods.enter_pending();
                    state.mods.phase = ModPhase::Verifying;
                    state.mods.attempts = 2;
                    state.mods.targets = target("2");
                    state.save(lock)
                })
                .unwrap();
            controller
                .manual_mod_update(restart, None, None, None)
                .await
                .unwrap();
            let action = controller
                .room
                .with_lock(|lock| {
                    let mut state = PolicyState::load(lock)?;
                    assert_eq!(state.mods.attempts, 0);
                    assert_eq!(state.mods.targets, target("2"));
                    let plan = Policy::load(lock)?.plan(
                        &mut state,
                        Utc::now(),
                        &policy::Inputs::default(),
                    )?;
                    state.save(lock)?;
                    Ok(plan.action.unwrap())
                })
                .unwrap();
            controller
                .action(action, controller.interrupted.subscribe())
                .await
                .unwrap();
            assert_eq!(
                controller.policy_state().unwrap().mods.phase,
                ModPhase::Verifying
            );
            assert_eq!(controller.policy_state().unwrap().mods.attempts, 1);
            assert_eq!(
                *controller.exit.borrow(),
                if restart { None } else { Some(0) }
            );
            assert_eq!(controller.room.status().phase, Phase::Stopped);
            let controller = if restart {
                controller
            } else {
                let reopened = test_controller(controller.room.clone());
                reopened.initialize().unwrap();
                reopened
            };
            controller.start(None).await.unwrap();
            controller
                .check_mods(&Err(crate::steam::Error::Transport(
                    crate::steam::TransportError::Connection,
                )))
                .await;
            assert_eq!(controller.room.status().phase, Phase::Running);
            assert_eq!(
                controller.policy_state().unwrap().mods.phase,
                ModPhase::Verifying
            );

            let server = Server::start(vec![response(200, "", &workshop)]).await;
            controller.check_mods(&Ok(server.client())).await;
            controller.stop(None, None, None).await.unwrap();
            let retry = controller
                .update_policy("test_plan", None, |lock, state| {
                    let ModPhase::Retry { not_before } = state.mods.phase else {
                        panic!("the loaded version still differs");
                    };
                    Ok(Policy::load(lock)?
                        .plan(state, not_before, &policy::Inputs::default())?
                        .action
                        .unwrap())
                })
                .unwrap();
            assert_eq!(retry, policy::Action::UpdateMods { attempt: 2 });
            controller
                .action(retry, controller.interrupted.subscribe())
                .await
                .unwrap();
            assert_eq!(*controller.exit.borrow(), None);
            controller.room.shutdown_priority().await.unwrap();
        }
    }

    #[tokio::test]
    async fn maintenance_origin_survives_restart_and_download_failure_then_links_manual_retry() {
        let (directory, cluster, controller) = running_controller().await;
        loaded_mod_fixture(&cluster);
        controller.stop(None, None, None).await.unwrap();
        controller
            .room
            .with_lock(|lock| {
                let mut state = PolicyState::load(lock)?;
                state.mods.enter_pending();
                state.mods.phase = ModPhase::Verifying;
                state.mods.attempts = 2;
                state.mods.targets = target("2");
                state.save(lock)
            })
            .unwrap();
        let mut events = controller
            .room
            .local_log()
            .events
            .subscribe_for_test(crate::rpc::StreamKind::Lifecycle);
        let automatic = controller.policy_state().unwrap().mods.origin.unwrap();
        assert_eq!(automatic.source, policy::ModSource::ModAuto);
        controller.initialize().unwrap();
        assert_eq!(
            controller.policy_state().unwrap().mods.origin.as_ref(),
            Some(&automatic)
        );
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::UpdateMods {
                        restart: false,
                        notice: None,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        let origin = controller.policy_state().unwrap().mods.origin.unwrap();
        assert_ne!(origin, automatic);
        assert_eq!(origin.source, policy::ModSource::Rpc);
        let records = events.drain();
        let queued = records
            .iter()
            .find(|record| {
                record["event_name"] == "dst.mod.maintenance"
                    && record["body"]["reason"] == "manual_request"
            })
            .unwrap();
        assert_eq!(
            queued["body"]["previous_origin"]["operation_id"],
            automatic.operation_id
        );
        assert_eq!(
            queued["attributes"]["dst.operation.id"],
            origin.operation_id
        );
        let action = controller
            .update_policy("test_plan", None, |lock, state| {
                Ok(Policy::load(lock)?
                    .plan(state, Utc::now(), &policy::Inputs::default())?
                    .action
                    .unwrap())
            })
            .unwrap();
        fs::write(
            directory.path().join("install/bin64/game.py"),
            "#!/usr/bin/env python3\nraise SystemExit(2)\n",
        )
        .unwrap();
        let error = controller
            .action(action, controller.interrupted.subscribe())
            .await
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Internal);
        let state = controller.policy_state().unwrap();
        assert!(matches!(state.mods.phase, ModPhase::Retry { .. }));
        assert_eq!(state.mods.origin.as_ref(), Some(&origin));
        assert_eq!(state.mods.attempts, 1);
        assert_eq!(state.mods.targets, target("2"));
        assert_eq!(*controller.exit.borrow(), None);
        controller.initialize().unwrap();
        assert_eq!(
            controller.policy_state().unwrap().mods.origin.as_ref(),
            Some(&origin)
        );
        let records = events.drain();
        assert!(
            records
                .iter()
                .any(|record| record["body"]["phase"] == "finished"
                    && record["body"]["outcome"] == "failure"
                    && record["attributes"]["dst.operation.parent_id"] == origin.operation_id)
        );
        controller
            .update_policy("configuration", None, |_, state| {
                state.mods = Default::default();
                Ok(())
            })
            .unwrap();
        let records = events.drain();
        assert!(
            records
                .iter()
                .any(|record| record["body"]["reason"] == "cancelled"
                    && record["body"]["origin"]["operation_id"] == origin.operation_id)
        );
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn only_explicit_configuration_removal_cancels_expected_participants() {
        let (_directory, cluster, controller) = running_controller().await;
        loaded_mod_fixture(&cluster);
        controller
            .room
            .with_lock(|lock| {
                let mut state = PolicyState::load(lock)?;
                state.mods.enter_pending();
                state.mods.phase = ModPhase::Verifying;
                state.mods.attempts = 1;
                state.mods.targets = target("2");
                state.save(lock)
            })
            .unwrap();
        controller.stop(None, None, None).await.unwrap();
        // A loaded Mod can come from dynamic Lua or historical configuration.
        fs::write(cluster.join("mods/modsettings.lua"), "").unwrap();
        for shard in ["Master", "Caves"] {
            fs::write(cluster.join(shard).join("modoverrides.lua"), "return {}\n").unwrap();
        }
        controller.initialize().unwrap();
        assert_eq!(controller.policy_state().unwrap().mods.targets, target("2"));
        let mut configuration = controller
            .room
            .with_lock(|lock| Ok(crate::settings::ClusterConfig::load(lock)?.into_value()))
            .unwrap();
        configuration["settings"]["cluster_name"] = json!("unrelated change");
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::Configure {
                        configuration: configuration.clone(),
                        replace_permissions: false,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(controller.policy_state().unwrap().mods.targets, target("2"));
        for shard in ["Master", "Caves"] {
            configuration["shards"][shard]["mods"] =
                json!({"entries":{"workshop-2189004162":{"enabled":true}}});
        }
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::Configure {
                        configuration: configuration.clone(),
                        replace_permissions: false,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        configuration["shards"]["Caves"]["mods"] = json!({});
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::Configure {
                        configuration: configuration.clone(),
                        replace_permissions: false,
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            controller.policy_state().unwrap().mods.targets[&2189004162].shards,
            ["Master".into()].into()
        );
        controller
            .invoke(
                Envelope::new(
                    Target::Room,
                    Request::EditConfiguration {
                        changes: vec![("/cluster/shards/Master/mods".into(), json!({}))],
                        unset: vec![],
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        assert!(controller.policy_state().unwrap().mods.targets.is_empty());
        controller.room.shutdown_priority().await.unwrap();
    }

    #[tokio::test]
    async fn policy_changes_cancel_automatic_retries_and_preserve_manual_retries() {
        for (manual, field_edit) in [(false, false), (false, true), (true, false), (true, true)] {
            let (_directory, _cluster, controller) = running_controller().await;
            controller
                .room
                .with_lock(|lock| {
                    let mut state = PolicyState::load(lock)?;
                    if manual {
                        state.queue_mod_update(true)?;
                    } else {
                        state.mods.enter_pending();
                    }
                    state.mods.phase = ModPhase::Retry {
                        not_before: Utc::now() + chrono::TimeDelta::minutes(5),
                    };
                    state.mods.attempts = 1;
                    state.mods.targets = target("2");
                    state.save(lock)
                })
                .unwrap();
            let request = if field_edit {
                let rejected = controller
                    .invoke(
                        Envelope::new(
                            Target::Room,
                            Request::EditConfiguration {
                                changes: vec![(
                                    "/cluster/settings/cluster_name".into(),
                                    json!("blocked"),
                                )],
                                unset: vec![],
                            },
                        )
                        .unwrap(),
                    )
                    .await
                    .unwrap_err();
                assert_eq!(rejected.code, ErrorCode::Busy);
                Request::EditConfiguration {
                    changes: vec![("/policy/mod_auto_update".into(), json!(false))],
                    unset: vec![],
                }
            } else {
                Request::SetPolicy {
                    policy: json!({"mod_auto_update":false}),
                }
            };
            controller
                .invoke(Envelope::new(Target::Room, request).unwrap())
                .await
                .unwrap();
            controller.tick().await.unwrap();
            let state = controller.policy_state().unwrap();
            if manual {
                assert!(matches!(state.mods.phase, ModPhase::Retry { .. }));
                assert_eq!(state.mods.targets, target("2"));
                assert_eq!(state.mods.attempts, 1);
            } else {
                assert_eq!(state.mods, Default::default());
            }
            assert_eq!(controller.room.status().phase, Phase::Running);
            controller.room.shutdown_priority().await.unwrap();
        }
    }

    async fn enable_idle_regeneration(controller: &Controller) {
        let inputs = controller
            .inputs(&controller.room.status(), false)
            .await
            .unwrap();
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
                    sessions: inputs
                        .presence
                        .iter()
                        .map(|(name, presence)| (name.clone(), presence.session_id.clone()))
                        .collect(),
                    observations: inputs
                        .presence
                        .iter()
                        .map(|(name, presence)| (name.clone(), presence.observation.clone()))
                        .collect(),
                    observed_seconds: inputs
                        .presence
                        .iter()
                        .map(|(name, presence)| (name.clone(), presence.observed_seconds))
                        .collect(),
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

    fn write_marker(path: &std::path::Path, contents: &str) {
        let temporary = path.with_extension("new");
        fs::write(&temporary, contents).unwrap();
        fs::rename(temporary, path).unwrap();
    }

    #[tokio::test]
    async fn policy_queries_allow_mutations_and_discard_the_earlier_idle_observation() {
        let (_directory, cluster, controller) = running_controller().await;
        enable_idle_regeneration(&controller).await;
        let hold = cluster.join("Caves/hold-presence");
        fs::write(&hold, "").unwrap();
        let tick = {
            let controller = controller.clone();
            tokio::spawn(async move { controller.tick().await })
        };
        wait_for(|| cluster.join("Caves/presence-waiting").exists()).await;
        // Master reported an idle room. It can still accept writes while Caves is answering.
        controller
            .invoke(
                Envelope::new(
                    Target::Shard("Master".into()),
                    Request::ExecuteJson {
                        source: "return true".into(),
                    },
                )
                .unwrap(),
            )
            .await
            .unwrap();
        let mut changed = Box::pin(controller.changed.notified());
        assert!(futures::poll!(&mut changed).is_pending());
        drop(changed);
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
        enable_idle_regeneration(&controller).await;
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
        async fn advance(duration: Duration) {
            tokio::time::pause();
            tokio::time::advance(duration).await;
            tokio::time::resume();
        }

        let (_directory, cluster, controller) = running_controller().await;
        controller
            .room
            .with_lock(|lock| {
                lock.update_state(|document| {
                    document.get_mut("recovery").unwrap()["restarts_used"] = json!(1);
                    Ok(())
                })
            })
            .unwrap();
        enable_idle_regeneration(&controller).await;
        advance(recovery::STABLE_RESET - Duration::from_secs(60)).await;
        let hold = cluster.join("Master/hold-presence");
        fs::write(&hold, "").unwrap();
        let tick = {
            let controller = controller.clone();
            tokio::spawn(async move { controller.tick().await })
        };
        wait_for(|| cluster.join("Master/presence-waiting").exists()).await;
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
        advance(Duration::from_secs(61)).await;
        controller.tick().await.unwrap();
        assert_eq!(controller.recovery().unwrap().restarts_used, 1);
        advance(recovery::STABLE_RESET).await;
        controller.tick().await.unwrap();
        assert_eq!(controller.recovery().unwrap().restarts_used, 0);
        controller.room.shutdown().await.unwrap();
    }

    fn one_hour_window(starts_in_minutes: i64) -> Policy {
        let start = Utc::now() + chrono::TimeDelta::minutes(starts_in_minutes);
        let end = start + chrono::TimeDelta::hours(1);
        serde_json::from_value(json!({
            "timezone":"UTC", "mod_auto_update":false,
            "schedule":[{"start":start.format("%H:%M").to_string(),"end":end.format("%H:%M").to_string()}],
        })).unwrap()
    }

    #[tokio::test]
    async fn scheduled_closing_finishes_an_accepted_stop_or_restart() {
        for restart in [false, true] {
            let (_directory, cluster, controller) = running_controller().await;
            let notice = Some(model::Countdown {
                template: "Closing in {remaining}".into(),
                delay: 120.0,
                interval: 30.0,
                parameters: Default::default(),
            });
            let request = if restart {
                Request::Restart { notice }
            } else {
                Request::Stop { notice }
            };
            let worker = {
                let controller = controller.clone();
                tokio::spawn(async move {
                    controller
                        .invoke(Envelope::new(Target::Room, request).unwrap())
                        .await
                })
            };
            wait_for(|| cluster.join("announcements").exists()).await;
            controller
                .room
                .with_lock(|lock| one_hour_window(180).save(lock))
                .unwrap();
            controller.tick().await.unwrap();
            let result = tokio::time::timeout(Duration::from_secs(5), worker)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            if restart {
                assert_eq!(result["restart"], false);
            }
            assert_eq!(*controller.exit.borrow(), (!restart).then_some(0));
            assert_eq!(controller.room.status().phase, Phase::Stopped);
            for name in ["Master", "Caves"] {
                assert_eq!(
                    fs::read_to_string(cluster.join(name).join("native-lifecycle")).unwrap(),
                    "start\nshutdown\nshutdown_save\n"
                );
            }
            controller.room.close_telemetry().await.unwrap();
        }
    }

    #[tokio::test]
    async fn failed_scheduled_closing_waits_for_the_next_tick() {
        let (_directory, _cluster, controller) = running_controller().await;
        controller
            .room
            .with_lock(|lock| one_hour_window(180).save(lock))
            .unwrap();
        let held = controller.mutation.clone().acquire_owned().await.unwrap();
        controller.tick().await.unwrap();
        assert!(controller.closing_game.load(Ordering::Acquire));
        controller
            .interrupted
            .send_modify(|revision| *revision += 1);
        drop(held);
        wait_for(|| !controller.closing_game.load(Ordering::Acquire)).await;
        assert_eq!(controller.room.status().phase, Phase::Running);
        assert_eq!(
            controller.error.lock().unwrap().as_deref(),
            Some("scheduled operation was interrupted")
        );
        let mut changed = Box::pin(controller.changed.notified());
        assert!(futures::poll!(&mut changed).is_pending());
        drop(changed);
        controller.tick().await.unwrap();
        wait_for(|| controller.room.status().phase == Phase::Stopped).await;
        controller.room.close_telemetry().await.unwrap();
    }

    #[tokio::test]
    async fn scheduled_closing_interrupts_a_long_announcement_and_rejects_a_restart() {
        let (_directory, cluster, controller) = running_controller().await;
        let room = &controller.room;
        let identities: Vec<_> = room
            .driver_states()
            .into_values()
            .map(|state| (state.pid, state.nonce))
            .collect();
        room.with_lock(|lock| one_hour_window(180).save(lock))
            .unwrap();

        let announcing = controller.clone();
        let prior_operation = tokio::spawn(async move {
            announcing
                .invoke(
                    Envelope::new(
                        Target::Room,
                        Request::Announce {
                            message: "still playing".into(),
                            count: 2,
                            interval: 90.0,
                        },
                    )
                    .unwrap(),
                )
                .await
        });
        wait_for(|| cluster.join("announcements").exists()).await;
        controller.tick().await.unwrap();
        assert!(controller.closing_game.load(Ordering::Acquire));
        let rejected = controller
            .invoke(Envelope::new(Target::Room, Request::Restart { notice: None }).unwrap())
            .await
            .unwrap_err();
        assert_eq!(rejected.code, ErrorCode::Busy);
        assert_eq!(
            prior_operation.await.unwrap().unwrap_err().code,
            ErrorCode::Unknown
        );
        wait_for(|| {
            room.status().phase == Phase::Stopped && controller.mutation.available_permits() == 1
        })
        .await;
        assert_eq!(*controller.exit.borrow(), None);
        let stopped: Vec<_> = room.driver_states().into_values().collect();
        assert_eq!(
            stopped
                .iter()
                .map(|state| (state.pid, state.nonce.clone()))
                .collect::<Vec<_>>(),
            identities
        );
        assert!(
            stopped.iter().all(|state| !state.running
                && state.exit.as_ref().is_some_and(|exit| exit.output_drained))
        );
        assert!(!controller.run_state().unwrap().active);
        for shard in ["Master", "Caves"] {
            assert_eq!(
                fs::read_to_string(cluster.join(shard).join("native-lifecycle")).unwrap(),
                "start\nshutdown\nshutdown_save\n"
            );
        }
        // A canceled policy wait can leave Updating persisted after the room's
        // retained worker has reaped its updater; closing must finish that attempt.
        room.with_lock(|lock| {
            let mut state = PolicyState::load(lock)?;
            state.mods.enter_pending();
            state.mods.phase = ModPhase::Updating;
            state.mods.attempts = 1;
            state.save(lock)
        })
        .unwrap();
        controller.record_stop().unwrap();
        let maintenance = controller.policy_state().unwrap().mods;
        assert!(matches!(maintenance.phase, ModPhase::Retry { .. }));
        assert_eq!(maintenance.attempts, 1);
        room.with_lock(|lock| one_hour_window(-30).save(lock))
            .unwrap();
        controller.tick().await.unwrap();
        wait_for(|| {
            room.status().phase == Phase::Running && controller.mutation.available_permits() == 1
        })
        .await;
        assert_eq!(*controller.exit.borrow(), None);
        for (old, new) in stopped.iter().zip(room.driver_states().into_values()) {
            assert!(new.ready && new.running);
            assert_ne!(old.nonce, new.nonce);
        }
        room.shutdown_priority().await.unwrap();
        room.close_telemetry().await.unwrap();
    }
}
