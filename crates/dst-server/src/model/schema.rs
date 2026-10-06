//! Generate discovery metadata from the same field types and defaults used to decode requests.
use super::{
    Countdown, Error, MAX_SAFE_INTEGER, MAX_SOURCE_BYTES, Method, MethodDescription, Result,
    finite, identifier, integer, positive_duration, text,
};
use serde_json::{Map, Value, json};

#[derive(Clone, Copy, Debug)]
pub(super) struct Argument {
    pub name: &'static str,
    pub describe: fn(&str, &str) -> Value,
    pub optional: bool,
    pub default: Option<fn() -> Value>,
}

impl Method {
    pub(super) fn apply_defaults(
        &self,
        input: &mut Value,
    ) -> std::result::Result<(), &'static str> {
        let fields = input.as_object_mut().ok_or("arguments must be an object")?;
        for argument in self.arguments {
            if let Some(default) = argument.default {
                fields.entry(argument.name).or_insert_with(default);
            }
        }
        Ok(())
    }

    pub fn description(self) -> MethodDescription {
        let mut properties = Map::new();
        let mut required = Vec::new();
        for field in self.arguments {
            let mut property = (field.describe)(self.name, field.name);
            if let Some(default) = field.default {
                property["default"] = default();
            } else if !field.optional {
                required.push(field.name);
            }
            properties.insert(field.name.into(), property);
        }
        let mut arguments_schema = json!({
            "type": "object", "properties": properties, "required": required, "additionalProperties": false,
        });
        if !self.alternatives.is_empty() {
            arguments_schema["anyOf"] = self
                .alternatives
                .iter()
                .map(|&field| {
                    json!({
                        "required": [field], "properties": {field: {"not": {"enum": [null, []]}}},
                    })
                })
                .collect();
        }
        MethodDescription {
            name: self.name.into(),
            mutation: self.mutation,
            scopes: self.scopes.to_vec(),
            default_timeout: self.default_timeout,
            arguments_schema,
        }
    }
}

pub(super) trait ArgumentType {
    const OPTIONAL: bool = false;
    fn schema(method: &str, field: &str) -> Value;
    fn check(&self, method: &str, field: &str) -> Result<()>;
}

impl<T: ArgumentType> ArgumentType for Option<T> {
    const OPTIONAL: bool = true;
    fn schema(method: &str, field: &str) -> Value {
        json!({"anyOf": [T::schema(method, field), {"type": "null"}]})
    }
    fn check(&self, method: &str, field: &str) -> Result<()> {
        if let Some(value) = self {
            value.check(method, field)?;
        }
        Ok(())
    }
}

fn identifier_field(field: &str) -> bool {
    matches!(
        field,
        "userid"
            | "shard_id"
            | "session_id"
            | "expected_session_id"
            | "command"
            | "starter_userid"
            | "target_userid"
    )
}

impl ArgumentType for String {
    fn schema(_: &str, field: &str) -> Value {
        if identifier_field(field) {
            json!({"type":"string","minLength":1,"maxLength":128,"not":{"pattern":r"[\u0000-\u001f\u007f-\u009f]"}})
        } else {
            json!({"type":"string","minLength":1,"maxLength":MAX_SOURCE_BYTES,"not":{"pattern":r"\u0000"},
                "description":format!("At most {MAX_SOURCE_BYTES} UTF-8 bytes.")})
        }
    }
    fn check(&self, _: &str, field: &str) -> Result<()> {
        if identifier_field(field) {
            identifier(field, self)
        } else {
            text(field, self, MAX_SOURCE_BYTES)
        }
    }
}

fn integer_bounds(method: &str, field: &str) -> (u64, u64) {
    match (method, field) {
        (_, "limit") => (1, 100),
        ("give", "count") => (1, 64),
        (_, "before") | ("rollback", "count") => (0, MAX_SAFE_INTEGER),
        _ => (1, MAX_SAFE_INTEGER),
    }
}

