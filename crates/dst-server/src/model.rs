//! Public SDK values shared by native callers, the Agent, RPC and Python.

use std::{
    collections::{BTreeMap, HashSet},
    fmt,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;
pub const MAX_REQUEST_BYTES: usize = 1024 * 1024;
pub const MAX_SOURCE_BYTES: usize = 3072;
pub const COMMAND_TIMEOUT: f64 = 120.0;
pub const START_TIMEOUT: f64 = 900.0;
pub const SAVE_TIMEOUT: f64 = 300.0;
pub const RELOAD_TIMEOUT: f64 = 900.0;
pub const STOP_TIMEOUT: f64 = 120.0;
pub const OUTPUT_DRAIN_TIMEOUT: f64 = 30.0;
pub const RESTART_TIMEOUT: f64 = 1200.0;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    Invalid,
    NotReady,
    Busy,
    Unsupported,
    NotFound,
    Conflict,
    PartialFailure,
    Unknown,
    Transport,
    Timeout,
    Overflow,
    StaleReference,
    Lua,
    Protocol,
    Internal,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Error {
    pub code: ErrorCode,
    pub message: String,
    #[serde(default)]
    pub details: Value,
}

impl Error {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            details: Value::Null,
        }
    }

    pub fn with_details(mut self, details: Value) -> Self {
        self.details = details;
        self
    }

    pub fn invalid(field: &str, message: impl Into<String>) -> Self {
        Self::new(ErrorCode::Invalid, message).with_details(json!({ "field": field }))
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for Error {}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Room,
    Shard,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "scope",
    content = "shard",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum Target {
    Room,
    Shard(String),
}

impl Target {
    pub fn scope(&self) -> Scope {
        match self {
            Self::Room => Scope::Room,
            Self::Shard(_) => Scope::Shard,
        }
    }

    pub fn validate(&self) -> Result<()> {
        if let Self::Shard(name) = self {
            validate_shard_name(name)?;
        }
        Ok(())
    }
}

const ROOM: &[Scope] = &[Scope::Room];
const SHARD: &[Scope] = &[Scope::Shard];
const BOTH: &[Scope] = &[Scope::Room, Scope::Shard];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Countdown {
    pub template: String,
    #[serde(default = "notice_delay")]
    pub delay: f64,
    #[serde(default = "notice_interval")]
    pub interval: f64,
    #[serde(default)]
    pub parameters: BTreeMap<String, Value>,
}

fn notice_delay() -> f64 {
    60.0
}
fn notice_interval() -> f64 {
    30.0
}
fn one() -> u64 {
    1
}
fn hundred() -> u64 {
    100
}
fn compression_level() -> u64 {
    3
}
fn yes() -> bool {
    true
}

fn default_notice(message: &str) -> Option<Countdown> {
    Some(Countdown {
        template: message.to_owned(),
        delay: notice_delay(),
        interval: notice_interval(),
        parameters: BTreeMap::new(),
    })
}

fn shutdown_notice() -> Option<Countdown> {
    default_notice("本房间{when}关闭，请提前安排游戏进度。")
}

fn restart_notice() -> Option<Countdown> {
    default_notice("本房间{when}维护重启，预计耗时约 5 分钟，请提前安排游戏进度。")
}

fn mod_notice() -> Option<Countdown> {
    default_notice("本房间{when}重启更新 MOD，预计耗时约 5 分钟，请提前安排游戏进度。")
}

impl Countdown {
    pub fn validate(&self) -> Result<()> {
        text("notice.template", &self.template, MAX_SOURCE_BYTES)?;
        nonnegative("notice.delay", self.delay)?;
        positive_duration("notice.interval", self.interval)?;
        for (name, value) in &self.parameters {
            if ["remaining", "minutes", "when"].contains(&name.as_str()) {
                return Err(Error::invalid(
                    "notice.parameters",
                    "reserved countdown parameter",
                ));
            }
            if !matches!(value, Value::String(_) | Value::Number(_))
                || value.is_number() && !value.as_f64().is_some_and(f64::is_finite)
            {
                return Err(Error::invalid(
                    "notice.parameters",
                    "countdown parameters must be text or finite numbers",
                ));
            }
        }
        let mut chars = self.template.chars().peekable();
        while let Some(character) = chars.next() {
            if character == '{' {
                if chars.peek() == Some(&'{') {
                    chars.next();
                    continue;
                }
                let mut name = String::new();
                loop {
                    match chars.next() {
                        Some('}') => break,
                        Some(character) => name.push(character),
                        None => {
                            return Err(Error::invalid(
                                "notice.template",
                                "unclosed countdown placeholder",
                            ));
                        }
                    }
                }
                if !identifier_name(&name)
                    || !["remaining", "minutes", "when"].contains(&name.as_str())
                        && !self.parameters.contains_key(&name)
                {
                    return Err(Error::invalid(
                        "notice.template",
                        "unknown or invalid countdown placeholder",
                    ));
                }
            } else if character == '}' && chars.next() != Some('}') {
                return Err(Error::invalid("notice.template", "unmatched closing brace"));
            }
        }
        Ok(())
    }
}

fn identifier_name(value: &str) -> bool {
    let mut chars = value.chars();
    chars
        .next()
        .is_some_and(|character| character == '_' || character.is_alphabetic())
        && chars.all(|character| character == '_' || character.is_alphanumeric())
}

#[derive(Debug, Clone, Copy)]
pub struct Method {
    pub name: &'static str,
    pub mutation: bool,
    pub scopes: &'static [Scope],
    pub default_timeout: f64,
    arguments: &'static [(&'static str, &'static str, &'static str)],
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MethodDescription {
    pub name: String,
    pub mutation: bool,
    pub scopes: Vec<Scope>,
    pub default_timeout: f64,
    pub arguments_schema: Value,
}

