//! Typed, sparse configuration and native file adapters.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value, json};

use crate::{
    configuration,
    files::{self, FileChanges, StoppedRoom},
    lua,
};

mod checks;
pub mod types;

use checks::Check;
use types::Resolve;

pub const PERMISSION_FIELDS: [&str; 3] = ["adminlist", "whitelist", "blocklist"];

fn object(value: &Value) -> Result<&Map<String, Value>> {
    value.as_object().context("configuration must be an object")
}

fn check_input(value: &Value) -> Result<()> {
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
        serde_json::to_vec(value)?.len() <= files::MAX_CONFIG_BYTES,
        "configuration exceeds the byte limit"
    );
    Ok(())
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

macro_rules! configuration_models {
    ($($name:ident => $wire:literal),+ $(,)?) => {
        $(
            #[derive(Clone, PartialEq)]
            pub struct $name(types::$name);

            impl $name {
                pub fn from_value(value: Value) -> Result<Self> {
                    Self::from_ref(&value)
                }

                fn from_ref(value: &Value) -> Result<Self> {
                    check_input(value)?;
                    let data: types::$name = crate::validation::decode(value)
                        .map_err(|error| anyhow::anyhow!("invalid {} at {}", $wire, error.path))?;
                    data.check()?;
                    Ok(Self(data))
                }

                pub fn data(&self) -> &types::$name { &self.0 }
                pub fn as_value(&self) -> Value {
                    serde_json::to_value(&self.0).expect("serializable configuration")
                }
                pub fn into_value(self) -> Value {
                    serde_json::to_value(self.0).expect("serializable configuration")
                }
                pub fn resolved(&self) -> Value { self.0.resolved() }
            }

            impl std::fmt::Debug for $name {
                fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                    redact(self.as_value()).fmt(formatter)
                }
            }

            impl Serialize for $name {
                fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
                    redact(self.as_value()).serialize(serializer)
                }
            }

            impl<'de> Deserialize<'de> for $name {
                fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
                    Self::from_value(Value::deserialize(deserializer)?).map_err(serde::de::Error::custom)
                }
            }
        )+

        pub fn validate(name: &str, value: &Value) -> Result<()> {
            match name {
                $($wire => $name::from_ref(value).map(|_| ()),)+
                _ => bail!("unknown configuration model"),
            }
        }

        /// Fill defaults while retaining the sparse input in the stored model.
        pub fn resolved(name: &str, value: &Value) -> Result<Value> {
            match name {
                $($wire => $name::from_ref(value).map(|model| model.resolved()),)+
                _ => bail!("unknown configuration model"),
            }
        }
    };
}

configuration_models! {
    ClusterConfig => "ClusterConfig",
    ClusterSettings => "ClusterSettings",
    ShardConfig => "ShardConfig",
    ShardSettings => "ShardSettings",
    RoomPreset => "RoomPreset",
    WorldgenOverride => "WorldgenOverride",
    LevelDataOverride => "LevelDataOverride",
    ModOverride => "ModOverride",
    ModOverrides => "ModOverrides",
    ModSettings => "ModSettings",
    WorkshopDownloads => "WorkshopDownloads",
    WorldOverrides => "WorldOverrides",
    ForestOverrides => "ForestOverrides",
    CaveOverrides => "CaveOverrides",
    QuagmireOverrides => "QuagmireOverrides",
    LavaArenaOverrides => "LavaArenaOverrides",
    CustomWorldOverrides => "CustomWorldOverrides",
    LoadedForestOverrides => "_LoadedForestOverrides",
    LoadedCaveOverrides => "_LoadedCaveOverrides",
}

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

fn validate_world_values(values: &Value) -> Result<()> {
    for value in object(values)?.values() {
        ensure!(
            !value.is_array() && !value.is_object(),
            "world overrides require scalar values"
        );
    }
    let mut values = object(values)?.clone();
    values.retain(|_, value| !value.is_null());
    lua::render_literal(&Value::Object(values))?;
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

pub(crate) fn workshop_id(value: &str) -> Result<u64> {
    ensure!(
        !value.is_empty()
            && !value.starts_with('0')
            && value.bytes().all(|byte| byte.is_ascii_digit()),
        "Workshop id must be a positive decimal uint64"
    );
    value.parse().context("Workshop id exceeds uint64")
}

fn ini_sections(model: &str) -> Result<&'static [(&'static str, &'static [&'static str])]> {
    match model {
        "ClusterSettings" => Ok(types::ClusterSettings::SECTIONS),
        "ShardSettings" => Ok(types::ShardSettings::SECTIONS),
        _ => bail!("unknown INI model"),
    }
}

