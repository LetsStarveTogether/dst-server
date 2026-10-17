//! Sparse configuration models. Defaults and field constraints live with each field.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, bail, ensure};
use garde::Validate;
use serde::{Deserialize, Serialize};
use serde_json::{Number, Value, json};

use crate::{lua, validation};

pub trait Resolve {
    fn resolved(&self) -> Value;
}

macro_rules! scalar_resolve {
    ($($ty:ty),* $(,)?) => {$(
        impl Resolve for $ty {
            fn resolved(&self) -> Value {
                serde_json::to_value(self).expect("serializable configuration value")
            }
        }
    )*};
}

scalar_resolve!(
    String,
    bool,
    u64,
    i64,
    Number,
    Value,
    crate::validation::FiniteNumber
);

impl<T: Resolve> Resolve for Option<T> {
    fn resolved(&self) -> Value {
        self.as_ref().map_or(Value::Null, Resolve::resolved)
    }
}

impl<T: Resolve> Resolve for Box<T> {
    fn resolved(&self) -> Value {
        self.as_ref().resolved()
    }
}

impl<T: Resolve> Resolve for Vec<T> {
    fn resolved(&self) -> Value {
        Value::Array(self.iter().map(Resolve::resolved).collect())
    }
}

impl<T: Resolve, const N: usize> Resolve for [T; N] {
    fn resolved(&self) -> Value {
        Value::Array(self.iter().map(Resolve::resolved).collect())
    }
}

impl<T: Resolve> Resolve for BTreeMap<String, T> {
    fn resolved(&self) -> Value {
        Value::Object(
            self.iter()
                .map(|(key, value)| (key.clone(), value.resolved()))
                .collect(),
        )
    }
}