// One registry generates the enum and the metadata used by dispatch/discovery.
macro_rules! requests {
    ($($variant:ident { $($(#[$attribute:meta])* $field:ident: $kind:ty),* $(,)? }
        => ($name:literal, $mutation:expr, $scopes:expr, $timeout:expr)),* $(,)?) => {
        #[derive(Debug, Clone, PartialEq, Serialize)]
        #[serde(tag = "method", content = "arguments", rename_all = "snake_case")]
        pub enum Request {
            $(#[serde(rename = $name)]
            $variant { $($(#[$attribute])* $field: $kind),* }),*
        }

        impl<'de> Deserialize<'de> for Request {
            fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
                let wire = WireRequest::deserialize(deserializer)?;
                // Serde's tagged-enum buffering loses arbitrary-precision JSON numbers.
                // Decode the payload directly so finite floats and integer widths survive.
                match wire.method.as_str() {
                    $($name => {
                        #[derive(Deserialize)]
                        #[serde(deny_unknown_fields)]
                        struct Arguments { $($(#[$attribute])* $field: $kind),* }
                        let Arguments { $($field),* } = serde_json::from_value(wire.arguments)
                            .map_err(serde::de::Error::custom)?;
                        Ok(Self::$variant { $($field),* })
                    }),*
                    _ => Err(serde::de::Error::custom("unknown request method")),
                }
            }
        }

        pub const METHODS: &[Method] = &[$(Method {
            name: $name,
            mutation: $mutation,
            scopes: $scopes,
            default_timeout: $timeout,
            arguments: &[$((stringify!($field), stringify!($kind), stringify!($(#[$attribute])*))),*],
        }),*];

        impl Request {
            pub fn metadata(&self) -> Method {
                match self { $(Self::$variant { .. } => Method {
                    name: $name,
                    mutation: $mutation,
                    scopes: $scopes,
                    default_timeout: $timeout,
                    arguments: &[$((stringify!($field), stringify!($kind), stringify!($(#[$attribute])*))),*],
                }),* }
            }
        }
    };
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WireRequest {
    method: String,
    #[serde(default = "empty_arguments")]
    arguments: Value,
}

fn empty_arguments() -> Value {
    json!({})
}

requests! {
    Status {} => ("status", false, BOTH, COMMAND_TIMEOUT),
    Start {} => ("start", true, BOTH, START_TIMEOUT),
    Stop { #[serde(default = "shutdown_notice")] notice: Option<Countdown> }
        => ("stop", true, BOTH, 220.0),
    Restart { #[serde(default = "restart_notice")] notice: Option<Countdown> }
        => ("restart", true, BOTH, RESTART_TIMEOUT),
    Kill {} => ("kill", true, BOTH, STOP_TIMEOUT + OUTPUT_DRAIN_TIMEOUT),
    UpdateMods {
        #[serde(default)] restart: bool,
        #[serde(default = "mod_notice")] notice: Option<Countdown>,
    } => ("update_mods", true, ROOM, 7200.0),
    ReadConfiguration {} => ("read_configuration", false, ROOM, COMMAND_TIMEOUT),
    Configure { configuration: Value, #[serde(default)] replace_permissions: bool }
        => ("configure", true, ROOM, COMMAND_TIMEOUT),
    SetPolicy { policy: Value } => ("set_policy", true, ROOM, COMMAND_TIMEOUT),
    ExportArchive {
        #[serde(default = "empty_arguments")] options: Value,
        #[serde(default = "compression_level")] compression_level: u64,
    } => ("export_archive", true, ROOM, 7200.0),
    ReleaseArchive { artifact_id: String } => ("release_archive", true, ROOM, COMMAND_TIMEOUT),
    Execute { source: String } => ("execute", true, SHARD, COMMAND_TIMEOUT),
    ExecuteJson { source: String } => ("execute_json", true, SHARD, COMMAND_TIMEOUT),
    Evaluate { source: String } => ("evaluate", true, SHARD, COMMAND_TIMEOUT),
    ExecuteAll { source: String } => ("execute_all", true, ROOM, COMMAND_TIMEOUT),
    Announce {
        message: String,
        #[serde(default = "one")] count: u64,
        #[serde(default = "notice_interval")] interval: f64,
    } => ("announce", true, ROOM, COMMAND_TIMEOUT),
    Save {} => ("save", true, ROOM, SAVE_TIMEOUT),
    Pause { paused: bool } => ("pause", true, BOTH, COMMAND_TIMEOUT),
    Reset {} => ("reset", true, ROOM, RELOAD_TIMEOUT),
    Rollback { #[serde(default = "one")] count: u64 }
        => ("rollback", true, ROOM, RELOAD_TIMEOUT),
    Regenerate {
        #[serde(default)] expected_session_id: Option<String>,
        #[serde(default)] require_empty: Option<bool>,
    } => ("regenerate", true, ROOM, RELOAD_TIMEOUT),
    RegenerateShard { #[serde(default = "yes")] preserve_settings: bool }
        => ("regenerate_shard", true, SHARD, RELOAD_TIMEOUT),
    Snapshots {
        #[serde(default = "hundred")] limit: u64,
        #[serde(default)] before: Option<u64>,
    } => ("list_snapshots", false, BOTH, COMMAND_TIMEOUT),
    RollbackToDay { day: u64 } => ("rollback_to_day", true, ROOM, RELOAD_TIMEOUT),
    RollbackToSnapshot { session_id: String, snapshot_id: u64 }
        => ("rollback_to_snapshot", true, ROOM, RELOAD_TIMEOUT),
    Health {} => ("health", false, BOTH, COMMAND_TIMEOUT),
    Presence {} => ("presence", false, BOTH, COMMAND_TIMEOUT),
    Room {} => ("room", false, SHARD, COMMAND_TIMEOUT),
    World {} => ("world", false, SHARD, COMMAND_TIMEOUT),
    Runtime {} => ("runtime", false, SHARD, COMMAND_TIMEOUT),
    Mods {} => ("mods", false, BOTH, COMMAND_TIMEOUT),
    ConnectedShards {} => ("connected_shards", false, SHARD, COMMAND_TIMEOUT),
    ListPlayers {} => ("list_players", false, BOTH, COMMAND_TIMEOUT),
    GetPlayer { userid: String } => ("get_player", false, BOTH, COMMAND_TIMEOUT),
    Inventory { userid: String } => ("inventory", false, BOTH, COMMAND_TIMEOUT),
    Kick { userid: String } => ("kick", true, BOTH, COMMAND_TIMEOUT),
    Ban { userid: String, #[serde(default)] seconds: Option<u64> }
        => ("ban", true, BOTH, COMMAND_TIMEOUT),
    Blocklist {} => ("blocklist", false, BOTH, COMMAND_TIMEOUT),
    IsBlocked { userid: String } => ("is_blocked", false, BOTH, COMMAND_TIMEOUT),
    Unban { userid: String } => ("unban", true, BOTH, COMMAND_TIMEOUT),
    IsAdmin { userid: String } => ("is_admin", false, BOTH, COMMAND_TIMEOUT),
    ReadPermissions {} => ("read_permissions", false, ROOM, COMMAND_TIMEOUT),
    SetAdmin { userid: String, #[serde(default)] remove: bool }
        => ("set_admin", true, ROOM, COMMAND_TIMEOUT),
    SetVitals {
        userid: String,
        #[serde(default)] health: Option<f64>,
        #[serde(default)] hunger: Option<f64>,
        #[serde(default)] sanity: Option<f64>,
        #[serde(default)] temperature: Option<f64>,
        #[serde(default)] moisture: Option<f64>,
    } => ("set_vitals", true, BOTH, COMMAND_TIMEOUT),
    KillPlayer { userid: String } => ("kill_player", true, BOTH, COMMAND_TIMEOUT),
    Revive { userid: String } => ("revive", true, BOTH, COMMAND_TIMEOUT),
    Despawn { userid: String } => ("despawn", true, BOTH, COMMAND_TIMEOUT),
    Migrate { userid: String, shard_id: String, #[serde(default = "one")] portal_id: u64 }
        => ("migrate", true, BOTH, RELOAD_TIMEOUT),
    Teleport { userid: String, x: f64, y: f64, z: f64 }
        => ("teleport", true, BOTH, COMMAND_TIMEOUT),
    Give { userid: String, item: String, #[serde(default = "one")] count: u64 }
        => ("give", true, BOTH, COMMAND_TIMEOUT),
    Remove { userid: String, item: String, #[serde(default = "one")] count: u64 }
        => ("remove", true, BOTH, COMMAND_TIMEOUT),
    IsWhitelisted { userid: String } => ("is_whitelisted", false, ROOM, COMMAND_TIMEOUT),
    Whitelist { userid: String } => ("whitelist", true, ROOM, COMMAND_TIMEOUT),
    Unwhitelist { userid: String } => ("unwhitelist", true, ROOM, COMMAND_TIMEOUT),
}

impl Request {
    pub fn method(&self) -> &'static str {
        self.metadata().name
    }
    pub fn mutating(&self) -> bool {
        self.metadata().mutation
    }

    pub fn completion_timeout(&self) -> Result<Duration> {
        let base = self.metadata().default_timeout;
        let seconds = match self {
            Self::Announce {
                count, interval, ..
            } => base + count.saturating_sub(1) as f64 * interval,
            Self::Stop { notice } | Self::Restart { notice } | Self::UpdateMods { notice, .. } => {
                base - notice_delay() + notice.as_ref().map_or(0.0, |notice| notice.delay)
            }
            _ => base,
        };
        positive_duration("completion_timeout", seconds)
    }

    pub fn arguments(&self) -> Result<Value> {
        self.validate()?;
        let mut value = serde_json::to_value(self)
            .map_err(|_| Error::new(ErrorCode::Internal, "request serialization failed"))?;
        Ok(value
            .get_mut("arguments")
            .map(Value::take)
            .unwrap_or_else(|| json!({})))
    }

    pub fn userid(&self) -> Option<&str> {
        match self {
            Self::GetPlayer { userid }
            | Self::Inventory { userid }
            | Self::Kick { userid }
            | Self::Ban { userid, .. }
            | Self::IsBlocked { userid }
            | Self::Unban { userid }
            | Self::IsAdmin { userid }
            | Self::SetAdmin { userid, .. }
            | Self::SetVitals { userid, .. }
            | Self::KillPlayer { userid }
            | Self::Revive { userid }
            | Self::Despawn { userid }
            | Self::Migrate { userid, .. }
            | Self::Teleport { userid, .. }
            | Self::Give { userid, .. }
            | Self::Remove { userid, .. }
            | Self::IsWhitelisted { userid }
            | Self::Whitelist { userid }
            | Self::Unwhitelist { userid } => Some(userid),
            _ => None,
        }
    }

    pub fn validate(&self) -> Result<()> {
        if let Some(userid) = self.userid() {
            identifier("userid", userid)?;
        }
        match self {
            Self::ExportArchive {
                options,
                compression_level,
            } => {
                if !options.is_object() {
                    return Err(Error::invalid(
                        "options",
                        "archive options must be an object",
                    ));
                }
                integer("compression_level", *compression_level, 1, 22)?;
            }
            Self::ReleaseArchive { artifact_id } => validate_artifact_id(artifact_id)?,
            Self::Configure { configuration, .. } => {
                if !configuration.is_object() {
                    return Err(Error::invalid(
                        "configuration",
                        "configuration must be an object",
                    ));
                }
            }
            Self::SetPolicy { policy } => {
                if !policy.is_object() {
                    return Err(Error::invalid("policy", "policy must be an object"));
                }
            }
            Self::Stop { notice } | Self::Restart { notice } | Self::UpdateMods { notice, .. } => {
                if let Some(notice) = notice {
                    notice.validate()?;
                }
            }
            Self::Execute { source }
            | Self::ExecuteJson { source }
            | Self::Evaluate { source }
            | Self::ExecuteAll { source } => text("source", source, MAX_SOURCE_BYTES)?,
            Self::Announce {
                message,
                count,
                interval,
            } => {
                text("message", message, MAX_SOURCE_BYTES)?;
                integer("count", *count, 1, MAX_SAFE_INTEGER)?;
                positive_duration("interval", *interval)?;
            }
            Self::Rollback { count } => integer("count", *count, 0, MAX_SAFE_INTEGER)?,
            Self::Regenerate {
                expected_session_id: Some(session),
                ..
            } => identifier("expected_session_id", session)?,
            Self::Snapshots { limit, before } => {
                integer("limit", *limit, 1, 100)?;
                if let Some(before) = before {
                    integer("before", *before, 0, MAX_SAFE_INTEGER)?;
                }
            }
            Self::RollbackToDay { day } => integer("day", *day, 1, MAX_SAFE_INTEGER)?,
            Self::RollbackToSnapshot {
                session_id,
                snapshot_id,
            } => {
                identifier("session_id", session_id)?;
                integer("snapshot_id", *snapshot_id, 1, MAX_SAFE_INTEGER)?;
            }
            Self::Ban {
                seconds: Some(seconds),
                ..
            } => integer("seconds", *seconds, 1, MAX_SAFE_INTEGER)?,
            Self::SetVitals {
                health,
                hunger,
                sanity,
                temperature,
                moisture,
                ..
            } => {
                if [health, hunger, sanity, temperature, moisture]
                    .iter()
                    .all(|value| value.is_none())
                {
                    return Err(Error::invalid(
                        "vitals",
                        "at least one player vital must be supplied",
                    ));
                }
                for (name, value) in [
                    ("health", health),
                    ("hunger", hunger),
                    ("sanity", sanity),
                    ("moisture", moisture),
                ] {
                    if value
                        .is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value))
                    {
                        return Err(Error::invalid(
                            name,
                            "player vital must be finite and between 0 and 1",
                        ));
                    }
                }
                if let Some(value) = temperature {
                    finite("temperature", *value)?;
                }
            }
            Self::Migrate {
                shard_id,
                portal_id,
                ..
            } => {
                identifier("shard_id", shard_id)?;
                integer("portal_id", *portal_id, 1, MAX_SAFE_INTEGER)?;
            }
            Self::Teleport { x, y, z, .. } => {
                for (name, value) in [("x", x), ("y", y), ("z", z)] {
                    finite(name, *value)?;
                }
            }
            Self::Give { item, count, .. } | Self::Remove { item, count, .. } => {
                text("item", item, MAX_SOURCE_BYTES)?;
                integer(
                    "count",
                    *count,
                    1,
                    if matches!(self, Self::Give { .. }) {
                        64
                    } else {
                        MAX_SAFE_INTEGER
                    },
                )?;
            }
            _ => {}
        }
        Ok(())
    }
}

impl Method {
    pub fn description(self) -> MethodDescription {
        let properties: serde_json::Map<String, Value> = self
            .arguments
            .iter()
            .map(|(name, kind, _)| ((*name).to_owned(), argument_schema(self.name, name, kind)))
            .collect();
        let required: Vec<_> = self
            .arguments
            .iter()
            .filter(|(_, kind, attributes)| {
                !kind.starts_with("Option") && !attributes.contains("default")
            })
            .map(|(name, _, _)| *name)
            .collect();
        MethodDescription {
            name: self.name.to_owned(),
            mutation: self.mutation,
            scopes: self.scopes.to_vec(),
            default_timeout: self.default_timeout,
            arguments_schema: json!({"type": "object", "properties": properties, "required": required, "additionalProperties": false}),
        }
    }
}

pub fn describe(scope: Scope) -> Vec<MethodDescription> {
    METHODS
        .iter()
        .filter(|method| method.scopes.contains(&scope))
        .map(|method| method.description())
        .collect()
}

fn argument_schema(method: &str, name: &str, kind: &str) -> Value {
    let mut schema = if kind.contains("Countdown") {
        json!({"type": "object", "required": ["template"], "additionalProperties": false, "properties": {
            "template": {"type": "string", "minLength": 1},
            "delay": {"type": "number", "minimum": 0, "default": 60},
            "interval": {"type": "number", "exclusiveMinimum": 0, "default": 30},
            "parameters": {"type": "object", "additionalProperties": {"type": ["string", "number"]}}
        }})
    } else if kind == "Value" {
        json!({"type": "object"})
    } else if kind.contains("String") {
        let max = if ["userid", "shard_id", "session_id", "expected_session_id"].contains(&name) {
            128
        } else {
            MAX_SOURCE_BYTES
        };
        json!({"type": "string", "minLength": 1, "maxLength": max})
    } else if kind.contains("u64") {
        let maximum = match name {
            "limit" => 100,
            "compression_level" => 22,
            "count" if method == "give" => 64,
            _ => MAX_SAFE_INTEGER,
        };
        let minimum = u64::from(name != "before" && !(name == "count" && method == "rollback"));
        json!({"type": "integer", "minimum": minimum, "maximum": maximum})
    } else if kind.contains("bool") {
        json!({"type": "boolean"})
    } else {
        let mut schema = json!({"type": "number"});
        if ["health", "hunger", "sanity", "moisture"].contains(&name) {
            schema["minimum"] = json!(0);
            schema["maximum"] = json!(1);
        } else if name == "interval" {
            schema["exclusiveMinimum"] = json!(0);
        }
        schema
    };
    if kind.starts_with("Option") {
        schema = json!({"anyOf": [schema, {"type": "null"}]});
    }
    match name {
        "count" | "portal_id" => schema["default"] = json!(1),
        "limit" => schema["default"] = json!(100),
        "compression_level" => schema["default"] = json!(3),
        "options" => schema["default"] = json!({}),
        "interval" => schema["default"] = json!(30),
        "preserve_settings" => schema["default"] = json!(true),
        "restart" | "replace_permissions" => schema["default"] = json!(false),
        "notice" => {
            schema["default"] = match method {
                "stop" => json!(shutdown_notice()),
                "restart" => json!(restart_notice()),
                _ => json!(mod_notice()),
            }
        }
        _ => {}
    }
    schema
}

/// `timeout` limits the caller's wait; an accepted mutation continues in the Agent.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "UncheckedEnvelope")]
pub struct Envelope {
    pub target: Target,
    pub request: Request,
    pub timeout: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UncheckedEnvelope {
    target: Target,
    request: Request,
    #[serde(default)]
    timeout: Option<f64>,
}

impl TryFrom<UncheckedEnvelope> for Envelope {
    type Error = Error;
    fn try_from(value: UncheckedEnvelope) -> Result<Self> {
        let envelope = Self {
            target: value.target,
            request: value.request,
            timeout: value.timeout,
        };
        envelope.validate()?;
        Ok(envelope)
    }
}

impl Envelope {
    pub fn new(target: Target, request: Request) -> Result<Self> {
        let value = Self {
            target,
            request,
            timeout: None,
        };
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<()> {
        self.target.validate()?;
        self.request.validate()?;
        if !self
            .request
            .metadata()
            .scopes
            .contains(&self.target.scope())
        {
            return Err(Error::new(
                ErrorCode::Unsupported,
                "method is unavailable at the requested scope",
            )
            .with_details(json!({"method": self.request.method(), "target": self.target})));
        }
        self.timeout()?;
        self.request.completion_timeout()?;
        Ok(())
    }

    pub fn timeout(&self) -> Result<Duration> {
        match self.timeout {
            Some(timeout) => positive_duration("timeout", timeout),
            None => self.request.completion_timeout(),
        }
    }

    pub fn from_json(bytes: &[u8]) -> Result<Self> {
        let value: UncheckedEnvelope = parse_json(bytes)?;
        Self::try_from(value)
    }
}

/// Decode external JSON before a Value map can discard duplicate object keys.
pub(crate) fn parse_json<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    if bytes.len() > MAX_REQUEST_BYTES {
        return Err(Error::invalid("request", "request exceeds 1 MiB"));
    }
    serde_json::from_slice::<JsonStructure>(bytes).map_err(json_error)?;
    serde_json::from_slice(bytes).map_err(json_error)
}

fn json_error(error: serde_json::Error) -> Error {
    Error::invalid(
        "request",
        format!(
            "invalid request JSON at line {}, column {}",
            error.line(),
            error.column()
        ),
    )
}

struct JsonStructure;

impl<'de> Deserialize<'de> for JsonStructure {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = JsonStructure;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("JSON with unique object keys")
            }
            fn visit_bool<E: serde::de::Error>(
                self,
                _: bool,
            ) -> std::result::Result<Self::Value, E> {
                Ok(JsonStructure)
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> std::result::Result<Self::Value, E> {
                Ok(JsonStructure)
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> std::result::Result<Self::Value, E> {
                Ok(JsonStructure)
            }
            fn visit_f64<E: serde::de::Error>(
                self,
                value: f64,
            ) -> std::result::Result<Self::Value, E> {
                if value.is_finite() {
                    Ok(JsonStructure)
                } else {
                    Err(E::custom("non-finite JSON number"))
                }
            }
            fn visit_str<E: serde::de::Error>(
                self,
                _: &str,
            ) -> std::result::Result<Self::Value, E> {
                Ok(JsonStructure)
            }
            fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<Self::Value, E> {
                Ok(JsonStructure)
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut values: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                while values.next_element::<JsonStructure>()?.is_some() {}
                Ok(JsonStructure)
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut values: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut names = HashSet::new();
                while let Some(name) = values.next_key::<String>()? {
                    if !names.insert(name) {
                        return Err(serde::de::Error::custom("duplicate JSON key"));
                    }
                    values.next_value::<JsonStructure>()?;
                }
                Ok(JsonStructure)
            }
        }
        deserializer.deserialize_any(Visitor)
    }
}

pub fn positive_duration(field: &str, value: f64) -> Result<Duration> {
    if !value.is_finite() || value <= 0.0 {
        return Err(Error::invalid(
            field,
            "duration must be positive and finite",
        ));
    }
    let duration = checked_duration(field, value)?;
    if duration.is_zero() {
        return Err(Error::invalid(
            field,
            "duration must be at least one nanosecond",
        ));
    }
    Ok(duration)
}

fn finite(field: &str, value: f64) -> Result<()> {
    if value.is_finite() {
        Ok(())
    } else {
        Err(Error::invalid(field, "number must be finite"))
    }
}

fn nonnegative(field: &str, value: f64) -> Result<()> {
    finite(field, value)?;
    if value < 0.0 {
        return Err(Error::invalid(field, "number cannot be negative"));
    }
    checked_duration(field, value).map(|_| ())
}

fn checked_duration(field: &str, value: f64) -> Result<Duration> {
    Duration::try_from_secs_f64(value)
        .ok()
        .filter(|duration| std::time::Instant::now().checked_add(*duration).is_some())
        .ok_or_else(|| Error::invalid(field, "duration is out of range"))
}

fn integer(field: &str, value: u64, minimum: u64, maximum: u64) -> Result<()> {
    if (minimum..=maximum).contains(&value) {
        Ok(())
    } else {
        Err(Error::invalid(
            field,
            format!("integer must be between {minimum} and {maximum}"),
        ))
    }
}

fn text(field: &str, value: &str, maximum_bytes: usize) -> Result<()> {
    if value.is_empty() || value.len() > maximum_bytes || value.contains('\0') {
        return Err(Error::invalid(
            field,
            "text is empty, contains NUL, or exceeds the byte limit",
        ));
    }
    Ok(())
}

fn identifier(field: &str, value: &str) -> Result<()> {
    if value.is_empty() || value.chars().count() > 128 || value.chars().any(char::is_control) {
        return Err(Error::invalid(
            field,
            "identifier must contain 1 to 128 characters without control characters",
        ));
    }
    Ok(())
}

pub(crate) fn validate_artifact_id(value: &str) -> Result<()> {
    if value.len() != 26
        || value.as_bytes()[0] > b'7'
        || !value
            .bytes()
            .all(|byte| b"0123456789ABCDEFGHJKMNPQRSTVWXYZ".contains(&byte))
        || value.bytes().all(|byte| byte == b'0')
    {
        return Err(Error::invalid(
            "artifact_id",
            "archive artifact ID must be a canonical nonzero ULID",
        ));
    }
    Ok(())
}

fn validate_shard_name(name: &str) -> Result<()> {
    if name.trim().is_empty()
        || name.len() > 255
        || name.starts_with('.')
        || name.contains(['/', '\\', '\0', '\r', '\n'])
        || [
            "console",
            "mods",
            "cluster.ini",
            "cluster_token.txt",
            "adminlist.txt",
            "whitelist.txt",
            "blocklist.txt",
        ]
        .contains(&name.to_lowercase().as_str())
    {
        return Err(Error::invalid(
            "target.shard",
            "unsafe shard directory name",
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum Outcome<T> {
    Success { value: T },
    Failure { error: Error },
}

impl<T> From<Result<T>> for Outcome<T> {
    fn from(result: Result<T>) -> Self {
        match result {
            Ok(value) => Self::Success { value },
            Err(error) => Self::Failure { error },
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShardResult<T = Value> {
    pub shard: String,
    pub result: Outcome<T>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationStatus {
    pub id: String,
    pub method: String,
    pub target: Target,
    pub started_at_ns: u64,
    pub completed_at_ns: Option<u64>,
    pub result: Option<Outcome<Value>>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationHistory {
    pub current: Option<OperationStatus>,
    pub last: Option<OperationStatus>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionIdentity {
    pub session_id: String,
    /// The process driver's nonce; Lua generation alone can restart at zero.
    pub process_id: String,
    pub generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EntityRef {
    pub shard: String,
    pub identity: SessionIdentity,
    pub guid: u64,
}

impl EntityRef {
    pub fn validate_current(&self, shard: &str, identity: &SessionIdentity) -> Result<()> {
        validate_shard_name(&self.shard)?;
        integer("guid", self.guid, 1, MAX_SAFE_INTEGER)?;
        if self.shard != shard || &self.identity != identity {
            return Err(Error::new(
                ErrorCode::StaleReference,
                "entity reference belongs to another world or process generation",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Stopped,
    Preparing,
    Starting,
    Running,
    Stopping,
    Failed,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Readiness {
    pub process_running: bool,
    pub world_loaded: bool,
    pub control_ready: bool,
    pub missing_shards: Vec<String>,
}

impl Readiness {
    pub fn ready(&self) -> bool {
        self.process_running
            && self.world_loaded
            && self.control_ready
            && self.missing_shards.is_empty()
    }

    pub fn require(&self, shard: &str) -> Result<()> {
        let reason = if !self.process_running {
            "game process is stopped"
        } else if !self.world_loaded {
            "world is still loading"
        } else if !self.control_ready {
            "required game control is unavailable"
        } else if !self.missing_shards.is_empty() {
            "required shard connections are missing"
        } else {
            return Ok(());
        };
        Err(Error::new(ErrorCode::NotReady, reason)
            .with_details(json!({"shard": shard, "readiness": self})))
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShardStatus {
    pub name: String,
    pub is_master: bool,
    pub phase: Phase,
    pub pid: Option<u32>,
    pub identity: Option<SessionIdentity>,
    pub readiness: Readiness,
    pub error: Option<Error>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoomStatus {
    pub phase: Phase,
    pub master: String,
    pub shards: Vec<ShardStatus>,
    pub operations: OperationHistory,
    pub error: Option<Error>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlayerState {
    Loading,
    Active,
    Migrating,
    Disconnected,
    Conflict,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlayerLocation {
    pub userid: String,
    pub shard: Option<String>,
    pub identity: Option<SessionIdentity>,
    pub state: PlayerState,
    pub player: Value,
}

impl PlayerLocation {
    pub fn require_active(&self) -> Result<(&str, &SessionIdentity)> {
        identifier("userid", &self.userid)?;
        let (code, message) = match self.state {
            PlayerState::Loading => (ErrorCode::NotReady, "player character has not loaded"),
            PlayerState::Migrating => (ErrorCode::NotReady, "player is migrating between shards"),
            PlayerState::Disconnected => (ErrorCode::NotFound, "player is disconnected"),
            PlayerState::Conflict => (ErrorCode::Conflict, "player is active on multiple shards"),
            PlayerState::Active => {
                if let (Some(shard), Some(identity)) = (&self.shard, &self.identity) {
                    return Ok((shard, identity));
                }
                (
                    ErrorCode::NotReady,
                    "player location has no confirmed world identity",
                )
            }
        };
        Err(Error::new(code, message)
            .with_details(json!({"userid": self.userid, "state": self.state})))
    }
}

/// Emitted only after the matching final native save callback.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SavedSnapshot {
    pub identity: SessionIdentity,
    pub snapshot_id: u64,
    pub save_id: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SaveResult {
    pub shards: Vec<ShardResult<SavedSnapshot>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShardStopped {
    /// Exit status, or the negative signal number for signal termination.
    pub returncode: Option<i32>,
    pub forced: bool,
    pub output_drained: bool,
    pub saved_snapshot: Option<SavedSnapshot>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StopResult {
    pub shards: Vec<ShardResult<ShardStopped>>,
}
