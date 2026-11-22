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
pub const RELOAD_TIMEOUT: f64 = 900.0;
pub const STOP_TIMEOUT: f64 = 120.0;
pub const OUTPUT_DRAIN_TIMEOUT: f64 = 30.0;
pub const RESTART_TIMEOUT: f64 = 1210.0;
pub(crate) const NOTICE_SEND_TIMEOUT: Duration = Duration::from_secs(5);

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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, thiserror::Error)]
#[error("{message}")]
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

    pub(crate) fn shutdown(code: ErrorCode) -> Self {
        Self::new(code, "room shutdown is in progress").with_details(json!({"reason":"shutdown"}))
    }

    pub(crate) fn is_shutdown(&self) -> bool {
        matches!(self.code, ErrorCode::Unknown | ErrorCode::NotReady)
            && self.details["reason"] == "shutdown"
    }
}

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

pub(crate) fn shutdown_notice() -> Option<Countdown> {
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

mod schema;

#[derive(Debug, Clone, Copy)]
pub struct Method {
    pub name: &'static str,
    pub mutation: bool,
    pub scopes: &'static [Scope],
    pub default_timeout: f64,
    arguments: &'static [schema::Argument],
    alternatives: &'static [&'static str],
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
    ($($variant:ident { $($field:ident: $kind:ty $(= $default:expr)?),* $(,)? }
        $(require_any($label:literal, $message:literal; $($alternative:ident),+))?
        => ($name:literal, $mutation:expr, $scopes:expr, $timeout:expr)),* $(,)?) => {
        #[derive(Debug, Clone, PartialEq, Serialize)]
        #[serde(tag = "method", content = "arguments")]
        pub enum Request {
            $(#[serde(rename = $name)] $variant { $( $field: $kind ),* }),*
        }

        #[repr(usize)]
        enum MethodIndex { $($variant),* }

        pub const METHODS: &[Method] = &[$(Method {
            name: $name,
            mutation: $mutation,
            scopes: $scopes,
            default_timeout: $timeout,
            arguments: &[$(schema::Argument {
                name: stringify!($field),
                describe: <$kind as schema::ArgumentType>::schema,
                optional: <$kind as schema::ArgumentType>::OPTIONAL,
                default: requests!(@default $kind $(, $default)?),
            }),*],
            alternatives: &[$($(stringify!($alternative)),+)?],
        }),*];

        impl<'de> Deserialize<'de> for Request {
            fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
                let mut wire = WireRequest::deserialize(deserializer)?;
                // Decode the payload directly to retain arbitrary-precision JSON numbers.
                match wire.method.as_str() {
                    $($name => {
                        #[derive(Deserialize)]
                        #[serde(deny_unknown_fields)]
                        struct Arguments { $($field: $kind),* }
                        METHODS[MethodIndex::$variant as usize].apply_defaults(&mut wire.arguments)
                            .map_err(serde::de::Error::custom)?;
                        let Arguments { $($field),* } = serde_json::from_value(wire.arguments)
                            .map_err(serde::de::Error::custom)?;
                        Ok(Self::$variant { $($field),* })
                    }),*
                    _ => Err(serde::de::Error::custom("unknown request method")),
                }
            }
        }

        impl Request {
            pub fn metadata(&self) -> Method {
                match self { $(Self::$variant { .. } => METHODS[MethodIndex::$variant as usize]),* }
            }

            pub fn validate(&self) -> Result<()> {
                match self {
                    $(Self::$variant { $($field),* } => {
                        $(if !($(schema::Supplied::supplied($alternative))||+) {
                            return Err(Error::invalid($label, $message));
                        })?
                        $(schema::ArgumentType::check($field, $name, stringify!($field))?;)*
                        Ok(())
                    }),*
                }
            }
        }
    };
    (@default $kind:ty, $default:expr) => { Some(|| {
        let value: $kind = $default;
        json!(value)
    }) };
    (@default $kind:ty) => { None };
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
    Start {} => ("start", true, ROOM, START_TIMEOUT),
    Stop { notice: Option<Countdown> = shutdown_notice() }
        => ("stop", true, ROOM, 230.0),
    Restart { notice: Option<Countdown> = restart_notice() }
        => ("restart", true, ROOM, RESTART_TIMEOUT),
    Kill {} => ("kill", true, ROOM, STOP_TIMEOUT + OUTPUT_DRAIN_TIMEOUT),
    UpdateMods {
        restart: bool = Default::default(),
        notice: Option<Countdown> = mod_notice(),
    } => ("update_mods", true, ROOM, 7210.0),
    ReadConfiguration {} => ("read_configuration", false, ROOM, COMMAND_TIMEOUT),
    Configure { configuration: Value, replace_permissions: bool = Default::default() }
        => ("configure", true, ROOM, COMMAND_TIMEOUT),
    EditConfiguration {
        changes: Vec<(String, Value)> = Default::default(),
        unset: Vec<String> = Default::default(),
    } require_any("changes", "provide changes or unset fields"; changes, unset)
        => ("edit_configuration", true, ROOM, COMMAND_TIMEOUT),
    SetPolicy { policy: Value } => ("set_policy", true, ROOM, COMMAND_TIMEOUT),
    Execute { source: String } => ("execute", true, SHARD, COMMAND_TIMEOUT),
    ExecuteJson { source: String } => ("execute_json", true, SHARD, COMMAND_TIMEOUT),
    Evaluate { source: String } => ("evaluate", true, SHARD, COMMAND_TIMEOUT),
    ExecuteAll { source: String } => ("execute_all", true, ROOM, COMMAND_TIMEOUT),
    Announce {
        message: String,
        count: u64 = one(),
        interval: f64 = notice_interval(),
    } => ("announce", true, ROOM, COMMAND_TIMEOUT),
    Save {} => ("save", true, ROOM, COMMAND_TIMEOUT),
    Pause { paused: bool } => ("pause", true, BOTH, COMMAND_TIMEOUT),
    Reset {} => ("reset", true, ROOM, RELOAD_TIMEOUT),
    Rollback { count: u64 = one() }
        => ("rollback", true, ROOM, RELOAD_TIMEOUT),
    Regenerate {
        expected_session_id: Option<String>,
        require_empty: Option<bool>,
    } => ("regenerate", true, ROOM, RELOAD_TIMEOUT),
    RegenerateShard { preserve_settings: bool = yes() }
        => ("regenerate_shard", true, SHARD, RELOAD_TIMEOUT),
    Snapshots {
        limit: u64 = hundred(),
        before: Option<u64>,
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
    Kick { userid: String } => ("kick", true, ROOM, COMMAND_TIMEOUT),
    Ban { userid: String, seconds: Option<u64> }
        => ("ban", true, ROOM, COMMAND_TIMEOUT),
    Blocklist {} => ("blocklist", false, ROOM, COMMAND_TIMEOUT),
    IsBlocked { userid: String } => ("is_blocked", false, ROOM, COMMAND_TIMEOUT),
    Unban { userid: String } => ("unban", true, ROOM, COMMAND_TIMEOUT),
    IsAdmin { userid: String } => ("is_admin", false, ROOM, COMMAND_TIMEOUT),
    ReadPermissions {} => ("read_permissions", false, ROOM, COMMAND_TIMEOUT),
    SetAdmin { userid: String, enabled: bool }
        => ("set_admin", true, ROOM, COMMAND_TIMEOUT),
    SetVitals {
        userid: String,
        health: Option<f64>,
        hunger: Option<f64>,
        sanity: Option<f64>,
        temperature: Option<f64>,
        moisture: Option<f64>,
    } require_any("vitals", "at least one player vital must be supplied"; health, hunger, sanity, temperature, moisture)
        => ("set_vitals", true, BOTH, COMMAND_TIMEOUT),
    KillPlayer { userid: String } => ("kill_player", true, BOTH, COMMAND_TIMEOUT),
    Revive { userid: String } => ("revive", true, BOTH, COMMAND_TIMEOUT),
    Despawn { userid: String } => ("despawn", true, BOTH, COMMAND_TIMEOUT),
    Migrate { userid: String, shard_id: String, portal_id: u64 = one() }
        => ("migrate", true, BOTH, COMMAND_TIMEOUT),
    Teleport { userid: String, x: f64, y: f64, z: f64 }
        => ("teleport", true, BOTH, COMMAND_TIMEOUT),
    Give { userid: String, item: String, count: u64 = one() }
        => ("give", true, BOTH, COMMAND_TIMEOUT),
    Remove { userid: String, item: String, count: u64 = one() }
        => ("remove", true, BOTH, COMMAND_TIMEOUT),
    IsWhitelisted { userid: String } => ("is_whitelisted", false, ROOM, COMMAND_TIMEOUT),
    Whitelist { userid: String } => ("whitelist", true, ROOM, COMMAND_TIMEOUT),
    Unwhitelist { userid: String } => ("unwhitelist", true, ROOM, COMMAND_TIMEOUT),
    VoteCommands {} => ("list_vote_commands", false, ROOM, COMMAND_TIMEOUT),
    GetVote {} => ("get_vote", false, ROOM, COMMAND_TIMEOUT),
    StartVote {
        command: String,
        starter_userid: String,
        target_userid: Option<String>,
    } => ("start_vote", true, ROOM, COMMAND_TIMEOUT),
    CancelVote {} => ("cancel_vote", true, ROOM, COMMAND_TIMEOUT),
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
                base - notice_delay() - 10.0
                    + notice.as_ref().map_or(0.0, |notice| notice.delay + 10.0)
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
}

pub fn describe(scope: Scope) -> Vec<MethodDescription> {
    METHODS
        .iter()
        .filter(|method| method.scopes.contains(&scope))
        .map(|method| method.description())
        .collect()
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
    parse_unique_json(bytes).map_err(json_error)
}

pub(crate) fn parse_unique_json<T: serde::de::DeserializeOwned>(
    bytes: &[u8],
) -> serde_json::Result<T> {
    serde_json::from_slice::<JsonStructure>(bytes)?;
    serde_json::from_slice(bytes)
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

pub(crate) fn identifier(field: &str, value: &str) -> Result<()> {
    if value.is_empty() || value.chars().count() > 128 || value.chars().any(char::is_control) {
        return Err(Error::invalid(
            field,
            "identifier must contain 1 to 128 characters without control characters",
        ));
    }
    Ok(())
}

fn validate_shard_name(name: &str) -> Result<()> {
    crate::configuration::validate_shard_name(name)
        .map_err(|error| Error::invalid("target.shard", error.to_string()))
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
    pub error: Option<ErrorCode>,
}

impl OperationStatus {
    pub(crate) fn complete(&mut self, result: &Result<Value>) {
        self.completed_at_ns = Some(crate::operation::timestamp());
        self.error = result.as_ref().err().map(|error| error.code);
    }
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
    /// The room attempt nonce and process ID; Lua generation alone can restart at zero.
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
    Departing,
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
            PlayerState::Departing => (
                ErrorCode::NotReady,
                "player is leaving the current character",
            ),
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

pub type ShardStopped = crate::process::ExitSummary;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StopResult {
    pub force_requested: bool,
    pub shards: Vec<ShardResult<ShardStopped>>,
}
