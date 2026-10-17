//! Typed input validation with bounded, value-free diagnostics.

use std::fmt;

use garde::Validate;
use serde::{Deserialize, Deserializer, Serialize, de::DeserializeOwned};
use serde_json::{Number, Value};

#[derive(Debug)]
pub struct Invalid {
    pub path: String,
}

impl fmt::Display for Invalid {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "invalid value at {}", self.path)
    }
}

impl std::error::Error for Invalid {}

pub fn decode<T>(value: &Value) -> Result<T, Invalid>
where
    T: DeserializeOwned + Validate<Context = ()>,
{
    let parsed: T = serde_path_to_error::deserialize(value).map_err(|error| Invalid {
        path: error.path().to_string(),
    })?;
    check(&parsed)?;
    Ok(parsed)
}

pub fn check<T: Validate<Context = ()>>(value: &T) -> Result<(), Invalid> {
    value.validate().map_err(|report| Invalid {
        path: report
            .iter()
            .next()
            .map(|(path, _)| path.to_string())
            .unwrap_or_default(),
    })
}

/// An absent field stays None; an explicit null is parsed by T.
pub fn present<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

pub fn finite(value: &Number, _: &()) -> garde::Result {
    if value.as_f64().is_some_and(f64::is_finite) {
        Ok(())
    } else {
        Err(garde::Error::new("number must be finite"))
    }
}

pub fn is_true(value: &bool, _: &()) -> garde::Result {
    if *value {
        Ok(())
    } else {
        Err(garde::Error::new("must be true"))
    }
}

/// Retain the producer's integer or floating-point representation.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct FiniteNumber(#[garde(custom(finite))] pub Number);
