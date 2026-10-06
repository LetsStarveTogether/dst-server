//! Observe the service and room once per poll. Only public results need the combined JSON view.
use super::*;
use crate::{
    model::Phase,
    status::{ControlStatus, GameStatus},
};

pub(super) struct HostStatus {
    number: u16,
    service: UnitStatus,
    saved: Option<ControlStatus>,
    game: Option<GameStatus>,
    configuration_error: Option<String>,
    error: Option<String>,
}

impl Host {
    pub async fn status(&self, number: u16, game: bool) -> Result<Value> {
        let mut status = self.observe(number, game).await?;
        if status.service.running()
            && let Err(error) = self
                .managed_unit(number)
                .and_then(|unit| unit.check_native_ports())
            && error.is::<crate::deployment::PublishedPortMismatch>()
        {
            status.configuration_error = Some(error.to_string());
        }
        Ok(status.report())
    }

    pub(super) async fn observe(&self, number: u16, game: bool) -> Result<HostStatus> {
        let directory = self.rooms.path(number)?;
        let unit = Self::unit(number);
        let mut status = HostStatus {
            number,
            service: self.systemd.status(&unit).await?,
            saved: None,
            game: None,
            configuration_error: None,
            error: None,
        };
        if !status.service.running() {
            let saved = (|| -> Result<ControlStatus> {
                let lock = RoomLock::try_acquire(&directory)?;
                let state = lock.read_state()?;
                if let Err(error) = self.rooms.load_from_state(number, &lock, &state) {
                    status.configuration_error = Some(
                        error
                            .downcast_ref::<crate::deployment::PublishedPortMismatch>()
                            .map_or_else(
                                || "room configuration could not be loaded".into(),
                                ToString::to_string,
                            ),
                    );
                }
                ControlStatus::from_document(state)
            })();
            match saved {
                Ok(control) => status.saved = Some(control),
                Err(_) => {
                    status.configuration_error =
                        Some("room control state could not be loaded".into())
                }
            }
            if status.configuration_error.is_some() {
                let current = self.systemd.status(&unit).await?;
                if current.running() {
                    status.service = current;
                    status.configuration_error = None;
                    status.saved = None;
                }
            }
        }
        if game && status.service.running() {
            match timeout(
                Duration::from_secs(5),
                self.call(number, Request::Status {}),
            )
            .await
            {
                Ok(Ok(value)) => match serde_json::from_value(value) {
                    Ok(game) => status.game = Some(game),
                    Err(error) => status.error = Some(format!("invalid Agent status: {error}")),
                },
                Ok(Err(error)) => status.error = Some(error.to_string()),
                Err(_) => status.error = Some("Agent status timed out".into()),
            }
        } else if let Some(control) = &status.saved {
            if let Some(reason) = control.recovery.closed {
                status.error = Some(format!(
                    "room recovery is exhausted or unsafe: {}",
                    json!(reason)
                ));
            } else if control.maintenance.phase == ModPhase::Failed {
                status.error = Some("Mod maintenance exhausted three attempts".into());
            }
        }
        if !directory.is_dir() {
            status.configuration_error = Some("room directory is missing".into());
        }
        Ok(status)
    }
}

impl HostStatus {
    fn control(&self) -> Option<&ControlStatus> {
        self.game
            .as_ref()
            .map(|game| &game.agent.control)
            .or(self.saved.as_ref())
    }

    pub(super) fn report(&self) -> Value {
        let control = self.control();
        json!({
            "number": self.number, "desired": control.map(|c| c.schedule.open),
            "running": self.service.running(), "load": self.service.load, "active": self.service.active,
            "sub": self.service.sub, "job_id": self.service.job_id, "units": {Host::unit(self.number): &self.service},
            "game": self.game, "policy": control.map(|c| &c.policy), "schedule": control.map(|c| &c.schedule),
            "maintenance": control.map(|c| &c.maintenance), "recovery": control.map(|c| &c.recovery),
            "error": self.error.as_ref().or(self.configuration_error.as_ref()), "configuration_error": self.configuration_error,
        })
    }

    pub(super) fn recovery_closed(&self) -> bool {
        self.control()
            .is_some_and(|control| control.recovery.closed.is_some())
    }

    fn check_failures(&self) -> Result<()> {
        if let Some(error) = &self.configuration_error {
            bail!("{error}");
        }
        ensure!(self.service.active != "failed", "room service failed");
        ensure!(
            self.control()
                .is_none_or(|c| c.maintenance.phase != ModPhase::Failed),
            "Mod maintenance exhausted three attempts"
        );
        Ok(())
    }