pub fn parse_ini(model: &str, source: &str) -> Result<Value> {
    let sections = ini_sections(model)?;
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
    let mut result = Map::new();
    for (section, fields) in document {
        ensure!(
            sections
                .iter()
                .any(|(name, _)| name.eq_ignore_ascii_case(&section)),
            "unknown INI section"
        );
        for (name, text) in fields {
            if model == "ShardSettings" && section == "steam" && name == "authentication_port" {
                continue;
            }
            let value = match model {
                "ClusterSettings" => types::ClusterSettings::parse_field(&section, &name, &text)?,
                _ => types::ShardSettings::parse_field(&section, &name, &text)?,
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
        let defaults = resolved(model, sparse)?;
        value["encode_user_path"] = defaults["encode_user_path"].clone();
        if multi {
            value["is_master"] = defaults["is_master"].clone();
        }
    } else if multi && value.get("shard_enabled").is_none() {
        value["shard_enabled"] = json!(true);
    }
    let mut sections = Vec::new();
    for (section, fields) in ini_sections(model)? {
        let mut lines = Vec::new();
        for field in *fields {
            if let Some(value) = value.get(*field).filter(|value| !value.is_null()) {
                let text = value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string());
                lines.push(format!("{field} = {text}"));
            }
        }
        if !lines.is_empty() {
            sections.push(format!("[{section}]\n{}", lines.join("\n")));
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
        render_ini("ClusterSettings", &self.as_value(), multi)
    }
}
impl ShardSettings {
    pub fn parse(source: &str) -> Result<Self> {
        Self::from_value(parse_ini("ShardSettings", source)?)
    }
    pub fn render(&self, multi: bool) -> Result<String> {
        render_ini("ShardSettings", &self.as_value(), multi)
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
        let mut entries: Map<String, Value> = self
            .0
            .entries()
            .into_iter()
            .map(|(name, entry)| (name, ModOverride(entry).into_value()))
            .collect();
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
        if let Some(disabled) = self.0.client_mods_disabled() {
            entries.insert("client_mods_disabled".into(), json!(disabled));
        }
        files::render_lua_table(&Value::Object(entries))
    }
    pub fn workshop_items(&self) -> BTreeSet<u64> {
        self.0
            .entries
            .iter()
            .flatten()
            .filter(|(_, value)| value.enabled() == Some(true))
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
        self.0
            .force_enabled
            .iter()
            .flatten()
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
        for (ids, function) in [
            (self.0.items(), "ServerModSetup"),
            (self.0.collections(), "ServerModCollectionSetup"),
        ] {
            let ids: BTreeSet<_> = ids.into_iter().collect();
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
                    .filter(|kind| validate(world_model(kind).unwrap(), &overrides).is_ok())
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
        let mut table = json!({"override_enabled":self.0.enabled(), "overrides":{}});
        for (field, preset) in [
            ("worldgen_preset", self.0.worldgen_preset()),
            ("settings_preset", self.0.settings_preset()),
        ] {
            if let Some(preset) = preset {
                table[field] = json!(preset);
            }
        }
        if let Some(values) = self.0.overrides().values().as_object() {
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
        let mut table = object(&self.as_value())?.clone();
        table.retain(|_, value| !value.is_null());
        let overrides = self.0.overrides.values();
        let values = object(&overrides)?;
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
    pub(crate) fn from_files(files: &BTreeMap<PathBuf, Vec<u8>>) -> Result<Self> {
        let read_bytes = |path: &Path| {
            files
                .get(path)
                .map(|bytes| {
                    ensure!(
                        bytes.len() <= files::MAX_FILE_BYTES,
                        "configuration file exceeds the byte limit"
                    );
                    Ok(bytes.clone())
                })
                .transpose()
        };
        let directories = files
            .keys()
            .filter(|path| {
                path.components().count() == 2
                    && path.file_name().is_some_and(|name| name == "server.ini")
            })
            .map(|path| path.iter().next().unwrap().to_string_lossy().into_owned())
            .collect();
        load_cluster(
            &|path| {
                read_bytes(path)?
                    .map(String::from_utf8)
                    .transpose()
                    .context("read UTF-8 configuration text")
            },
            &read_bytes,
            directories,
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
    }

    pub fn load(room: &files::RoomReader) -> Result<Self> {
        Self::load_with_kinds(room, &BTreeMap::new(), &BTreeMap::new())
    }
    pub fn load_with_kinds(
        room: &files::RoomReader,
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
    pub fn files(&self) -> Result<FileChanges> {
        let multi = self.0.shards.len() > 1;
        let settings = self.0.settings();
        let mut files = FileChanges::new();
        files.insert(
            PathBuf::from("cluster.ini"),
            ClusterSettings(settings.clone()).render(multi)?,
        );
        let token = self.0.token();
        files.insert(
            PathBuf::from("cluster_token.txt"),
            if token.is_empty() {
                String::new()
            } else {
                format!("{token}\n")
            },
        );
        for (field, users) in [
            ("adminlist", self.0.adminlist()),
            ("whitelist", self.0.whitelist()),
            ("blocklist", self.0.blocklist()),
        ] {
            files.insert(PathBuf::from(format!("{field}.txt")), users);
        }
        files.insert(
            PathBuf::from("mods/modsettings.lua"),
            ModSettings(self.0.mod_settings()).render()?,
        );
        files.insert(
            PathBuf::from("mods/dedicated_server_mods_setup.lua"),
            self.resolved_downloads()?.render()?,
        );
        let multi = multi || settings.shard_enabled();
        for (name, shard) in &self.0.shards {
            files.insert(
                PathBuf::from(name).join("server.ini"),
                ShardSettings(shard.settings.clone()).render(multi)?,
            );
            files.insert(
                PathBuf::from(name).join("modoverrides.lua"),
                ModOverrides(shard.mods()).render()?,
            );
            if let Some(world) = shard.world() {
                files.insert(
                    PathBuf::from(name).join("worldgenoverride.lua"),
                    WorldgenOverride(world).render()?,
                );
            }
            if let Some(level) = shard.level() {
                files.insert(
                    PathBuf::from(name).join("leveldataoverride.lua"),
                    LevelDataOverride(level).render()?,
                );
            }
        }
        Ok(files)
    }

    pub fn resolved_downloads(&self) -> Result<WorkshopDownloads> {
        let downloads = self.0.downloads();
        let mut items: BTreeSet<_> = downloads.items().into_iter().collect();
        items.extend(ModSettings(self.0.mod_settings()).workshop_items());
        for shard in self.0.shards.values() {
            items.extend(ModOverrides(shard.mods()).workshop_items());
        }
        Ok(WorkshopDownloads(types::WorkshopDownloads {
            items: Some(items.into_iter().collect()),
            collections: Some(downloads.collections()),
        }))
    }

    /// Apply a complete offline configuration while retaining saves and live lists.
    pub fn save(
        &self,
        room: &mut StoppedRoom<'_>,
        permissions: files::PermissionFiles,
    ) -> Result<Vec<PathBuf>> {
        let mut configuration = self.0.clone();
        let has_key = self.0.settings().cluster_key().is_some()
            || self
                .0
                .shards
                .values()
                .any(|shard| shard.settings.cluster_key().is_some());
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
            configuration
                .settings
                .get_or_insert_with(Default::default)
                .cluster_key = Some(Some(key));
        }
        check_input(&serde_json::to_value(&configuration)?)?;
        crate::validation::check(&configuration)?;
        configuration.check()?;
        let target = Self(configuration);
        let mut files = target.files()?;
        if permissions == files::PermissionFiles::Preserve {
            for field in PERMISSION_FIELDS {
                files.remove(Path::new(&format!("{field}.txt")));
            }
        }
        if self.0.token.is_none() && room.read_optional_text("cluster_token.txt")?.is_some() {
            files.remove(Path::new("cluster_token.txt"));
        }
        if self.0.mod_settings.is_none()
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
            if !target.0.shards.contains_key(&name)
                && room
                    .read_optional_text(Path::new(&name).join("server.ini"))?
                    .is_some()
            {
                removed.push(PathBuf::from(name).join("server.ini"));
            }
        }
        for name in target.0.shards.keys() {
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
        room.write_files(changed, removed, permissions)
    }
}

fn native_key(path: &str, source: &str) -> Result<Option<String>> {
    Ok(configuration::Ini::parse(Path::new(path), source, None)?
        .into_sections()
        .get("shard")
        .and_then(|fields| fields.get("cluster_key"))
        .cloned())
}

pub(crate) fn new_cluster_key() -> Result<String> {
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
    let token = read(Path::new("cluster_token.txt"))?.unwrap_or_default();
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
            let part = part.as_value();
            overlay(&mut output["settings"], &part["settings"]);
            if part["mods"]
                .as_object()
                .is_some_and(|mods| !mods.is_empty())
            {
                merge_mods(&mut output["mods"], &part["mods"]);
                for shard in output["shards"].as_object_mut().unwrap().values_mut() {
                    if shard.get("mods").is_none() {
                        shard["mods"] = json!({});
                    }
                    merge_mods(&mut shard["mods"], &part["mods"]);
                }
            }
            for (name, patch) in part["shards"].as_object().into_iter().flatten() {
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
        let value = self.as_value();
        let mut configuration = json!({"settings": value.get("settings").cloned().unwrap_or_else(|| json!({})), "shards":value.get("shards").cloned().unwrap_or_else(|| json!({})), "token":token});
        if let Some(settings) = settings {
            overlay(&mut configuration["settings"], &settings.as_value());
        }
        if let Some(key) = cluster_key {
            configuration["settings"]["cluster_key"] = json!(key);
        }
        for shard in configuration["shards"]
            .as_object_mut()
            .unwrap()
            .values_mut()
        {
            let mut mods = value.get("mods").cloned().unwrap_or_else(|| json!({}));
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
    use crate::files::RoomLock;
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
            "[SHARD]\nis_master = true\n\n[ACCOUNT]\nencode_user_path = false\n"
        );
    }

    #[test]
    fn saving_writes_default_encoding_and_preserves_explicit_encoding() {
        let directory = tempfile::tempdir().unwrap();
        fs::create_dir(directory.path().join("forest")).unwrap();
        fs::write(
            directory.path().join("cluster.ini"),
            "[SHARD]\ncluster_key = existing-key\n",
        )
        .unwrap();
        let server = directory.path().join("forest/server.ini");
        fs::write(&server, "[NETWORK]\nserver_port = 12000\n").unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        let cluster = ClusterConfig::load(&room).unwrap();
        assert!(
            cluster.as_value()["shards"]["forest"]["settings"]
                .get("encode_user_path")
                .is_none()
        );
        assert_eq!(
            cluster.resolved()["shards"]["forest"]["settings"]["encode_user_path"],
            false
        );
        assert!(
            cluster.files().unwrap()[Path::new("forest/server.ini")]
                .contains("encode_user_path = false\n")
        );
        cluster
            .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        assert!(
            fs::read_to_string(&server)
                .unwrap()
                .contains("encode_user_path = false\n")
        );

        // Maintenance must repair a file that omits the setting even when its effective value agrees.
        let source = fs::read_to_string(&server)
            .unwrap()
            .replace("encode_user_path = false\n", "");
        fs::write(&server, source).unwrap();
        let cluster = ClusterConfig::load(&room).unwrap();
        assert_eq!(
            cluster
                .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
                .unwrap(),
            vec![PathBuf::from("forest/server.ini")]
        );
        assert!(
            cluster
                .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
                .unwrap()
                .is_empty()
        );

        let mut value = cluster.into_value();
        value["shards"]["forest"]["settings"]["encode_user_path"] = json!(true);
        let encoded = ClusterConfig::from_value(value).unwrap();
        encoded
            .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        assert!(
            fs::read_to_string(&server)
                .unwrap()
                .contains("encode_user_path = true\n")
        );
        assert_eq!(
            ClusterConfig::load(&room).unwrap().resolved()["shards"]["forest"]["settings"]["encode_user_path"],
            true
        );
    }

    #[test]
    fn native_models_reject_invalid_presence_collections_and_world_values() {
        for (model, value) in [
            ("ShardSettings", json!({"encode_user_path":null})),
            ("ClusterSettings", json!({"autosaver_enabled":null})),
            ("ClusterConfig", json!({"shards":{}})),
            ("WorkshopDownloads", json!({"items":[1,1]})),
            ("ModSettings", json!({"force_enabled":["local","local"]})),
            (
                "ModOverride",
                json!({"configuration_options":{"value":null}}),
            ),
            ("QuagmireOverrides", json!({"loop_percent":1.01})),
            ("QuagmireOverrides", json!({"loop_percent":-0.01})),
            ("WorldgenOverride", json!({"overrides":{"kind":"forest"}})),
            (
                "WorldgenOverride",
                json!({"overrides":{"kind":"forest","values":{},"unexpected":true}}),
            ),
        ] {
            assert!(validate(model, &value).is_err(), "{model}: {value}");
        }
        for value in [0, 1] {
            validate("QuagmireOverrides", &json!({"loop_percent":value})).unwrap();
        }
        let values = json!({"cluster_name":null,"pvp":false});
        assert_eq!(
            ClusterSettings::from_value(values.clone())
                .unwrap()
                .into_value(),
            values
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
        assert_eq!(shard.as_value(), json!({"encode_user_path":false}));
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
