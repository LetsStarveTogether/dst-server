//! Typed game events received from the Lua producer.

pub mod coverage;
pub mod types;

pub use types::{EVENT_NAMES, GameEvent};

use serde::{Deserialize, Serialize};
use serde_json::{Value, error::Category, json};

use crate::{model, process::MAX_PROTOCOL_LINE_BYTES, validation};

pub const MAX_EVENT_BYTES: usize = MAX_PROTOCOL_LINE_BYTES - b"DST_OTEL|\n".len();

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RejectionReason {
    Length,
    JsonEof,
    JsonSyntax,
    JsonData,
    JsonIo,
    Complexity,
    Schema,
    Nonce,
    Generation,
    Sequence,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct JsonPosition {
    pub line: usize,
    pub column: usize,
}

fn rejected(message: &str, reason: RejectionReason) -> model::Error {
    model::Error::invalid("event", message).with_details(json!({"reason": reason}))
}

impl GameEvent {
    pub fn into_value(self) -> Value {
        serde_json::to_value(self).expect("serializable game event")
    }
}

/// Validate a complete native event without changing its values.
pub fn validate(value: &Value) -> model::Result<()> {
    check_complexity(value)?;
    let event = decode(value)?;
    if serde_json::to_vec(value)
        .map_err(|_| model::Error::invalid("event", "game event must contain finite JSON values"))?
        .len()
        > MAX_EVENT_BYTES
    {
        return Err(rejected(
            "game event exceeds byte limit",
            RejectionReason::Length,
        ));
    }
    // Serde can read a named struct from a positional array. Consumers of the
    // original JSON must be able to look up the same fields as a typed event.
    if event.into_value() != *value {
        return Err(rejected(
            "invalid game event shape",
            RejectionReason::Schema,
        ));
    }
    Ok(())
}

fn check_complexity(value: &Value) -> model::Result<()> {
    let mut stack = vec![(value, 0)];
    let mut nodes = 0;
    while let Some((value, depth)) = stack.pop() {
        nodes += 1;
        if depth > 64 || nodes > 65_536 {
            return Err(rejected(
                "game event exceeds complexity limit",
                RejectionReason::Complexity,
            ));
        }
        match value {
            Value::Object(fields) => stack.extend(fields.values().map(|value| (value, depth + 1))),
            Value::Array(items) => stack.extend(items.iter().map(|value| (value, depth + 1))),
            _ => {}
        }
    }
    Ok(())
}

fn decode<'de>(value: impl serde::Deserializer<'de>) -> model::Result<GameEvent> {
    validation::decode(value).map_err(|error| {
        model::Error::invalid("event", "invalid game event")
            .with_details(json!({"reason": RejectionReason::Schema, "field": error.path}))
    })
}

/// Parse a native record, retaining explicit nulls and exact JSON numbers.
pub fn parse(source: impl AsRef<[u8]>) -> model::Result<GameEvent> {
    let source = source.as_ref();
    if source.len() > MAX_EVENT_BYTES {
        return Err(rejected(
            "game event exceeds byte limit",
            RejectionReason::Length,
        ));
    }
    let value = serde_json::from_slice(source).map_err(|error| {
        let reason = match error.classify() {
            Category::Eof => RejectionReason::JsonEof,
            Category::Syntax => RejectionReason::JsonSyntax,
            Category::Data => RejectionReason::JsonData,
            Category::Io => RejectionReason::JsonIo,
        };
        model::Error::invalid("event", "game event must be valid JSON").with_details(json!({
            "reason": reason,
            "position": JsonPosition { line: error.line(), column: error.column() },
        }))
    })?;
    check_complexity(&value)?;
    decode(value)
}
