//! Low-frequency observer health shared by the Room publisher and journal reader.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::driver::TelemetryProfile;

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum TelemetryStatus {
    Disabled,
    Active,
    Degraded,
    Failed,
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Capability {
    Disabled,
    Active,
    Unsupported,
    Failed,
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Fault {
    CallbackFailed,
    EncodingFailed,
    EventTooLarge,
    InstallationFailed,
    QueueFull,
    ScheduleFailed,
    WriteFailed,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LastError {
    stage: String,
    message: Fault,
    count: u64,
    revision: u64,
}

/// Project only telemetry fields from the game's health response.
#[derive(Serialize, Deserialize)]
pub struct CaptureHealth {
    profile: TelemetryProfile,
    telemetry_status: TelemetryStatus,
    capabilities: BTreeMap<String, Capability>,
    faults: BTreeMap<String, Fault>,
    pub revision: u64,
    pub errors: u64,
    pub play_queue_dropped: u64,
    #[serde(deserialize_with = "Option::deserialize")]
    last_error: Option<LastError>,
}

impl CaptureHealth {
    pub fn from_game(value: &Value) -> Option<Self> {
        let last_error = value.get("last_error")?;
        if !last_error.is_null() && !last_error.is_object() {
            return None;
        }
        Self::deserialize(value).ok()
    }

    pub fn degraded(&self) -> bool {
        matches!(
            self.telemetry_status,
            TelemetryStatus::Degraded | TelemetryStatus::Failed
        ) || !self.faults.is_empty()
    }
}

#[derive(Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShardLoss {
    pub telemetry_gaps: u64,
    pub invalid_telemetry: u64,
    pub generation_rejected: u64,
    pub sequence_rejected: u64,
    pub reply_dropped: u64,
    pub relay_dropped: u64,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoomLoss {
    pub stdout_dropped: u64,
}

#[derive(Serialize, Deserialize)]
pub struct CoverageBody {
    pub requested_profile: TelemetryProfile,
    #[serde(flatten)]
    pub health: CaptureHealth,
    pub shard_loss: ShardLoss,
    pub room_loss: RoomLoss,
}

impl CoverageBody {
    /// Serde can also accept positional arrays for structs. History needs the writer's exact JSON shape.
    pub fn valid(value: &Value) -> bool {
        Self::deserialize(value).is_ok_and(|body| json!(body) == *value)
    }
}