impl ArgumentType for u64 {
    fn schema(method: &str, field: &str) -> Value {
        let (min, max) = integer_bounds(method, field);
        json!({"type":"integer","minimum":min,"maximum":max})
    }
    fn check(&self, method: &str, field: &str) -> Result<()> {
        let (min, max) = integer_bounds(method, field);
        integer(field, *self, min, max)
    }
}

fn is_fraction(field: &str) -> bool {
    matches!(field, "health" | "hunger" | "sanity" | "moisture")
}

impl ArgumentType for f64 {
    fn schema(_: &str, field: &str) -> Value {
        if is_fraction(field) {
            json!({"type":"number","minimum":0,"maximum":1})
        } else if field == "interval" {
            json!({"type":"number","exclusiveMinimum":0})
        } else {
            json!({"type":"number"})
        }
    }
    fn check(&self, _: &str, field: &str) -> Result<()> {
        if is_fraction(field) && !(0.0..=1.0).contains(self) {
            return Err(Error::invalid(
                field,
                "player vital must be finite and between 0 and 1",
            ));
        }
        if field == "interval" {
            positive_duration(field, *self).map(|_| ())
        } else {
            finite(field, *self)
        }
    }
}

impl ArgumentType for bool {
    fn schema(_: &str, _: &str) -> Value {
        json!({"type":"boolean"})
    }
    fn check(&self, _: &str, _: &str) -> Result<()> {
        Ok(())
    }
}
impl ArgumentType for Value {
    fn schema(_: &str, _: &str) -> Value {
        json!({"type":"object"})
    }
    fn check(&self, _: &str, field: &str) -> Result<()> {
        if self.is_object() {
            Ok(())
        } else {
            Err(Error::invalid(field, format!("{field} must be an object")))
        }
    }
}
impl ArgumentType for Countdown {
    fn schema(_: &str, _: &str) -> Value {
        json!({
            "type":"object","required":["template"],"additionalProperties":false,
            "properties":{
                "template":String::schema("", "template"),
                "delay":{"type":"number","minimum":0,"default":super::notice_delay()},
                "interval":{"type":"number","exclusiveMinimum":0,"default":super::notice_interval()},
                "parameters":{"type":"object","propertyNames":{"not":{"enum":["remaining","minutes","when"]}},
                    "additionalProperties":{"type":["string","number"]}}
            }
        })
    }
    fn check(&self, _: &str, _: &str) -> Result<()> {
        self.validate()
    }
}

fn edit_pointer() -> Value {
    json!({"type":"string","pattern":"^/(cluster|policy)(/|$)"})
}

fn check_pointers<'a>(pointers: impl Iterator<Item = &'a String>) -> Result<()> {
    for pointer in pointers {
        if !["/cluster", "/policy"].iter().any(|root| {
            pointer == root
                || pointer
                    .strip_prefix(root)
                    .is_some_and(|tail| tail.starts_with('/'))
        }) {
            return Err(Error::invalid(
                "pointer",
                "online edits only support cluster and policy fields; deployment and template changes require an inactive room service",
            ));
        }
    }
    Ok(())
}

impl ArgumentType for Vec<(String, Value)> {
    fn schema(_: &str, _: &str) -> Value {
        json!({"type":"array","items":{"type":"array","prefixItems":[edit_pointer(),{}],"minItems":2,"maxItems":2}})
    }
    fn check(&self, _: &str, _: &str) -> Result<()> {
        check_pointers(self.iter().map(|(path, _)| path))
    }
}
impl ArgumentType for Vec<String> {
    fn schema(_: &str, _: &str) -> Value {
        json!({"type":"array","items":edit_pointer()})
    }
    fn check(&self, _: &str, _: &str) -> Result<()> {
        check_pointers(self.iter())
    }
}

pub(super) trait Supplied {
    fn supplied(&self) -> bool;
}
impl<T> Supplied for Option<T> {
    fn supplied(&self) -> bool {
        self.is_some()
    }
}
impl<T> Supplied for Vec<T> {
    fn supplied(&self) -> bool {
        !self.is_empty()
    }
}
