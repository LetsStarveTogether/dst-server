//! Decode the Agent's local envelope once for journal history and live consumers.

mod journal;
mod netdata;
#[cfg(test)]
mod tests;
pub use journal::{
    HistoryCheckpoint, HistoryPage, HistoryQuery, HistoryReader, HistorySelection,
    decode_journal_field,
};
pub use netdata::{RoomStream, TelemetrySearch, TelemetrySearchResult};

use std::{collections::BTreeSet, sync::LazyLock};

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::{events, host::Host, rooms::MAX_ROOM_SLOT, telemetry, validation};

pub const COVERAGE: &str = "dst.telemetry.coverage";
pub const PREFIX: &str = "DST_RECORD|";
const LEGACY_CHAT: &str = "dst.player.chat";

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct EventFilter {
    pub events: BTreeSet<String>,
    pub shards: BTreeSet<String>,
    pub userid: Option<String>,
}

impl EventFilter {
    pub fn validate(&self, coverage_only: bool) -> Result<()> {
        ensure!(
            !coverage_only || (self.events.is_empty() && self.userid.is_none()),
            "coverage-only accepts shard selection but cannot filter events or players"
        );
        for event in &self.events {
            ensure!(
                events::EVENT_NAMES.contains(&event.as_str()),
                "unknown gameplay event {event}"
            );
        }
        for shard in &self.shards {
            crate::configuration::validate_shard_name(shard)?;
        }
        if let Some(userid) = &self.userid {
            events::types::validate_userid(userid)?;
        }
        Ok(())
    }

    pub fn selects_shard(&self, shard: &str) -> bool {
        self.shards.is_empty() || self.shards.contains(shard)
    }

    pub fn selects_name(&self, name: &str) -> bool {
        events::EVENT_NAMES.contains(&name)
            && (self.events.is_empty() || self.events.contains(name))
    }

    fn matches(&self, name: &str, data: &Value) -> bool {
        self.selects_name(name)
            && self.userid.as_ref().is_none_or(|id| {
                events::types::participant_fields(name)
                    .iter()
                    .any(|path| role_matches(data, path, id))
            })
    }
}

fn role_matches(value: &Value, path: &str, userid: &str) -> bool {
    let (key, rest) = path.split_once('.').unwrap_or((path, ""));
    let matches = |item: &Value| {
        if rest.is_empty() {
            item.as_str() == Some(userid)
        } else {
            role_matches(item, rest, userid)
        }
    };
    if let Some(key) = key.strip_suffix("[]") {
        value[key]
            .as_array()
            .is_some_and(|items| items.iter().any(matches))
    } else {
        matches(&value[key])
    }
}

pub fn valid_uid(value: &str) -> bool {
    value
        .parse::<ulid::Ulid>()
        .is_ok_and(|id| id.to_string() == value)
}

pub fn room_writer(unit: &str, uid: Option<&str>, user: bool) -> Option<u16> {
    if user && uid.and_then(|s| s.parse::<u32>().ok()) != Some(unsafe { libc::getuid() }) {
        return None;
    }
    room_number(unit)
}

pub fn room_number(unit: &str) -> Option<u16> {
    let digits = unit.strip_prefix("dst-")?.strip_suffix(".service")?;
    let number: u16 = digits.parse().ok()?;
    (number <= MAX_ROOM_SLOT && Host::unit(number) == unit).then_some(number)
}

#[derive(Debug, Serialize)]
pub struct Observation {
    pub uid: String,
    pub cluster: String,
    pub shard: String,
    pub observed_timestamp_ns: u64,
    pub resource: Map<String, Value>,
    pub attributes: Map<String, Value>,
    pub severity_text: Option<String>,
    pub trace: Option<crate::telemetry::LogTrace>,
    #[serde(flatten)]
    pub body: ObservationBody,
}

#[derive(Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ObservationBody {
    Gameplay {
        event: Value,
    },
    Coverage {
        nonce: String,
        generation: u64,
        data: Value,
        agent_instance_id: String,
    },
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Rejection {
    InvalidEnvelope,
    InvalidIdentity,
    InvalidGameplay,
    InvalidCoverage,
    UnsupportedSchema,
}

