//! Sparse, validated native configuration. The bundled schemas contain data only.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value, json};

use crate::{
    configuration,
    files::{self, FileChanges, RoomLock, StoppedRoom},
    lua,
};

static SCHEMAS: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../resources/settings/schemas.json"))
        .expect("bundled configuration schemas")
});
static DEFAULTS: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../resources/settings/defaults.json"))
        .expect("bundled configuration defaults")
});
static INI: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../resources/settings/ini.json"))
        .expect("bundled INI metadata")
});
static VALIDATORS: LazyLock<BTreeMap<String, jsonschema::Validator>> = LazyLock::new(|| {
    SCHEMAS
        .as_object()
        .expect("schema map")
        .iter()
        .map(|(name, schema)| {
            (
                name.clone(),
                jsonschema::options()
                    .should_validate_formats(true)
                    .build(schema)
                    .expect("valid bundled schema"),
            )
        })
        .collect()
});

pub const PERMISSION_FIELDS: [&str; 3] = ["adminlist", "whitelist", "blocklist"];

/// Configuration schemas are embedded and self-contained; input cannot supply references.
pub fn schema(name: &str) -> Result<&'static Value> {
    SCHEMAS.get(name).context("unknown configuration schema")
}

fn object(value: &Value) -> Result<&Map<String, Value>> {
    value.as_object().context("configuration must be an object")
}

pub fn validate(name: &str, value: &Value) -> Result<()> {
    let mut stack = vec![(value, 0)];
    let mut nodes = 0;
    while let Some((value, depth)) = stack.pop() {
        nodes += 1;
        ensure!(
            depth <= lua::MAX_NESTING && nodes <= lua::MAX_TOKENS,
            "configuration exceeds its complexity limit"
        );
        match value {
            Value::Object(fields) => stack.extend(fields.values().map(|value| (value, depth + 1))),
            Value::Array(items) => stack.extend(items.iter().map(|value| (value, depth + 1))),
            _ => {}
        }
    }
    ensure!(
        serde_json::to_vec(value)?.len() <= files::MAX_TRANSACTION_BYTES,
        "configuration exceeds the byte limit"
    );
    let validator = VALIDATORS
        .get(name)
        .context("unknown configuration schema")?;
    if let Err(error) = validator.validate(value) {
        bail!("invalid {name} at {}", error.instance_path());
    }
    validate_relations(name, value)
}

/// Resolve defaults without changing which fields the input explicitly supplied.
pub fn resolved(name: &str, sparse: &Value) -> Value {
    let mut value = DEFAULTS.get(name).cloned().unwrap_or_else(|| json!({}));
    if let Some(properties) = SCHEMAS[name]["properties"].as_object() {
        for (key, property) in properties {
            if value.get(key).is_none()
                && let Some(default) = property.get("default")
            {
                value[key] = default.clone();
            }
        }
    }
    if let Some(fields) = sparse.as_object() {
        value
            .as_object_mut()
            .expect("object default")
            .extend(fields.clone());
    }
    match name {
        "ClusterConfig" | "RoomPreset" => {
            value["settings"] = resolved("ClusterSettings", &value["settings"]);
            if value.get("shards").is_none() {
                value["shards"] = json!({});
            }
            for shard in value["shards"]
                .as_object_mut()
                .expect("validated shards")
                .values_mut()
            {
                *shard = resolved("ShardConfig", shard);
            }
            if name == "ClusterConfig" {
                value["downloads"] = resolved("WorkshopDownloads", &value["downloads"]);
                value["mod_settings"] = resolved("ModSettings", &value["mod_settings"]);
            } else {
                value["mods"] = resolved("ModOverrides", &value["mods"]);
            }
        }
        "ShardConfig" => {
            value["settings"] = resolved("ShardSettings", &value["settings"]);
            value["mods"] = resolved("ModOverrides", &value["mods"]);
            for (key, model) in [
                ("world", "WorldgenOverride"),
                ("level", "LevelDataOverride"),
            ] {
                if value.get(key).is_some_and(|v| !v.is_null()) {
                    value[key] = resolved(model, &value[key]);
                }
            }
        }
        "WorldgenOverride" | "LevelDataOverride" => {
            if let Some(kind) = value["overrides"]["kind"].as_str().map(str::to_owned)
                && let Some(model) = world_model(&kind)
            {
                value["overrides"]["values"] = resolved(model, &value["overrides"]["values"]);
            }
        }
        _ => {}
    }
    value
}

pub fn redact(mut value: Value) -> Value {
    fn walk(value: &mut Value) {
        match value {
            Value::Object(fields) => {
                for (key, value) in fields {
                    if ["token", "cluster_key", "cluster_password"].contains(&key.as_str())
                        && !value.is_null()
                    {
                        *value = Value::String("**********".into());
                    } else {
                        walk(value);
                    }
                }
            }
            Value::Array(items) => {
                for value in items {
                    walk(value);
                }
            }
            _ => {}
        }
    }
    walk(&mut value);
    value
}

macro_rules! configuration_value {
    ($($name:ident),+ $(,)?) => {$(
        #[derive(Clone, PartialEq)]
        pub struct $name(Value);
        impl $name {
            pub fn from_value(value: Value) -> Result<Self> {
                validate(stringify!($name), &value)?;
                Ok(Self(value))
            }
            pub fn as_value(&self) -> &Value { &self.0 }
            pub fn into_value(self) -> Value { self.0 }
            pub fn resolved(&self) -> Value { resolved(stringify!($name), &self.0) }
            pub fn schema() -> &'static Value { &SCHEMAS[stringify!($name)] }
        }
        impl std::fmt::Debug for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { redact(self.0.clone()).fmt(f) }
        }
        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> { redact(self.0.clone()).serialize(serializer) }
        }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
                Self::from_value(Value::deserialize(deserializer)?).map_err(serde::de::Error::custom)
            }
        }
    )+};
}

configuration_value!(
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
    RoomPreset,
    WorldgenOverride,
    LevelDataOverride,
    ModOverrides,
    ModSettings,
    WorkshopDownloads
);

fn world_model(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "world" => "WorldOverrides",
        "forest" => "ForestOverrides",
        "cave" => "CaveOverrides",
        "quagmire" => "QuagmireOverrides",
        "lavaarena" => "LavaArenaOverrides",
        "custom" => "CustomWorldOverrides",
        "loaded_forest" => "_LoadedForestOverrides",
        "loaded_cave" => "_LoadedCaveOverrides",
        _ => return None,
    })
}

fn preset_kind(preset: &str) -> Option<&'static str> {
    match preset.to_ascii_uppercase().as_str() {
        "SURVIVAL_TOGETHER"
        | "RELAXED"
        | "ENDLESS"
        | "WILDERNESS"
        | "LIGHTS_OUT"
        | "COMPLETE_DARKNESS"
        | "TERRARIA"
        | "SURVIVAL_TOGETHER_CLASSIC"
        | "SURVIVAL_DEFAULT_PLUS"
        | "MOD_MISSING" => Some("forest"),
        "DST_CAVE" | "DST_CAVE_PLUS" | "TERRARIA_CAVE" => Some("cave"),
        "QUAGMIRE" => Some("quagmire"),
        "LAVAARENA" => Some("lavaarena"),
        _ => None,
    }
}

