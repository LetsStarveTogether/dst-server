//! The room and its supervisor publish one status document to local clients.
use anyhow::Result;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::{
    model::{Phase, RoomStatus},
    mods::monitor::Check,
    policy::{ModMaintenance, Policy, PolicyState, ScheduleState},
    recovery::{self, RecoveryState},
};

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct ControlStatus {
    pub policy: Policy,
    pub schedule: ScheduleState,
    pub maintenance: ModMaintenance,
    pub recovery: RecoveryState,
}

impl ControlStatus {
    pub fn from_document(document: Map<String, Value>) -> Result<Self> {
        let policy = Policy::from_document(&document)?;
        Ok(Self {
            schedule: policy.schedule_at(Utc::now())?,
            policy,
            maintenance: PolicyState::from_document(&document)?.mods,
            recovery: recovery::decode(&document)?,
        })
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct GameStatus {
    #[serde(flatten)]
    pub room: RoomStatus,
    pub agent: AgentStatus,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct AgentStatus {
    #[serde(flatten)]
    pub control: ControlStatus,
    pub ready: bool,
    pub requires_container_restart: bool,
    pub error: Option<String>,
    pub mod_check: Check,
    pub telemetry: Value,
}

impl GameStatus {
    pub fn ready_for_schedule(&self) -> bool {
        let shards = &self.room.shards;
        self.agent.ready
            && !self.agent.requires_container_restart
            && !shards.is_empty()
            && if self.agent.control.schedule.open {
                self.room.phase == Phase::Running && shards.iter().all(|s| s.readiness.ready())
            } else {
                self.room.phase == Phase::Stopped
                    && shards.iter().all(|s| !s.readiness.process_running)
            }
    }
}