macro_rules! sparse_model {
    (
        $(#[$meta:meta])* $name:ident {
            required { $($(#[$required_meta:meta])* $required:ident: $required_ty:ty,)* }
            optional { $($(#[$field_meta:meta])* $field:ident: $ty:ty = $default:expr,)* }
        }
    ) => {
        $(#[$meta])*
        #[derive(Clone, PartialEq, Serialize, Deserialize, Validate)]
        #[serde(deny_unknown_fields)]
        pub struct $name {
            $($(#[$required_meta])* pub $required: $required_ty,)*
            $(
                $(#[$field_meta])*
                #[serde(default, deserialize_with = "validation::present", skip_serializing_if = "Option::is_none")]
                pub $field: Option<$ty>,
            )*
        }

        impl $name {
            $(pub fn $field(&self) -> $ty {
                self.$field.clone().unwrap_or_else(|| $default)
            })*
        }

        impl Resolve for $name {
            fn resolved(&self) -> Value {
                Value::Object([
                    $((stringify!($required).trim_start_matches("r#").into(), self.$required.resolved()),)*
                    $((stringify!($field).trim_start_matches("r#").into(), self.$field().resolved()),)*
                ].into_iter().collect())
            }
        }

        impl std::fmt::Debug for $name {
            fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                super::redact(serde_json::to_value(self).expect("serializable configuration")).fmt(formatter)
            }
        }
    };
}

macro_rules! choices {
    ($name:ident { $($variant:ident => $value:literal),+ $(,)? }) => {
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
        pub enum $name {
            $(#[serde(rename = $value)] $variant),+
        }
        scalar_resolve!($name);
        impl IniValue for $name {
            fn parse(text: &str) -> Result<Value> {
                Ok(json!(text))
            }
        }
    };
}

macro_rules! world_model {
    ($name:ident, $loaded:ident {
        $($(#[$meta:meta])* $field:ident: $ty:ty = $default:expr,)*
    } loaded {
        $($(#[$extra_meta:meta])* $extra:ident: $extra_ty:ty = $extra_default:expr,)*
    }) => {
        sparse_model! {
            #[derive(Default)]
            $name {
                required {}
                optional { $($(#[$meta])* $field: $ty = $default,)* }
            }
        }

        #[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Validate)]
        pub struct $loaded {
            $(
                $(#[$meta])*
                #[serde(default, deserialize_with = "validation::present", skip_serializing_if = "Option::is_none")]
                pub $field: Option<$ty>,
            )*
            $(
                $(#[$extra_meta])*
                #[serde(default, deserialize_with = "validation::present", skip_serializing_if = "Option::is_none")]
                pub $extra: Option<$extra_ty>,
            )*
            #[serde(flatten)]
            #[garde(dive)]
            pub custom: BTreeMap<String, LuaScalar>,
        }

        impl Resolve for $loaded {
            fn resolved(&self) -> Value {
                let mut value = Value::Object([
                    $((stringify!($field).trim_start_matches("r#").into(), self.$field.clone().unwrap_or_else(|| $default).resolved()),)*
                    $((stringify!($extra).trim_start_matches("r#").into(), self.$extra.clone().unwrap_or_else(|| $extra_default).resolved()),)*
                ].into_iter().collect());
                value.as_object_mut().expect("world fields").extend(
                    self.custom.iter().map(|(key, value)| (key.clone(), value.resolved()))
                );
                value
            }
        }
    };
}

trait IniValue {
    fn parse(text: &str) -> Result<Value>;
}

impl IniValue for bool {
    fn parse(text: &str) -> Result<Value> {
        ensure!(
            text.eq_ignore_ascii_case("true") || text.eq_ignore_ascii_case("false"),
            "INI boolean must be true or false"
        );
        Ok(json!(text.eq_ignore_ascii_case("true")))
    }
}

impl IniValue for u64 {
    fn parse(text: &str) -> Result<Value> {
        ensure!(
            !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()),
            "INI integer must be unsigned decimal"
        );
        Ok(json!(text.parse::<u64>().map_err(|_| anyhow::anyhow!(
            "INI integer exceeds uint64"
        ))?))
    }
}

impl IniValue for String {
    fn parse(text: &str) -> Result<Value> {
        Ok(json!(text))
    }
}

impl<T: IniValue> IniValue for Option<T> {
    fn parse(text: &str) -> Result<Value> {
        T::parse(text)
    }
}

macro_rules! ini_model {
    ($name:ident { $($section:literal { $($(#[$meta:meta])* $field:ident: $ty:ty = $default:expr,)* })* }) => {
        sparse_model! {
            #[derive(Default)]
            $name {
                required {}
                optional { $($($(#[$meta])* $field: $ty = $default,)*)* }
            }
        }

        impl $name {
            pub const SECTIONS: &'static [(&'static str, &'static [&'static str])] = &[
                $(($section, &[$(stringify!($field)),*])),*
            ];

            pub fn parse_field(section: &str, name: &str, text: &str) -> Result<Value> {
                match name {
                    $($(stringify!($field) if section.eq_ignore_ascii_case($section) => <$ty as IniValue>::parse(text),)*)*
                    _ => bail!("unknown INI option"),
                }
            }
        }
    };
}

fn lua_value(value: &Value, _: &()) -> garde::Result {
    lua::render_literal(value)
        .map(|_| ())
        .map_err(|_| garde::Error::new("invalid literal Lua value"))
}

fn lua_scalar(value: &Value, context: &()) -> garde::Result {
    if !matches!(value, Value::String(_) | Value::Bool(_) | Value::Number(_)) {
        return Err(garde::Error::new(
            "world overrides require scalar Lua values",
        ));
    }
    lua_value(value, context)
}

fn unit_interval(value: &Number, _: &()) -> garde::Result {
    if value
        .as_f64()
        .is_some_and(|value| value.is_finite() && (0.0..=1.0).contains(&value))
    {
        Ok(())
    } else {
        Err(garde::Error::new("number must be between zero and one"))
    }
}

fn unique<T: Ord>(value: &Option<Vec<T>>, _: &()) -> garde::Result {
    if value
        .as_ref()
        .is_none_or(|items| items.iter().collect::<BTreeSet<_>>().len() == items.len())
    {
        Ok(())
    } else {
        Err(garde::Error::new("items must be unique"))
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct LuaValue(#[garde(custom(lua_value))] pub Value);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct LuaScalar(#[garde(custom(lua_scalar))] pub Value);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Validate)]
#[serde(transparent)]
#[garde(transparent)]
pub struct UnitInterval(#[garde(custom(unit_interval))] pub Number);

scalar_resolve!(LuaValue, LuaScalar, UnitInterval);

include!("models.rs");
include!("world.rs");