fn validate_relations(name: &str, value: &Value) -> Result<()> {
    let current = resolved(name, value);
    match name {
        "ClusterSettings" | "ShardSettings" => {
            for (field, item) in object(&current)? {
                if !item.is_null()
                    && (DEFAULTS[name][field].is_number()
                        || (name == "ShardSettings"
                            && ["id", "master_port"].contains(&field.as_str())))
                {
                    ensure!(
                        item.as_u64().is_some(),
                        "{name}.{field} must be an unsigned integer"
                    );
                }
            }
            for value in object(&current)?.values().filter_map(Value::as_str) {
                ensure!(
                    !value.contains(['\0', '\r', '\n']),
                    "INI strings cannot contain NUL, CR or LF"
                );
            }
            if let Some(host) = current["master_ip"].as_str() {
                configuration::validate_host(host)?;
            }
            if name == "ClusterSettings" {
                ensure!(
                    !(current["steam_group_only"] == true || current["steam_group_admins"] == true)
                        || current["steam_group_id"].as_u64().unwrap_or(0) != 0,
                    "Steam group restrictions require steam_group_id"
                );
                ensure!(
                    current["whitelist_slots"].as_u64() <= current["max_players"].as_u64(),
                    "whitelist_slots exceeds max_players"
                );
            } else if let Some(id) = current["id"].as_u64() {
                ensure!(
                    (current["is_master"] == true && id == 1)
                        || (current["is_master"] == false && id >= 2),
                    "master shard id must be 1; secondary shard ids must be at least 2"
                );
            }
        }
        "ClusterConfig" => {
            validate_relations("ClusterSettings", &current["settings"])?;
            ensure!(
                current["token"]
                    .as_str()
                    .unwrap_or("")
                    .bytes()
                    .all(|byte| (b'!'..=b'~').contains(&byte)),
                "cluster token must be printable non-space ASCII"
            );
            for field in PERMISSION_FIELDS {
                ensure!(
                    !current[field].as_str().unwrap_or("").contains(['\0', '\r']),
                    "permission lists cannot contain NUL or CR"
                );
            }
            validate_relations("ModSettings", &current["mod_settings"])?;
            validate_relations("WorkshopDownloads", &current["downloads"])?;
            for shard in object(&value["shards"])?.values() {
                validate_relations("ShardConfig", shard)?;
            }
            validate_topology(value, &current)?;
        }
        "RoomPreset" => {
            validate_relations("ClusterSettings", &current["settings"])?;
            validate_relations("ModOverrides", &current["mods"])?;
            for (name, shard) in value["shards"].as_object().into_iter().flatten() {
                configuration::validate_shard_name(name)?;
                validate_relations("ShardConfig", shard)?;
            }
        }
        "ShardConfig" => {
            validate_relations("ShardSettings", &current["settings"])?;
            validate_relations("ModOverrides", &current["mods"])?;
            for (field, model) in [
                ("world", "WorldgenOverride"),
                ("level", "LevelDataOverride"),
            ] {
                if !value[field].is_null() {
                    validate_relations(model, &value[field])?;
                }
            }
        }
        "WorldgenOverride" => {
            let kinds: BTreeSet<_> = ["worldgen_preset", "settings_preset"]
                .iter()
                .filter_map(|key| current[*key].as_str().and_then(preset_kind))
                .collect();
            ensure!(
                kinds.len() <= 1,
                "different built-in world types cannot be combined"
            );
            let kind = current["overrides"]["kind"]
                .as_str()
                .unwrap_or("world")
                .trim_start_matches("loaded_");
            if let Some(expected) = kinds.first() {
                ensure!(
                    !matches!(*expected, "quagmire" | "lavaarena") || kind == *expected,
                    "event presets require matching overrides"
                );
                ensure!(
                    !matches!(kind, "forest" | "cave" | "quagmire" | "lavaarena")
                        || kind == *expected,
                    "preset and overrides use different world types"
                );
            }
            validate_world_values(&current["overrides"]["values"])?;
        }
        "LevelDataOverride" => {
            ensure!(
                value["overrides"]["values"]["task_set"]
                    .as_str()
                    .is_some_and(|value| !value.is_empty()),
                "level data requires explicit task_set"
            );
            validate_level(&current)?;
        }
        "WorkshopDownloads" => {
            for field in ["items", "collections"] {
                for id in current[field]
                    .as_array()
                    .context("Workshop ids must be an array")?
                {
                    ensure!(
                        id.as_u64().is_some_and(|id| id > 0),
                        "Workshop ids must be positive uint64 integers"
                    );
                }
            }
        }
        "ModOverrides" => {
            for (name, entry) in object(&current["entries"])? {
                validate_mod_name(name)?;
                if !entry["configuration_options"].is_null() {
                    lua::render_literal(&entry["configuration_options"])?;
                }
            }
        }
        "ModSettings" => {
            for name in current["force_enabled"]
                .as_array()
                .context("force_enabled must be an array")?
                .iter()
                .filter_map(Value::as_str)
            {
                validate_mod_name(name)?;
                static LUA_NUMBER: LazyLock<regex::Regex> = LazyLock::new(|| {
                    regex::Regex::new(r"(?i)^\s*[+-]?(?:nan|inf(?:inity)?|0x(?:[0-9a-f]+(?:\.[0-9a-f]*)?|\.[0-9a-f]+)(?:p[+-]?[0-9]+)?|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)\s*$").expect("numeric Mod name pattern")
                });
                let numeric = LUA_NUMBER.is_match(name);
                if numeric {
                    workshop_id(name)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

fn validate_world_values(values: &Value) -> Result<()> {
    for value in object(values)?.values() {
        ensure!(
            !value.is_array() && !value.is_object(),
            "world overrides require scalar values"
        );
    }
    let values: Map<_, _> = object(values)?
        .iter()
        .filter(|(_, value)| !value.is_null())
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    lua::render_literal(&Value::Object(values))?;
    Ok(())
}

fn validate_level(value: &Value) -> Result<()> {
    let overrides = &value["overrides"];
    validate_world_values(&overrides["values"])?;
    ensure!(
        overrides["values"]["task_set"]
            .as_str()
            .is_some_and(|value| !value.is_empty()),
        "level data requires task_set"
    );
    if let Some(range) = value["background_node_range"].as_array() {
        ensure!(
            range.iter().all(|value| value.as_i64().is_some()),
            "background_node_range requires integers"
        );
        ensure!(
            range[0].as_i64().unwrap_or(-1) >= 0 && range[1].as_i64() >= range[0].as_i64(),
            "invalid background_node_range"
        );
    }
    for field in [
        "version",
        "min_playlist_position",
        "max_playlist_position",
        "numrandom_set_pieces",
    ] {
        if !value[field].is_null() {
            ensure!(
                value[field]
                    .as_i64()
                    .is_some_and(
                        |value| (-lua::MAX_SAFE_INTEGER..=lua::MAX_SAFE_INTEGER).contains(&value)
                    ),
                "{field} requires a safe Lua integer"
            );
        }
    }
    if value["numrandom_set_pieces"].as_u64().unwrap_or(0) > 0 {
        ensure!(
            value["random_set_pieces"]
                .as_array()
                .is_some_and(|v| !v.is_empty()),
            "positive numrandom_set_pieces requires random_set_pieces"
        );
    }
    let kind = overrides["kind"]
        .as_str()
        .unwrap_or("custom")
        .trim_start_matches("loaded_");
    if matches!(kind, "forest" | "cave" | "quagmire" | "lavaarena") {
        ensure!(
            value["location"] == kind,
            "level location and override kind differ"
        );
    }
    if matches!(kind, "quagmire" | "lavaarena") {
        let id = kind.to_ascii_uppercase();
        ensure!(value["id"] == id, "event level has an incorrect id");
        for field in ["settings_id", "worldgen_id"] {
            ensure!(
                value[field].is_null() || value[field] == id,
                "event settings/worldgen ids differ"
            );
        }
        if let Some(prefabs) = value["required_prefabs"].as_array() {
            ensure!(
                prefabs.contains(&json!(format!("{kind}_portal"))),
                "event level is missing its portal prefab"
            );
        }
    }
    Ok(())
}

fn validate_topology(sparse: &Value, current: &Value) -> Result<()> {
    let shards = object(&current["shards"])?;
    let settings = &current["settings"];
    let multi = shards.len() > 1;
    ensure!(
        !multi || sparse["settings"]["shard_enabled"] != false,
        "shard_enabled cannot be false for multiple shards"
    );
    let mut names = BTreeSet::new();
    let mut ids = BTreeSet::new();
    let mut ports = BTreeSet::new();
    let mut master_ports = BTreeSet::new();
    let mut keys = BTreeSet::new();
    let mut masters = 0;
    for (name, shard) in shards {
        configuration::validate_shard_name(name)?;
        ensure!(
            names.insert(name.to_lowercase()),
            "duplicate shard directory names"
        );
        let shard_settings = &shard["settings"];
        let master = shard_settings["is_master"] == true;
        masters += usize::from(master);
        if !master {
            ensure!(
                shard_settings["name"]
                    .as_str()
                    .is_some_and(|value| !value.is_empty()),
                "secondary shards require a name"
            );
        }
        if let Some(id) = shard_settings["id"].as_u64() {
            ensure!(ids.insert(id), "shard ids must be unique");
        }
        for field in ["server_port", "master_server_port"] {
            ensure!(
                ports.insert(shard_settings[field].as_u64().unwrap()),
                "UDP ports must be unique"
            );
        }
        master_ports.insert(
            shard_settings["master_port"]
                .as_u64()
                .or(settings["master_port"].as_u64())
                .unwrap(),
        );
        keys.insert(
            shard_settings["cluster_key"]
                .as_str()
                .or(settings["cluster_key"].as_str()),
        );
        if (multi || settings["shard_enabled"] == true) && !master {
            ensure!(
                shard_settings["master_ip"]
                    .as_str()
                    .or(settings["master_ip"].as_str())
                    .is_some(),
                "secondary shards require master_ip"
            );
        }
        if matches!(
            settings["game_mode"].as_str(),
            Some("quagmire" | "lavaarena")
        ) {
            ensure!(
                !shard["level"].is_null(),
                "event game modes require level data for every shard"
            );
        }
    }
    ensure!(masters == 1, "expected exactly one master shard");
    ensure!(
        master_ports.len() == 1,
        "all shards must use the same master_port"
    );
    ensure!(
        !ports.contains(master_ports.first().unwrap()),
        "master_port conflicts with another UDP port"
    );
    ensure!(
        keys.len() == 1 && !keys.contains(&Some("")),
        "all shards require the same non-empty cluster_key or must omit it"
    );
    Ok(())
}

fn validate_mod_name(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= 255
            && !matches!(name, "." | "..")
            && !name.eq_ignore_ascii_case("client_mods_disabled")
            && !name.contains(['\0', '/', '\\', '\r', '\n']),
        "unsafe Mod name"
    );
    if let Some(id) = name.strip_prefix("workshop-") {
        workshop_id(id)?;
    }
    Ok(())
}

fn workshop_id(value: &str) -> Result<u64> {
    ensure!(
        !value.is_empty()
            && !value.starts_with('0')
            && value.bytes().all(|byte| byte.is_ascii_digit()),
        "Workshop id must be a positive decimal uint64"
    );
    value.parse().context("Workshop id exceeds uint64")
}

pub fn parse_ini(model: &str, source: &str) -> Result<Value> {
    ensure!(
        matches!(model, "ClusterSettings" | "ShardSettings"),
        "unknown INI model"
    );
    let document = configuration::Ini::parse(
        Path::new(if model == "ClusterSettings" {
            "cluster.ini"
        } else {
            "server.ini"
        }),
        source,
        Some(model == "ShardSettings"),
    )?
    .into_sections();
    let definitions = object(&INI[model]["fields"])?;
    let mut result = Map::new();
    for (section, fields) in document {
        ensure!(
            definitions.values().any(|value| value
                .as_str()
                .is_some_and(|v| v.eq_ignore_ascii_case(&section)))
                || INI[model]["ignored"]
                    .as_object()
                    .is_some_and(|ignored| ignored
                        .keys()
                        .any(|key| key.eq_ignore_ascii_case(&section))),
            "unknown INI section"
        );
        for (name, text) in fields {
            if model == "ShardSettings" && section == "steam" && name == "authentication_port" {
                continue;
            }
            ensure!(
                definitions
                    .get(&name)
                    .and_then(Value::as_str)
                    .is_some_and(|expected| expected.eq_ignore_ascii_case(&section)),
                "unknown INI option"
            );
            let default = &DEFAULTS[model][&name];
            let value = if default.is_boolean() {
                ensure!(
                    text.eq_ignore_ascii_case("true") || text.eq_ignore_ascii_case("false"),
                    "INI boolean must be true or false"
                );
                json!(text.eq_ignore_ascii_case("true"))
            } else if default.is_number()
                || (model == "ShardSettings" && ["id", "master_port"].contains(&name.as_str()))
            {
                ensure!(
                    !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()),
                    "INI integer must be unsigned decimal"
                );
                json!(text.parse::<u64>().context("INI integer exceeds uint64")?)
            } else {
                json!(text)
            };
            result.insert(name, value);
        }
    }
    let result = Value::Object(result);
    validate(model, &result)?;
    Ok(result)
}

pub fn render_ini(model: &str, sparse: &Value, multi: bool) -> Result<String> {
    validate(model, sparse)?;
    let mut value = sparse.clone();
    if model == "ShardSettings" {
        let defaults = resolved(model, sparse);
        value["encode_user_path"] = defaults["encode_user_path"].clone();
        if multi {
            value["is_master"] = defaults["is_master"].clone();
        }
    } else if multi && value.get("shard_enabled").is_none() {
        value["shard_enabled"] = json!(true);
    }
    let mut sections = Vec::new();
    for section in INI[model]["sections"]
        .as_array()
        .expect("bundled INI sections")
    {
        let mut lines = Vec::new();
        for field in section["fields"]
            .as_array()
            .expect("bundled INI fields")
            .iter()
            .filter_map(Value::as_str)
        {
            if let Some(value) = value.get(field).filter(|value| !value.is_null()) {
                let text = value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string());
                lines.push(format!("{field} = {text}"));
            }
        }
        if !lines.is_empty() {
            sections.push(format!(
                "[{}]\n{}",
                section["name"].as_str().unwrap(),
                lines.join("\n")
            ));
        }
    }
    let source = sections.join("\n\n");
    ensure!(
        source.len() < files::MAX_FILE_BYTES,
        "INI configuration exceeds the byte limit"
    );
    Ok(if source.is_empty() {
        source
    } else {
        format!("{source}\n")
    })
}

impl ClusterSettings {
    pub fn parse(source: &str) -> Result<Self> {
        Self::from_value(parse_ini("ClusterSettings", source)?)
    }
    pub fn render(&self, multi: bool) -> Result<String> {
        render_ini("ClusterSettings", &self.0, multi)
    }
}
impl ShardSettings {
    pub fn parse(source: &str) -> Result<Self> {
        Self::from_value(parse_ini("ShardSettings", source)?)
    }
    pub fn render(&self, multi: bool) -> Result<String> {
        render_ini("ShardSettings", &self.0, multi)
    }
}

impl ModOverrides {
    pub fn parse(source: &str) -> Result<Self> {
        let mut table = object(&lua::parse_return_table(source)?)?.clone();
        let client = table.remove("client_mods_disabled");
        let mut value = json!({"entries": table});
        if let Some(client) = client {
            value["client_mods_disabled"] = client;
        }
        Self::from_value(value)
    }
    pub fn render(&self) -> Result<String> {
        let mut entries = self.0["entries"].as_object().cloned().unwrap_or_default();
        for entry in entries.values_mut() {
            entry
                .as_object_mut()
                .expect("validated Mod entry")
                .retain(|key, value| {
                    !value.is_null()
                        && !(key == "configuration_options"
                            && value.as_object().is_some_and(Map::is_empty))
                });
        }
        if self
            .0
            .get("client_mods_disabled")
            .is_some_and(|value| !value.is_null())
        {
            entries.insert(
                "client_mods_disabled".into(),
                self.0["client_mods_disabled"].clone(),
            );
        }
        files::render_lua_table(&Value::Object(entries))
    }
    pub fn workshop_items(&self) -> BTreeSet<u64> {
        self.0["entries"]
            .as_object()
            .into_iter()
            .flatten()
            .filter(|(_, value)| value["enabled"] == true)
            .filter_map(|(name, _)| {
                name.strip_prefix("workshop-")
                    .and_then(|id| id.parse().ok())
            })
            .collect()
    }
}

const MOD_FUNCTIONS: [(&str, &str); 4] = [
    ("EnableModDebugPrint", "debug_print"),
    ("EnableModError", "mod_errors"),
    ("DisableModDisabling", "disable_mod_disabling"),
    ("DisableLocalModWarning", "disable_local_mod_warning"),
];

impl ModSettings {
    pub fn parse(source: &str) -> Result<Self> {
        let mut value = json!({});
        let mut enabled = BTreeSet::new();
        for (function, arguments) in lua::literal_calls(source)? {
            if function == "ForceEnableMod" {
                ensure!(arguments.len() == 1, "ForceEnableMod requires one string");
                enabled.insert(
                    arguments[0]
                        .as_str()
                        .context("ForceEnableMod requires one string")?
                        .to_owned(),
                );
            } else if let Some((_, field)) =
                MOD_FUNCTIONS.iter().find(|(name, _)| *name == function)
            {
                ensure!(
                    arguments.is_empty(),
                    "Mod flag functions accept no arguments"
                );
                value[*field] = json!(true);
            } else {
                bail!("unsupported Mod settings function");
            }
        }
        if !enabled.is_empty() {
            value["force_enabled"] = json!(enabled);
        }
        Self::from_value(value)
    }
    pub fn render(&self) -> Result<String> {
        let value = self.resolved();
        let mut output = String::new();
        let names: BTreeSet<_> = value["force_enabled"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(Value::as_str)
            .collect();
        for name in names {
            output.push_str(&format!(
                "ForceEnableMod({})\n",
                lua::render_literal(&json!(name))?
            ));
        }
        for (function, field) in MOD_FUNCTIONS {
            if value[field] == true {
                output.push_str(&format!("{function}()\n"));
            }
        }
        Ok(output)
    }
    pub fn workshop_items(&self) -> BTreeSet<u64> {
        self.0["force_enabled"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .filter_map(|name| workshop_id(name.strip_prefix("workshop-").unwrap_or(name)).ok())
            .collect()
    }
}

impl WorkshopDownloads {
    pub fn parse(source: &str) -> Result<Self> {
        let mut items = BTreeSet::new();
        let mut collections = BTreeSet::new();
        for (function, arguments) in lua::literal_calls(source)? {
            ensure!(
                arguments.len() == 1,
                "Workshop setup requires one string id"
            );
            let id = arguments[0]
                .as_str()
                .context("Workshop id must be a string")?;
            match function.as_str() {
                "ServerModSetup" if id.is_empty() => {}
                "ServerModSetup" => {
                    items.insert(workshop_id(id)?);
                }
                "ServerModCollectionSetup" => {
                    collections.insert(workshop_id(id)?);
                }
                _ => bail!("unsupported Workshop setup function"),
            }
        }
        Self::from_value(json!({"items":items,"collections":collections}))
    }
    pub fn render(&self) -> Result<String> {
        let mut output = String::new();
        for (field, function) in [
            ("items", "ServerModSetup"),
            ("collections", "ServerModCollectionSetup"),
        ] {
            let ids: BTreeSet<_> = self.0[field]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_u64)
                .collect();
            for id in ids {
                output.push_str(&format!("{function}(\"{id}\")\n"));
            }
        }
        Ok(output)
    }
}

impl WorldgenOverride {
    pub fn parse(source: &str) -> Result<Self> {
        Self::parse_with_kind(source, None)
    }

    pub fn parse_with_kind(source: &str, kind: Option<&str>) -> Result<Self> {
        let mut table = object(&lua::parse_return_table(source)?)?.clone();
        let known = [
            "override_enabled",
            "preset",
            "worldgen_preset",
            "settings_preset",
            "overrides",
        ];
        if table.keys().any(|name| !known.contains(&name.as_str())) {
            let mut overrides = table
                .get("overrides")
                .map(object)
                .transpose()?
                .cloned()
                .unwrap_or_default();
            if let Some(presetdata) = table.get("presetdata") {
                let presetdata = object(presetdata)?;
                if let Some(entries) = presetdata.get("overrides")
                    && entries != &json!({})
                {
                    for entry in entries
                        .as_array()
                        .context("legacy preset overrides must be pairs")?
                    {
                        let pair = entry
                            .as_array()
                            .context("legacy preset overrides must be pairs")?;
                        ensure!(pair.len() == 2, "legacy preset overrides must be pairs");
                        overrides.insert(
                            pair[0]
                                .as_str()
                                .context("legacy override key must be text")?
                                .to_owned(),
                            pair[1].clone(),
                        );
                    }
                }
            }
            for (name, group) in &table {
                if name != "presetdata"
                    && let Some(group) = group.as_object()
                {
                    overrides.extend(group.clone());
                }
            }
            let actual = table
                .get("actualpreset")
                .cloned()
                .or_else(|| table.get("preset").cloned());
            table.retain(|name, _| {
                ["worldgen_preset", "settings_preset", "override_enabled"].contains(&name.as_str())
            });
            if let Some(actual) = actual {
                table.insert("preset".into(), actual);
            }
            table.insert("overrides".into(), Value::Object(overrides));
        }
        if let Some(preset) = table.remove("preset") {
            table
                .entry("worldgen_preset")
                .or_insert_with(|| preset.clone());
            table.entry("settings_preset").or_insert(preset);
        }
        let overrides = table.remove("overrides").unwrap_or_else(|| json!({}));
        object(&overrides)?;
        let kind = if let Some(kind) = kind {
            kind.to_owned()
        } else {
            let presets: Vec<_> = ["worldgen_preset", "settings_preset"]
                .iter()
                .filter_map(|key| table.get(*key).and_then(Value::as_str))
                .collect();
            let kinds: BTreeSet<_> = presets
                .iter()
                .filter_map(|preset| preset_kind(preset))
                .collect();
            ensure!(
                kinds.len() <= 1,
                "different built-in world types cannot be combined"
            );
            if let Some(kind) = kinds.first() {
                (*kind).to_owned()
            } else if object(&overrides)?.is_empty() {
                "world".into()
            } else if !presets.is_empty() {
                "custom".into()
            } else {
                let candidates: Vec<_> = ["forest", "cave"]
                    .into_iter()
                    .filter(|kind| VALIDATORS[world_model(kind).unwrap()].is_valid(&overrides))
                    .collect();
                ensure!(
                    candidates.len() == 1,
                    "world type is ambiguous; supply an explicit override kind"
                );
                candidates[0].to_owned()
            }
        };
        ensure!(world_model(&kind).is_some(), "unknown world override kind");
        let enabled = table.remove("override_enabled").unwrap_or(json!(false));
        table.insert("enabled".into(), enabled);
        table.insert("overrides".into(), json!({"kind":kind,"values":overrides}));
        Self::from_value(Value::Object(table))
    }

    pub fn render(&self) -> Result<String> {
        let mut table = json!({"override_enabled":self.resolved()["enabled"], "overrides":{}});
        for field in ["worldgen_preset", "settings_preset"] {
            if self.0.get(field).is_some_and(|value| !value.is_null()) {
                table[field] = self.0[field].clone();
            }
        }
        if let Some(values) = self.0["overrides"]["values"].as_object() {
            table["overrides"] = Value::Object(
                values
                    .iter()
                    .filter(|(_, value)| !value.is_null())
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            );
        }
        files::render_lua_table(&table)
    }
}

impl LevelDataOverride {
    pub fn parse(source: &str) -> Result<Self> {
        Self::parse_with_kind(source, None)
    }
    pub fn parse_with_kind(source: &str, kind: Option<&str>) -> Result<Self> {
        let mut value = lua::parse_return_table(source)?;
        object(&value["overrides"])?;
        let inferred = match value["location"].as_str() {
            Some("forest") => "loaded_forest",
            Some("cave") => "loaded_cave",
            Some("quagmire") => "quagmire",
            Some("lavaarena") => "lavaarena",
            _ => "custom",
        };
        value["overrides"] = json!({"kind":kind.unwrap_or(inferred), "values":value["overrides"]});
        for field in [
            "background_node_range",
            "ordered_story_setpieces",
            "random_set_pieces",
            "required_prefabs",
            "required_setpieces",
        ] {
            if value[field] == json!({}) {
                value[field] = json!([]);
            }
        }
        Self::from_value(value)
    }
    pub fn render(&self) -> Result<String> {
        let mut table = object(&self.0)?.clone();
        table.retain(|_, value| !value.is_null());
        let values = object(&self.0["overrides"]["values"])?;
        table.insert(
            "overrides".into(),
            Value::Object(
                values
                    .iter()
                    .filter(|(_, value)| !value.is_null())
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            ),
        );
        files::render_lua_table(&Value::Object(table))
    }
}

impl ClusterConfig {
    pub fn load(room: &RoomLock) -> Result<Self> {
        Self::load_with_kinds(room, &BTreeMap::new(), &BTreeMap::new())
    }
    pub fn load_with_kinds(
        room: &RoomLock,
        world_kinds: &BTreeMap<String, String>,
        level_kinds: &BTreeMap<String, String>,
    ) -> Result<Self> {
        load_cluster(
            &|path| room.read_optional_text(path),
            &|path| room.read_optional_bytes(path),
            room.shard_directories()?,
            world_kinds,
            level_kinds,
        )
    }
    pub fn load_stopped(room: &StoppedRoom<'_>) -> Result<Self> {
        load_cluster(
            &|path| room.read_optional_text(path),
            &|path| room.read_optional_bytes(path),
            room.shard_directories()?,
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
    }
    pub fn files(&self) -> Result<FileChanges> {
        let current = self.resolved();
        let multi = object(&self.0["shards"])?.len() > 1;
        let settings = self.0.get("settings").cloned().unwrap_or_else(|| json!({}));
        let mut files = FileChanges::new();
        files.insert(
            PathBuf::from("cluster.ini"),
            render_ini("ClusterSettings", &settings, multi)?,
        );
        let token = current["token"].as_str().unwrap_or("");
        files.insert(
            PathBuf::from("cluster_token.txt"),
            if token.is_empty() {
                String::new()
            } else {
                format!("{token}\n")
            },
        );
        for field in PERMISSION_FIELDS {
            files.insert(
                PathBuf::from(format!("{field}.txt")),
                current[field].as_str().unwrap_or("").to_owned(),
            );
        }
        files.insert(
            PathBuf::from("mods/modsettings.lua"),
            ModSettings::from_value(
                self.0
                    .get("mod_settings")
                    .cloned()
                    .unwrap_or_else(|| json!({})),
            )?
            .render()?,
        );
        files.insert(
            PathBuf::from("mods/dedicated_server_mods_setup.lua"),
            self.resolved_downloads()?.render()?,
        );
        let multi = multi || current["settings"]["shard_enabled"] == true;
        for (name, shard) in object(&self.0["shards"])? {
            files.insert(
                PathBuf::from(name).join("server.ini"),
                render_ini("ShardSettings", &shard["settings"], multi)?,
            );
            files.insert(
                PathBuf::from(name).join("modoverrides.lua"),
                ModOverrides::from_value(shard.get("mods").cloned().unwrap_or_else(|| json!({})))?
                    .render()?,
            );
            for (field, file) in [
                ("world", "worldgenoverride.lua"),
                ("level", "leveldataoverride.lua"),
            ] {
                if shard.get(field).is_some_and(|value| !value.is_null()) {
                    let rendered = if field == "world" {
                        WorldgenOverride::from_value(shard[field].clone())?.render()?
                    } else {
                        LevelDataOverride::from_value(shard[field].clone())?.render()?
                    };
                    files.insert(PathBuf::from(name).join(file), rendered);
                }
            }
        }
        Ok(files)
    }

    pub fn resolved_downloads(&self) -> Result<WorkshopDownloads> {
        let current = self.resolved();
        let mut items: BTreeSet<_> = current["downloads"]["items"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(Value::as_u64)
            .collect();
        items.extend(ModSettings::from_value(current["mod_settings"].clone())?.workshop_items());
        for shard in object(&current["shards"])?.values() {
            items.extend(ModOverrides::from_value(shard["mods"].clone())?.workshop_items());
        }
        WorkshopDownloads::from_value(
            json!({"items":items,"collections":current["downloads"]["collections"]}),
        )
    }

    /// Apply a complete offline configuration while retaining saves and live lists.
    pub fn save(
        &self,
        room: &mut StoppedRoom<'_>,
        permissions: files::PermissionFiles,
    ) -> Result<Vec<PathBuf>> {
        let mut configuration = self.0.clone();
        let current = self.resolved();
        let has_key = object(&current["shards"])?.values().any(|shard| {
            shard["settings"]["cluster_key"].is_string()
                || current["settings"]["cluster_key"].is_string()
        });
        if !has_key {
            let old_cluster_key = room
                .read_optional_text("cluster.ini")?
                .map(|source| native_key("cluster.ini", &source))
                .transpose()?
                .flatten();
            let mut keys = BTreeSet::new();
            for name in room.shard_directories()? {
                if let Some(source) =
                    room.read_optional_text(Path::new(&name).join("server.ini"))?
                {
                    keys.insert(
                        native_key("server.ini", &source)?.or_else(|| old_cluster_key.clone()),
                    );
                }
            }
            if keys.is_empty() {
                keys.insert(old_cluster_key);
            }
            ensure!(
                keys.len() == 1 && !keys.contains(&Some(String::new())),
                "existing shards do not share one cluster_key"
            );
            let key = keys
                .into_iter()
                .next()
                .flatten()
                .map(Ok)
                .unwrap_or_else(new_cluster_key)?;
            if configuration.get("settings").is_none() {
                configuration["settings"] = json!({});
            }
            configuration["settings"]["cluster_key"] = json!(key);
        }
        let target = Self::from_value(configuration)?;
        let mut files = target.files()?;
        if permissions == files::PermissionFiles::Preserve {
            for field in PERMISSION_FIELDS {
                files.remove(Path::new(&format!("{field}.txt")));
            }
        }
        if self.0.get("token").is_none() && room.read_optional_text("cluster_token.txt")?.is_some()
        {
            files.remove(Path::new("cluster_token.txt"));
        }
        if self.0.get("mod_settings").is_none()
            && room.read_optional_text("mods/modsettings.lua")?.is_some()
        {
            files.remove(Path::new("mods/modsettings.lua"));
            if let Some(source) = room.read_optional_text("mods/dedicated_server_mods_setup.lua")? {
                let existing = WorkshopDownloads::parse(&source)?.resolved();
                let mut downloads = target.resolved_downloads()?.into_value();
                for field in ["items", "collections"] {
                    let ids: BTreeSet<_> = downloads[field]
                        .as_array()
                        .unwrap()
                        .iter()
                        .chain(existing[field].as_array().unwrap())
                        .filter_map(Value::as_u64)
                        .collect();
                    downloads[field] = json!(ids);
                }
                files.insert(
                    PathBuf::from("mods/dedicated_server_mods_setup.lua"),
                    WorkshopDownloads::from_value(downloads)?.render()?,
                );
            }
        }
        let mut removed = Vec::new();
        for name in room.shard_directories()? {
            if target.0["shards"].get(&name).is_none()
                && room
                    .read_optional_text(Path::new(&name).join("server.ini"))?
                    .is_some()
            {
                removed.push(PathBuf::from(name).join("server.ini"));
            }
        }
        for name in object(&target.0["shards"])?.keys() {
            for filename in ["worldgenoverride.lua", "leveldataoverride.lua"] {
                let path = PathBuf::from(name).join(filename);
                if !files.contains_key(&path) && room.read_optional_text(&path)?.is_some() {
                    removed.push(path);
                }
            }
        }
        let mut changed = FileChanges::new();
        for (path, contents) in files {
            if room.read_optional_bytes(&path)?.as_deref() != Some(contents.as_bytes()) {
                changed.insert(path, contents);
            }
        }
        room.commit_with_deletions(changed, removed, permissions)
    }
}

fn native_key(path: &str, source: &str) -> Result<Option<String>> {
    Ok(configuration::Ini::parse(Path::new(path), source, None)?
        .into_sections()
        .get("shard")
        .and_then(|fields| fields.get("cluster_key"))
        .cloned())
}

fn new_cluster_key() -> Result<String> {
    use std::io::Read;
    let mut bytes = [0_u8; 32];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn load_cluster(
    read: &impl Fn(&Path) -> Result<Option<String>>,
    read_bytes: &impl Fn(&Path) -> Result<Option<Vec<u8>>>,
    directories: Vec<String>,
    world_kinds: &BTreeMap<String, String>,
    level_kinds: &BTreeMap<String, String>,
) -> Result<ClusterConfig> {
    let required = |path: &Path| {
        read(path)?.with_context(|| format!("missing native configuration: {}", path.display()))
    };
    let settings = parse_ini("ClusterSettings", &required(Path::new("cluster.ini"))?)?;
    let token = required(Path::new("cluster_token.txt"))?;
    let token = token
        .strip_suffix("\r\n")
        .or_else(|| token.strip_suffix('\n'))
        .unwrap_or(&token);
    let mut shards = Map::new();
    for name in directories {
        let Some(source) = read(&Path::new(&name).join("server.ini"))? else {
            continue;
        };
        let mut shard = json!({"settings":parse_ini("ShardSettings", &source)?});
        for (field, filename) in [
            ("mods", "modoverrides.lua"),
            ("world", "worldgenoverride.lua"),
            ("level", "leveldataoverride.lua"),
        ] {
            if let Some(source) = read(&Path::new(&name).join(filename))? {
                shard[field] = match field {
                    "mods" => ModOverrides::parse(&source)?.into_value(),
                    "world" => WorldgenOverride::parse_with_kind(
                        &source,
                        world_kinds.get(&name).map(String::as_str),
                    )?
                    .into_value(),
                    _ => LevelDataOverride::parse_with_kind(
                        &source,
                        level_kinds.get(&name).map(String::as_str),
                    )?
                    .into_value(),
                };
            }
        }
        shards.insert(name, shard);
    }
    if shards.len() > 1 {
        ensure!(
            settings["shard_enabled"] == true,
            "existing multiple shards require explicit shard_enabled"
        );
    }
    if shards.len() > 1 || settings["shard_enabled"] == true {
        ensure!(
            shards
                .values()
                .all(|shard| shard["settings"].get("is_master").is_some()),
            "existing sharded server.ini requires explicit is_master"
        );
    }
    let mut cluster = json!({"settings":settings,"shards":shards,"token":token});
    for field in PERMISSION_FIELDS {
        if let Some(source) = read_bytes(Path::new(&format!("{field}.txt")))? {
            let users = files::permission_users(&source);
            cluster[field] = json!(if users.is_empty() {
                String::new()
            } else {
                users.join("\n") + "\n"
            });
        }
    }
    if let Some(source) = read(Path::new("mods/modsettings.lua"))? {
        cluster["mod_settings"] = ModSettings::parse(&source)?.into_value();
    }
    if let Some(source) = read(Path::new("mods/dedicated_server_mods_setup.lua"))? {
        cluster["downloads"] = WorkshopDownloads::parse(&source)?.into_value();
    }
    ClusterConfig::from_value(cluster)
}

impl RoomPreset {
    pub fn compose(parts: &[Self]) -> Result<Self> {
        let mut output = json!({"settings":{},"shards":{},"mods":{}});
        for part in parts {
            overlay(&mut output["settings"], &part.0["settings"]);
            if part.0["mods"]
                .as_object()
                .is_some_and(|mods| !mods.is_empty())
            {
                merge_mods(&mut output["mods"], &part.0["mods"]);
                for shard in output["shards"].as_object_mut().unwrap().values_mut() {
                    if shard.get("mods").is_none() {
                        shard["mods"] = json!({});
                    }
                    merge_mods(&mut shard["mods"], &part.0["mods"]);
                }
            }
            for (name, patch) in part.0["shards"].as_object().into_iter().flatten() {
                if let Some(base) = output["shards"].get_mut(name) {
                    let mut patch = patch.clone();
                    let mut settings = base["settings"].clone();
                    overlay(&mut settings, &patch["settings"]);
                    patch["settings"] = settings;
                    if patch.get("world").is_some_and(|world| !world.is_null())
                        && !base["world"].is_null()
                    {
                        let mut world = base["world"].clone();
                        if let Some(overrides) = patch["world"].get("overrides") {
                            let kind = world["overrides"]["kind"].as_str().unwrap_or("world");
                            ensure!(
                                Some(kind) == overrides["kind"].as_str(),
                                "cannot compose different world override kinds"
                            );
                            let mut values = world["overrides"]["values"].clone();
                            overlay(&mut values, &overrides["values"]);
                            patch["world"]["overrides"]["values"] = values;
                        }
                        overlay(&mut world, &patch["world"]);
                        patch["world"] = world;
                    }
                    if patch.get("mods").is_some() {
                        let mut mods = base.get("mods").cloned().unwrap_or_else(|| json!({}));
                        merge_mods(&mut mods, &patch["mods"]);
                        patch["mods"] = mods;
                    }
                    overlay(base, &patch);
                } else {
                    let mut shard = patch.clone();
                    let mut mods = output["mods"].clone();
                    merge_mods(&mut mods, &shard["mods"]);
                    shard["mods"] = mods;
                    output["shards"][name] = shard;
                }
            }
        }
        Self::from_value(output)
    }

    pub fn build(
        &self,
        token: &str,
        cluster_key: Option<&str>,
        settings: Option<&ClusterSettings>,
    ) -> Result<ClusterConfig> {
        let mut configuration = json!({"settings": self.0.get("settings").cloned().unwrap_or_else(|| json!({})), "shards":self.0.get("shards").cloned().unwrap_or_else(|| json!({})), "token":token});
        if let Some(settings) = settings {
            overlay(&mut configuration["settings"], settings.as_value());
        }
        if let Some(key) = cluster_key {
            configuration["settings"]["cluster_key"] = json!(key);
        }
        for shard in configuration["shards"]
            .as_object_mut()
            .unwrap()
            .values_mut()
        {
            let mut mods = self.0.get("mods").cloned().unwrap_or_else(|| json!({}));
            merge_mods(&mut mods, &shard["mods"]);
            shard["mods"] = mods;
        }
        ClusterConfig::from_value(configuration)
    }
}

fn overlay(base: &mut Value, patch: &Value) {
    if let Some(patch) = patch.as_object() {
        if !base.is_object() {
            *base = json!({});
        }
        base.as_object_mut().unwrap().extend(patch.clone());
    }
}

fn merge_mods(base: &mut Value, patch: &Value) {
    let mut patch = patch.clone();
    if let Some(entries) = patch.get("entries") {
        let mut merged = base.get("entries").cloned().unwrap_or_else(|| json!({}));
        overlay(&mut merged, entries);
        patch["entries"] = merged;
    }
    overlay(base, &patch);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn all_twelve_templates_round_trip_through_native_files() {
        let presets: Value =
            serde_json::from_str(include_str!("../resources/settings/presets.json")).unwrap();
        let templates = presets["templates"].as_object().unwrap();
        assert_eq!(templates.len(), 12);
        for (name, template) in templates {
            let directory = tempfile::tempdir().unwrap();
            let mut value = template["cluster"].clone();
            value["settings"]["cluster_key"] = json!("private-key");
            value["token"] = json!("private-token");
            let cluster =
                ClusterConfig::from_value(value).unwrap_or_else(|error| panic!("{name}: {error}"));
            let expected = cluster.files().unwrap();
            let mut room = RoomLock::try_acquire(directory.path()).unwrap();
            cluster
                .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
                .unwrap_or_else(|error| panic!("{name}: {error}"));
            let loaded =
                ClusterConfig::load(&room).unwrap_or_else(|error| panic!("{name}: {error}"));
            assert_eq!(loaded.files().unwrap(), expected, "{name}");
            assert!(
                loaded
                    .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
                    .unwrap()
                    .is_empty(),
                "{name}"
            );
            let public = serde_json::to_string(&loaded).unwrap();
            assert!(!public.contains("private-key") && !public.contains("private-token"));
            assert!(loaded.as_value()["token"] == "private-token");
        }
    }

    #[test]
    fn sparse_composition_preserves_defaults_and_explicit_null() {
        let base = RoomPreset::from_value(json!({"settings":{"cluster_name":"first"},"shards":{"forest":{"settings":{"encode_user_path":false},"world":{"overrides":{"kind":"forest","values":{"day":"onlynight"}}},"mods":{"entries":{"local":{"enabled":true,"configuration_options":{"value":1}}}}}}})).unwrap();
        let patch = RoomPreset::from_value(json!({"settings":{"cluster_name":null},"shards":{"forest":{"settings":{"server_port":12000},"world":null}},"mods":{"entries":{"local":{"enabled":false},"other":{"enabled":true}}}})).unwrap();
        let merged = RoomPreset::compose(&[base.clone(), patch]).unwrap();
        let cluster = merged.build("token", Some("key"), None).unwrap();
        assert_eq!(cluster.as_value()["settings"]["cluster_name"], Value::Null);
        assert_eq!(cluster.as_value()["shards"]["forest"]["world"], Value::Null);
        assert_eq!(
            cluster.as_value()["shards"]["forest"]["settings"],
            json!({"encode_user_path":false,"server_port":12000})
        );
        assert_eq!(
            cluster.as_value()["shards"]["forest"]["mods"]["entries"]["local"],
            json!({"enabled":false})
        );
        assert_eq!(base.as_value()["settings"]["cluster_name"], "first");
        assert!(
            ClusterSettings::from_value(json!({}))
                .unwrap()
                .as_value()
                .get("cluster_name")
                .is_none()
        );
        assert_eq!(
            ClusterSettings::from_value(json!({}))
                .unwrap()
                .render(false)
                .unwrap(),
            ""
        );
        assert_eq!(
            ShardSettings::from_value(json!({}))
                .unwrap()
                .render(true)
                .unwrap(),
            "[SHARD]\nis_master = true\n\n[ACCOUNT]\nencode_user_path = true\n"
        );
    }

    #[test]
    fn ini_constraints_and_cross_field_errors_never_echo_credentials() {
        assert!(
            ClusterSettings::parse("[NETWORK]\ntick_rate=1\ncluster_password=PRIVATE\n").is_ok()
        );
        assert!(ClusterSettings::parse("[STEAM]\nsteam_group_id=18446744073709551615\n").is_ok());
        for source in [
            "[NETWORK]\nunknown=PRIVATE\n",
            "[NETWORK]\ncluster_password=PRIVATE\ncluster_password=PRIVATE\n",
            "[MISC]\nmods_enabled=yes\n",
            "[SHARD]\nmaster_port=1023\n",
        ] {
            let error = ClusterSettings::parse(source).unwrap_err().to_string();
            assert!(!error.contains("PRIVATE"), "{error}");
        }
        for value in [
            json!({"steam_group_only":true}),
            json!({"max_players":1,"whitelist_slots":2}),
            json!({"tick_rate":61}),
            json!({"bind_ip":"::1"}),
            json!({"cluster_key":"PRIVATE\n"}),
            json!({"max_players":1.0}),
            json!({"steam_group_id":18446744073709551616_f64}),
        ] {
            let error = ClusterSettings::from_value(value).unwrap_err().to_string();
            assert!(!error.contains("PRIVATE"));
        }
        let shard = ShardSettings::parse(
            "[STEAM]\nauthentication_port=legacy\n[ACCOUNT]\nencode_user_path=false\n",
        )
        .unwrap();
        assert_eq!(shard.as_value(), &json!({"encode_user_path":false}));
        for value in [json!({"id":2}), json!({"is_master":false,"id":1})] {
            assert!(ShardSettings::from_value(value).is_err());
        }
    }

    #[test]
    fn native_mod_calls_and_world_tables_are_static_and_bounded() {
        let downloads = WorkshopDownloads::parse("ServerModSetup('')\nServerModSetup('42')\nreturn ServerModCollectionSetup('18446744073709551615')").unwrap();
        assert!(WorkshopDownloads::from_value(json!({"items":[42.0]})).is_err());
        assert_eq!(
            downloads.render().unwrap(),
            "ServerModSetup(\"42\")\nServerModCollectionSetup(\"18446744073709551615\")\n"
        );
        assert_eq!(
            ModSettings::parse("ForceEnableMod('local')\nreturn EnableModError()")
                .unwrap()
                .render()
                .unwrap(),
            "ForceEnableMod(\"local\")\nEnableModError()\n"
        );
        for source in [
            "ServerModSetup('01')",
            "ServerModSetup('18446744073709551616')",
            "local id='42'; ServerModSetup(id)",
            "ServerModSetup([[42]])",
            "return ServerModSetup('1'); return ServerModSetup('2')",
            "os.execute('bad')",
            "ServerModSetup('1')()",
        ] {
            assert!(WorkshopDownloads::parse(source).is_err(), "{source}");
        }
        for name in [
            "0",
            "01",
            " 12",
            "0x1",
            "1e2",
            "NaN",
            "+1",
            "18446744073709551616",
        ] {
            assert!(
                ModSettings::from_value(json!({"force_enabled":[name]})).is_err(),
                "{name}"
            );
        }
        assert!(ModSettings::from_value(json!({"force_enabled":["0xnot-a-number"]})).is_ok());
        assert!(
            ModOverrides::parse(
                "return {['workshop-42']={enabled=true}, ['workshop-43']={enabled=false}}"
            )
            .unwrap()
            .workshop_items()
                == BTreeSet::from([42])
        );
        assert!(WorldgenOverride::parse("return {overrides={day='onlynight'}}").is_err());
        let world = WorldgenOverride::parse_with_kind(
            "return {overrides={day='onlynight'}}",
            Some("forest"),
        )
        .unwrap();
        assert_eq!(world.as_value()["enabled"], false);
        assert!(world.render().unwrap().contains("onlynight"));
        let legacy = WorldgenOverride::parse("return {actualpreset='SURVIVAL_TOGETHER', override_enabled=true, presetdata={overrides={{'day','onlynight'}}}, resources={grass='often'}}").unwrap();
        assert_eq!(
            legacy.as_value()["overrides"]["values"],
            json!({"day":"onlynight","grass":"often"})
        );
        assert!(
            WorldgenOverride::from_value(
                json!({"worldgen_preset":"DST_CAVE","overrides":{"kind":"forest","values":{}}})
            )
            .is_err()
        );
        assert!(LevelDataOverride::from_value(json!({"id":"CUSTOM","name":"name","desc":"","location":"forest","overrides":{"kind":"forest","values":{}}})).is_err());
        let level = LevelDataOverride::parse("return {id='SAVED',name='world',desc='',location='forest',overrides={task_set='default',islands='always',custom_density=0.25}}").unwrap();
        assert_eq!(level.as_value()["overrides"]["kind"], "loaded_forest");
        assert_eq!(
            LevelDataOverride::parse(&level.render().unwrap()).unwrap(),
            level
        );
    }

    #[test]
    fn saving_retains_live_permissions_keys_and_retired_worlds() {
        let directory = tempfile::tempdir().unwrap();
        let mut lock = RoomLock::try_acquire(directory.path()).unwrap();
        let cluster = ClusterConfig::from_value(json!({"settings":{"master_ip":"127.0.0.1"},"shards":{"A":{"settings":{}},"B":{"settings":{"is_master":false,"name":"Caves","id":2,"server_port":11000,"master_server_port":27017}}},"token":"token"})).unwrap();
        cluster
            .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        let loaded = ClusterConfig::load(&lock).unwrap();
        let key = loaded.as_value()["settings"]["cluster_key"].clone();
        fs::write(directory.path().join("blocklist.txt"), "KU_NEW\r\n").unwrap();
        fs::create_dir_all(directory.path().join("B/save")).unwrap();
        fs::write(directory.path().join("B/save/world"), "saved cave").unwrap();
        let mut changed = loaded.into_value();
        changed["settings"]["max_players"] = json!(8);
        changed["settings"]
            .as_object_mut()
            .unwrap()
            .remove("cluster_key");
        changed["shards"].as_object_mut().unwrap().remove("B");
        let changed = ClusterConfig::from_value(changed).unwrap();
        let written = changed
            .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        assert!(
            written.contains(&PathBuf::from("cluster.ini"))
                && written.contains(&PathBuf::from("B/server.ini"))
        );
        assert!(!directory.path().join("B/server.ini").exists());
        assert_eq!(
            fs::read_to_string(directory.path().join("B/save/world")).unwrap(),
            "saved cave"
        );
        assert_eq!(
            fs::read_to_string(directory.path().join("blocklist.txt")).unwrap(),
            "KU_NEW\r\n"
        );
        assert_eq!(
            ClusterConfig::load(&lock).unwrap().as_value()["settings"]["cluster_key"],
            key
        );
        fs::write(
            directory.path().join("A/worldgenoverride.lua"),
            "return {overrides={day='onlynight'}}",
        )
        .unwrap();
        assert!(ClusterConfig::load(&lock).is_err());
        let kinds = BTreeMap::from([("A".to_owned(), "forest".to_owned())]);
        assert!(ClusterConfig::load_with_kinds(&lock, &kinds, &BTreeMap::new()).is_ok());
        fs::write(
            directory.path().join("A/modoverrides.lua"),
            "return generate_mods()",
        )
        .unwrap();
        assert!(ClusterConfig::load(&lock).is_err());
        assert!(configuration::discover(directory.path()).is_ok());
    }
}