    pub(super) fn ready(&self) -> Result<bool> {
        self.check_failures()?;
        if let Some(reason) = self.control().and_then(|c| c.recovery.closed) {
            bail!("room recovery is exhausted or unsafe: {}", json!(reason));
        }
        ensure!(self.service.running(), "room Agent service is stopped");
        let Some(game) = &self.game else {
            return Ok(false);
        };
        ensure!(
            game.room.phase != Phase::Failed,
            "room games failed to start"
        );
        Ok(game.ready_for_schedule())
    }

    pub(super) fn completed_mod_update(&self, restart: bool) -> Result<Option<Value>> {
        self.check_failures()?;
        let Some(control) = self.control() else {
            return Ok(None);
        };
        let pending = match control.maintenance.phase {
            ModPhase::Idle => false,
            ModPhase::Verifying => true,
            _ => return Ok(None),
        };
        if restart && control.schedule.open {
            if let Some(reason) = control.recovery.closed {
                bail!(
                    "room recovery remains closed after Mod maintenance: {}",
                    json!(reason)
                );
            }
            if pending {
                return Ok(None);
            }
        }
        let restarted = if self.service.running() {
            if !restart
                || !self
                    .game
                    .as_ref()
                    .is_some_and(GameStatus::ready_for_schedule)
            {
                return Ok(None);
            }
            control.schedule.open
        } else {
            ensure!(!restart, "room Agent service stopped after Mod maintenance");
            false
        };
        Ok(Some(
            json!({"restarted":restarted,"verification_pending":pending}),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        model::{Readiness, RoomStatus, ShardStatus},
        recovery::ClosedReason,
        status::AgentStatus,
    };

    fn status(
        maintenance: ModPhase,
        open: bool,
        agent: bool,
        games: bool,
        closed: bool,
    ) -> HostStatus {
        let mut control = ControlStatus::from_document(Default::default()).unwrap();
        control.schedule.open = open;
        control.maintenance.phase = maintenance;
        control.recovery.closed = closed.then_some(ClosedReason::UnknownFailure);
        let phase = if games {
            Phase::Running
        } else {
            Phase::Stopped
        };
        let mut status = HostStatus {
            number: 1,
            service: UnitStatus {
                active: if agent { "active" } else { "inactive" }.into(),
                ..Default::default()
            },
            game: None,
            saved: None,
            configuration_error: None,
            error: None,
        };
        if agent {
            let shard = ShardStatus {
                name: "Master".into(),
                is_master: true,
                phase,
                pid: None,
                identity: None,
                readiness: Readiness {
                    process_running: games,
                    world_loaded: games,
                    control_ready: games,
                    ..Default::default()
                },
                error: None,
            };
            status.game = Some(GameStatus {
                room: RoomStatus {
                    phase,
                    master: shard.name.clone(),
                    shards: vec![shard],
                    operations: Default::default(),
                    error: None,
                },
                agent: AgentStatus {
                    control,
                    ready: true,
                    requires_container_restart: false,
                    error: None,
                    mod_check: Default::default(),
                    telemetry: Value::Null,
                },
            });
        } else {
            status.saved = Some(control);
        }
        status
    }

    #[test]
    fn mod_update_completion_requires_requested_game_state_and_verification() {
        use ModPhase::{Idle, Updating, Verifying};
        for (phase, restart, open, agent, games, expected) in [
            (Idle, false, true, false, false, Some((false, false))),
            (Verifying, false, true, false, false, Some((false, true))),
            (Idle, true, true, true, true, Some((true, false))),
            (Verifying, true, true, true, true, None),
            (Verifying, true, false, true, true, None),
            (Verifying, true, false, true, false, Some((false, true))),
            (Idle, true, false, true, false, Some((false, false))),
            (Verifying, false, false, true, false, None),
            (Updating, false, false, false, false, None),
        ] {
            let status = status(phase, open, agent, games, false);
            assert_eq!(status.completed_mod_update(restart).unwrap(), expected.map(|(restarted, pending)|
                json!({"restarted":restarted, "verification_pending":pending})
            ));
        }
    }

    #[test]
    fn completed_maintenance_reports_closed_recovery_before_waiting_for_verification() {
        for phase in [
            ModPhase::Idle,
            ModPhase::Verifying,
            ModPhase::Updating,
            ModPhase::Retry {
                not_before: chrono::Utc::now(),
            },
        ] {
            for restart in [false, true] {
                for open in [false, true] {
                    let status = status(phase.clone(), open, true, false, true);
                    let result = status.completed_mod_update(restart);
                    let closed =
                        matches!(phase, ModPhase::Idle | ModPhase::Verifying) && restart && open;
                    assert_eq!(
                        result.is_err(),
                        closed,
                        "{phase:?} restart={restart} open={open}"
                    );
                    if closed {
                        assert!(result.unwrap_err().to_string().contains("unknown_failure"));
                    }
                }
            }
        }
    }
}
