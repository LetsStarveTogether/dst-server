//! Typed game events received from the Lua producer.

pub mod types;

pub use types::{EVENT_NAMES, GameEvent};

use serde_json::{Value, json};

use crate::{model, process::MAX_PROTOCOL_LINE_BYTES, validation};

pub const MAX_EVENT_BYTES: usize = MAX_PROTOCOL_LINE_BYTES - b"DST_OTEL|\n".len();

impl GameEvent {
    pub fn into_value(self) -> Value {
        serde_json::to_value(self).expect("serializable game event")
    }
}

/// Validate a complete native event without changing its values.
pub fn validate(value: &Value) -> model::Result<()> {
    decode(value)?;
    if serde_json::to_vec(value)
        .map_err(|_| model::Error::invalid("event", "game event must contain finite JSON values"))?
        .len()
        > MAX_EVENT_BYTES
    {
        return Err(model::Error::invalid(
            "event",
            "game event exceeds byte limit",
        ));
    }
    Ok(())
}

fn decode(value: &Value) -> model::Result<GameEvent> {
    let mut stack = vec![(value, 0)];
    let mut nodes = 0;
    while let Some((value, depth)) = stack.pop() {
        nodes += 1;
        if depth > 64 || nodes > 65_536 {
            return Err(model::Error::invalid(
                "event",
                "game event exceeds complexity limit",
            ));
        }
        match value {
            Value::Object(fields) => stack.extend(fields.values().map(|value| (value, depth + 1))),
            Value::Array(items) => stack.extend(items.iter().map(|value| (value, depth + 1))),
            _ => {}
        }
    }
    validation::decode(value).map_err(|error| {
        model::Error::invalid("event", "invalid game event")
            .with_details(json!({"field": error.path}))
    })
}

/// Parse a native record, retaining explicit nulls and exact JSON numbers.
pub fn parse(source: impl AsRef<[u8]>) -> model::Result<GameEvent> {
    let source = source.as_ref();
    if source.len() > MAX_EVENT_BYTES {
        return Err(model::Error::invalid(
            "event",
            "game event exceeds byte limit",
        ));
    }
    let value = serde_json::from_slice(source)
        .map_err(|_| model::Error::invalid("event", "game event must be valid JSON"))?;
    decode(&value)
}
