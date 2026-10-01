//! Native game event validation using the frozen producer contract.

use std::{collections::BTreeMap, sync::LazyLock};

use jsonschema::{Keyword, ValidationError, Validator};
use serde_json::{Value, json};

use crate::{model, process::MAX_PROTOCOL_LINE_BYTES};

pub const MAX_EVENT_BYTES: usize = MAX_PROTOCOL_LINE_BYTES - b"DST_OTEL|\n".len();

static SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../resources/events.json"))
        .expect("valid bundled game event schema")
});

// JSON Schema accepts 1.0 as an integer. Native counters require integer JSON
// values, matching the strict producer contract without rounding or coercion.
struct StrictInteger;

impl<'i> Keyword<'i> for StrictInteger {
    fn validate(&self, value: &'i Value) -> Result<(), ValidationError<'i>> {
        if self.is_valid(value) {
            Ok(())
        } else {
            Err(ValidationError::custom("expected an integer JSON value"))
        }
    }

    fn is_valid(&self, value: &'i Value) -> bool {
        value.as_number().is_some_and(|value| !value.is_f64())
    }
}

fn strict_integers(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            if fields.get("type").and_then(Value::as_str) == Some("integer") {
                fields.insert("x-dst-strict-integer".into(), Value::Bool(true));
            }
            fields.values_mut().for_each(strict_integers);
        }
        Value::Array(values) => values.iter_mut().for_each(strict_integers),
        _ => {}
    }
}

static VALIDATORS: LazyLock<BTreeMap<String, Validator>> = LazyLock::new(|| {
    let mut base = SCHEMA.clone();
    strict_integers(&mut base);
    let object = base.as_object_mut().expect("object schema");
    object.remove("oneOf");
    object.remove("discriminator");
    SCHEMA["discriminator"]["mapping"]
        .as_object()
        .expect("event discriminator")
        .iter()
        .map(|(event, reference)| {
            base["$ref"] = reference.clone();
            let validator = jsonschema::options()
                .should_validate_formats(true)
                .with_keyword("x-dst-strict-integer", |_, _, _| {
                    Ok(Box::new(StrictInteger))
                })
                .build(&base)
                .expect("valid bundled event variant");
            (event.clone(), validator)
        })
        .collect()
});

/// Self-contained schema for all supported version 3 game events.
pub fn schema() -> &'static Value {
    &SCHEMA
}

/// Validate a complete native event without changing its values.
pub fn validate(value: &Value) -> model::Result<()> {
    let event = value["event"]
        .as_str()
        .ok_or_else(|| model::Error::invalid("/event", "game event name must be a string"))?;
    let validator = VALIDATORS
        .get(event)
        .ok_or_else(|| model::Error::invalid("/event", "unsupported game event"))?;
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
            Value::Number(number) if number.as_f64().is_none_or(|number| !number.is_finite()) => {
                return Err(model::Error::invalid(
                    "event",
                    "game event numbers must be finite",
                ));
            }
            _ => {}
        }
    }
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
    validator.validate(value).map_err(|error| {
        model::Error::invalid("event", "invalid game event")
            .with_details(json!({"field": error.instance_path().to_string()}))
    })
}

/// Parse and validate a native JSON record, retaining nulls and exact numbers.
pub fn parse(source: &str) -> model::Result<Value> {
    if source.len() > MAX_EVENT_BYTES {
        return Err(model::Error::invalid(
            "event",
            "game event exceeds byte limit",
        ));
    }
    let value = serde_json::from_str(source)
        .map_err(|_| model::Error::invalid("event", "game event must be valid JSON"))?;
    validate(&value)?;
    Ok(value)
}