fn headers(log: &Value) -> Result<(Option<String>, Option<telemetry::LogTrace>), Rejection> {
    let severity = log
        .get("severity_text")
        .map(|value| {
            let text = value.as_str().ok_or(Rejection::InvalidEnvelope)?;
            telemetry::log_severity(text).map_err(|_| Rejection::InvalidEnvelope)?;
            Ok(text.to_owned())
        })
        .transpose()?;
    let trace = if ["trace_id", "span_id", "trace_flags"]
        .iter()
        .any(|key| log.get(key).is_some())
    {
        let trace =
            telemetry::LogTrace::deserialize(log).map_err(|_| Rejection::InvalidEnvelope)?;
        trace.ids().map_err(|_| Rejection::InvalidEnvelope)?;
        Some(trace)
    } else {
        None
    };
    Ok((severity, trace))
}

/// Classify unsequenced records using the stream the subscriber registered for.
pub fn live_auxiliary(log: &Value, lifecycle: bool) -> Result<bool, Rejection> {
    if log.get("event_name").is_none() && log["event"] == "room_status" {
        headers(log)?;
        return if lifecycle && log["status"].is_object() {
            Ok(true)
        } else {
            Err(Rejection::InvalidEnvelope)
        };
    }
    let name = log["event_name"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or(Rejection::InvalidEnvelope)?;
    if name == LEGACY_CHAT {
        return Err(Rejection::UnsupportedSchema);
    }
    let attrs = log["attributes"]
        .as_object()
        .ok_or(Rejection::InvalidEnvelope)?;
    if name == COVERAGE
        || events::EVENT_NAMES.contains(&name)
        || attrs.contains_key("dst.event.sequence")
    {
        return Ok(false);
    }
    if !lifecycle && name != "dst.runtime.diagnostic" {
        return Err(Rejection::UnsupportedSchema);
    }
    if log.get("body").is_none()
        || log["observed_timestamp_ns"].as_u64().is_none()
        || !attrs
            .get("log.record.uid")
            .and_then(Value::as_str)
            .is_some_and(valid_uid)
        || log.get("resource").is_some_and(|r| !r.is_object())
    {
        return Err(Rejection::InvalidEnvelope);
    }
    headers(log)?;
    Ok(true)
}

// Recognize the former item and actor shapes solely to classify a rejection.
fn legacy_meal(mut event: Value) -> Option<()> {
    use events::types::{Identifier, Position, PositiveCounter};

    let data = event["data"].as_object_mut()?;
    for role in ["player", "feeder"] {
        let actor = data.get_mut(role)?;
        if role == "feeder" && actor.is_null() {
            continue;
        }
        let position = actor.as_object_mut()?.remove("position")?;
        if !position.is_null() {
            position.as_object()?;
            validation::decode::<Position>(&position).ok()?;
        }
    }
    let food = data.get_mut("food")?.as_object_mut()?;
    for key in ["guid", "stack_size"] {
        validation::decode::<PositiveCounter>(&food.remove(key)?).ok()?;
    }
    let skin = food.remove("skin")?;
    if !skin.is_null() {
        validation::decode::<Identifier>(&skin).ok()?;
    }
    if let Some(condition) = food.remove("condition") {
        for (name, value) in condition.as_object()? {
            if !matches!(
                name.as_str(),
                "uses_percent" | "fuel_percent" | "freshness_percent" | "armor_percent"
            ) {
                return None;
            }
            validation::decode::<validation::FiniteNumber>(value).ok()?;
        }
    }
    let prefab = food.remove("prefab")?;
    if !food.is_empty() {
        return None;
    }
    data.insert("food".into(), prefab);
    events::validate(&event).ok()
}

pub fn decode_message(
    message: &str,
    filter: &EventFilter,
    coverage_only: bool,
) -> Result<Option<Observation>, Rejection> {
    let Some(payload) = message.strip_prefix(PREFIX) else {
        return Ok(None);
    };
    let log = crate::model::parse_unique_json(payload.as_bytes())
        .map_err(|_| Rejection::InvalidEnvelope)?;
    decode_record(log, filter, coverage_only)
}

/// Local socket records and framed journal JSON carry the same producer envelope.
pub fn decode_record(
    mut log: Value,
    filter: &EventFilter,
    coverage_only: bool,
) -> Result<Option<Observation>, Rejection> {
    if log.get("event").and_then(Value::as_str) == Some("room_status")
        && log.get("event_name").is_none()
    {
        return Ok(None);
    }
    let name = log["event_name"]
        .as_str()
        .ok_or(Rejection::InvalidEnvelope)?;
    if coverage_only && name != COVERAGE {
        return Ok(None);
    }
    if name == LEGACY_CHAT {
        return Err(Rejection::UnsupportedSchema);
    }
    let attrs = log["attributes"]
        .as_object()
        .ok_or(Rejection::InvalidEnvelope)?;
    if name != COVERAGE && !events::EVENT_NAMES.contains(&name) {
        return if attrs.contains_key("dst.event.sequence") {
            Err(Rejection::UnsupportedSchema)
        } else {
            Ok(None)
        };
    }
    if log.get("resource").is_some_and(|v| !v.is_object()) {
        return Err(Rejection::InvalidEnvelope);
    }
    let (severity_text, trace) = headers(&log)?;
    let attr = |key: &str| {
        attrs
            .get(key)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or(Rejection::InvalidIdentity)
    };
    let uid = attr("log.record.uid")?;
    let nonce = attr("dst.game.attempt.id")?;
    let cluster = attr("dst.cluster.name")?;
    let shard = attr("dst.shard.name")?;
    let generation = attrs
        .get("dst.runtime.generation")
        .and_then(Value::as_u64)
        .filter(|v| *v < (1u64 << 53))
        .ok_or(Rejection::InvalidIdentity)?;
    let observed_timestamp_ns = log["observed_timestamp_ns"]
        .as_u64()
        .ok_or(Rejection::InvalidIdentity)?;
    if !valid_uid(uid) || !valid_uid(nonce) {
        return Err(Rejection::InvalidIdentity);
    }
    let data = log.get("body").ok_or(Rejection::InvalidEnvelope)?;
    let body = if name == COVERAGE {
        let instance = log["resource"]["service.instance.id"]
            .as_str()
            .filter(|s| !s.trim().is_empty())
            .ok_or(Rejection::InvalidCoverage)?;
        if !events::coverage::CoverageBody::valid(data) {
            return Err(Rejection::InvalidCoverage);
        }
        if !filter.selects_shard(shard) {
            return Ok(None);
        }
        ObservationBody::Coverage {
            nonce: nonce.to_owned(),
            generation,
            data: data.clone(),
            agent_instance_id: instance.to_owned(),
        }
    } else {
        let event = json!({
            "event":name, "nonce":nonce, "generation":generation, "session_id":attrs.get("dst.session.id"),
            "seq":attrs.get("dst.event.sequence"), "tick":attrs.get("dst.tick"), "monotonic_ms":attrs.get("dst.monotonic_ms"),
            "cycle":attrs.get("dst.world.cycle"), "data":data,
        });
        if events::validate(&event).is_err() {
            return Err(
                if name == "dst.player.ate" && legacy_meal(event).is_some() {
                    Rejection::UnsupportedSchema
                } else {
                    Rejection::InvalidGameplay
                },
            );
        }
        if !filter.selects_shard(shard) || !filter.matches(name, &event["data"]) {
            return Ok(None);
        }
        ObservationBody::Gameplay { event }
    };
    let (uid, cluster, shard) = (uid.to_owned(), cluster.to_owned(), shard.to_owned());
    let attributes = std::mem::take(
        log["attributes"]
            .as_object_mut()
            .expect("validated attributes"),
    );
    let resource = match log
        .as_object_mut()
        .and_then(|fields| fields.remove("resource"))
    {
        Some(Value::Object(fields)) => fields,
        None => Map::new(),
        _ => return Err(Rejection::InvalidEnvelope),
    };
    Ok(Some(Observation {
        uid,
        cluster,
        shard,
        observed_timestamp_ns,
        resource,
        attributes,
        severity_text,
        trace,
        body,
    }))
}

pub fn candidate_pattern(coverage_only: bool) -> &'static str {
    static COVERAGE_PATTERN: LazyLock<String> = LazyLock::new(|| {
        format!(
            r#"^DST_RECORD\|.*"event_name"\s*:\s*"{}""#,
            regex::escape(COVERAGE)
        )
    });
    static HISTORY_PATTERN: LazyLock<String> = LazyLock::new(|| {
        let names = events::EVENT_NAMES
            .iter()
            .copied()
            .chain([COVERAGE, LEGACY_CHAT])
            .map(regex::escape)
            .collect::<Vec<_>>()
            .join("|");
        format!(r#"^DST_RECORD\|.*(?:"dst\.event\.sequence"\s*:|"event_name"\s*:\s*"(?:{names})")"#)
    });
    if coverage_only {
        &COVERAGE_PATTERN
    } else {
        &HISTORY_PATTERN
    }
}
