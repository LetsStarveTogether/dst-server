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

/// Forge and Gorge each use distinct world and inventory rules.
pub(crate) fn event_game_mode(mode: &str) -> Option<&str> {
    matches!(mode, "lavaarena" | "quagmire").then_some(mode)
}

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
                    let secret =
                        matches!(key.as_str(), "token" | "cluster_key" | "cluster_password")
                            || key.starts_with("OTEL_EXPORTER_OTLP_") && key.ends_with("_HEADERS")
                            || key
                                .rsplit_once('_')
                                .is_some_and(|(_, suffix)| suffix.eq_ignore_ascii_case("proxy"))
                                && value.as_str().is_some_and(|proxy| proxy.contains('@'));
                    if secret && !value.is_null() {
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

        /// Apply model defaults. A preset keeps its explicit fields until it is built.
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

pub(crate) fn preset_kind(preset: &str) -> Option<&'static str> {
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

pub fn parse_ini(model: &str, source: &str) -> Result<Value> {
    match model {
        "ClusterSettings" => Ok(ClusterSettings::parse(source)?.into_value()),
        "ShardSettings" => Ok(ShardSettings::parse(source)?.into_value()),
        _ => bail!("unknown INI model"),
    }
}

pub fn render_ini(model: &str, sparse: &Value, multi: bool) -> Result<String> {
    match model {
        "ClusterSettings" => ClusterSettings::from_ref(sparse)?.render(multi),
        "ShardSettings" => ShardSettings::from_ref(sparse)?.render(multi),
        _ => bail!("unknown INI model"),
    }
}

fn render_ini_fields(spec: &[(&str, &[&str])], value: Value) -> Result<String> {
    let mut sections = Vec::new();
    for (section, fields) in spec {
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
        Ok(Self(types::ClusterSettings::from_ini(
            configuration::Ini::parse(Path::new("cluster.ini"), source, Some(false))?
                .into_sections(),
        )?))
    }
    pub fn render(&self, multi: bool) -> Result<String> {
        let settings = Self(self.0.clone().for_shards(multi));
        render_ini_fields(types::ClusterSettings::SECTIONS, settings.into_value())
    }
}
impl ShardSettings {
    pub fn parse(source: &str) -> Result<Self> {
        Ok(Self(types::ShardSettings::from_ini(
            configuration::Ini::parse(Path::new("server.ini"), source, Some(true))?.into_sections(),
        )?))
    }
    pub fn render(&self, multi: bool) -> Result<String> {
        let mut value = self.as_value();
        value["encode_user_path"] = json!(self.0.encode_user_path());
        if multi {
            value["is_master"] = json!(self.0.is_master());
        }
        render_ini_fields(types::ShardSettings::SECTIONS, value)
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
    // The native file lists each item once, including Mods enabled by this room.
    fn exclude_enabled(&mut self, enabled: &BTreeSet<u64>) {
        if let Some(items) = &mut self.0.items {
            items.retain(|id| !enabled.contains(id));
        }
    }

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

fn worldgen_kind(table: &Map<String, Value>, overrides: &Value) -> &'static str {
    let presets = ["worldgen_preset", "settings_preset"]
        .map(|field| table.get(field).and_then(Value::as_str));
    if let Some(kind) = presets.into_iter().flatten().find_map(preset_kind) {
        return match kind {
            "forest" if ForestOverrides::from_ref(overrides).is_err() => "loaded_forest",
            "cave" if CaveOverrides::from_ref(overrides).is_err() => "loaded_cave",
            kind => kind,
        };
    }
    if overrides.as_object().is_some_and(Map::is_empty) {
        return "world";
    }
    if presets.iter().any(Option::is_some) {
        return "custom";
    }
    let mut candidates = [
        ("forest", ForestOverrides::from_ref(overrides).is_ok()),
        ("cave", CaveOverrides::from_ref(overrides).is_ok()),
        ("quagmire", QuagmireOverrides::from_ref(overrides).is_ok()),
        ("lavaarena", LavaArenaOverrides::from_ref(overrides).is_ok()),
    ]
    .into_iter()
    .filter_map(|(kind, valid)| valid.then_some(kind));
    match (candidates.next(), candidates.next()) {
        (Some(only), None) => only,
        _ => "custom",
    }
}

fn level_kind(location: &str) -> &'static str {
    match location {
        "forest" => "loaded_forest",
        "cave" => "loaded_cave",
        "quagmire" => "quagmire",
        "lavaarena" => "lavaarena",
        _ => "custom",
    }
}

// The game reads only the returned table. Some SDK models share the same native fields.
const OVERRIDE_KIND_COMMENT: &str = "-- dst-server overrides.kind = ";

fn saved_override_kind(source: &str) -> Option<&str> {
    source
        .trim_end()
        .trim_end_matches('\0')
        .rsplit(['\r', '\n'])
        .map(str::trim)
        .take_while(|line| line.is_empty() || line.starts_with("--"))
        .find_map(|line| line.strip_prefix(OVERRIDE_KIND_COMMENT))
}

fn render_override(table: &Value, kind: &str, inferred: &str) -> Result<String> {
    let mut source = files::render_lua_table(table)?;
    if kind != inferred {
        source.push_str(OVERRIDE_KIND_COMMENT);
        source.push_str(kind);
        source.push('\n');
        ensure!(
            source.len() <= lua::MAX_SOURCE_BYTES,
            "Lua source exceeds the byte limit"
        );
    }
    Ok(source)
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
        ensure!(
            table.keys().all(|name| known.contains(&name.as_str())),
            "unknown world override field"
        );
        if let Some(preset) = table.remove("preset") {
            table
                .entry("worldgen_preset")
                .or_insert_with(|| preset.clone());
            table.entry("settings_preset").or_insert(preset);
        }
        let overrides = table.remove("overrides").unwrap_or_else(|| json!({}));
        object(&overrides)?;
        let kind = kind
            .or_else(|| saved_override_kind(source))
            .unwrap_or_else(|| worldgen_kind(&table, &overrides));
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
        let Some(overrides) = &self.0.overrides else {
            return files::render_lua_table(&table);
        };
        let mut values = overrides.values();
        values
            .as_object_mut()
            .context("overrides must be an object")?
            .retain(|_, value| !value.is_null());
        table["overrides"] = values;
        let inferred = worldgen_kind(object(&table)?, &table["overrides"]);
        render_override(&table, overrides.kind(), inferred)
    }
}

impl LevelDataOverride {
    pub fn parse(source: &str) -> Result<Self> {
        Self::parse_with_kind(source, None)
    }
    pub fn parse_with_kind(source: &str, kind: Option<&str>) -> Result<Self> {
        let mut value = lua::parse_return_table(source)?;
        object(&value["overrides"])?;
        let kind = kind
            .or_else(|| saved_override_kind(source))
            .unwrap_or_else(|| level_kind(value["location"].as_str().unwrap_or_default()));
        value["overrides"] = json!({"kind":kind, "values":value["overrides"]});
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
        render_override(
            &Value::Object(table),
            self.0.overrides.kind(),
            level_kind(&self.0.location),
        )
    }
}

impl ClusterConfig {
    pub(crate) fn copy_permissions_from(&mut self, other: &Self) {
        self.0.adminlist.clone_from(&other.0.adminlist);
        self.0.blocklist.clone_from(&other.0.blocklist);
        self.0.whitelist.clone_from(&other.0.whitelist);
    }

    pub(crate) fn from_files(files: &BTreeMap<PathBuf, Vec<u8>>) -> Result<Self> {
        Self::from_value(Self::read_files(files)?)
    }

    pub(crate) fn read_files(files: &BTreeMap<PathBuf, Vec<u8>>) -> Result<Value> {
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
        Self::from_value(load_cluster(
            &|path| room.read_optional_text(path),
            &|path| room.read_optional_bytes(path),
            room.shard_directories()?,
            world_kinds,
            level_kinds,
        )?)
    }
    pub fn files(&self) -> Result<FileChanges> {
        let settings = self.0.settings();
        let sharded = settings.shard_enabled();
        let mut files = FileChanges::new();
        files.insert(
            PathBuf::from("cluster.ini"),
            render_ini_fields(
                types::ClusterSettings::SECTIONS,
                ClusterSettings(settings).into_value(),
            )?,
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
        for (name, shard) in &self.0.shards {
            files.insert(
                PathBuf::from(name).join("server.ini"),
                ShardSettings(shard.settings.clone()).render(sharded)?,
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
        let directories = room.shard_directories()?;
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
            for name in &directories {
                if let Some(source) = room.read_optional_text(Path::new(name).join("server.ini"))? {
                    keys.insert(
                        native_key("server.ini", &source)?.or_else(|| old_cluster_key.clone()),
                    );
                }
            }
            if keys.is_empty() {
                keys.insert(old_cluster_key);
            }
            ensure!(
                keys.len() == 1,
                "existing shards do not share one cluster_key"
            );
            let key = keys
                .into_iter()
                .next()
                .flatten()
                .filter(|key| !key.is_empty())
                .map(Ok)
                .unwrap_or_else(new_cluster_key)?;
            configuration
                .settings
                .get_or_insert_with(Default::default)
                .cluster_key = Some(Some(key));
        }
        let existing_mod_settings =
            if configuration.mod_settings.is_none() || configuration.downloads.is_none() {
                room.read_optional_bytes("mods/modsettings.lua")?
                    .map(|source| ModSettings::parse(&String::from_utf8_lossy(&source)))
            } else {
                None
            };
        let inherited_settings = existing_mod_settings
            .as_ref()
            .filter(|_| configuration.mod_settings.is_none());
        if let Some(settings) = inherited_settings {
            match settings {
                Ok(settings) => configuration.mod_settings = Some(settings.0.clone()),
                Err(_) if configuration.downloads.is_some() => bail!(
                    "provide mod_settings with downloads or edit the native setup file because enabled Mods cannot be determined from mods/modsettings.lua"
                ),
                Err(_) => {}
            }
        }
        let mut preserve_download_script = false;
        if configuration.downloads.is_none()
            && let Some(source) =
                room.read_optional_bytes("mods/dedicated_server_mods_setup.lua")?
        {
            match WorkshopDownloads::parse(&String::from_utf8_lossy(&source)) {
                Ok(mut downloads) => {
                    if downloads
                        .0
                        .items
                        .as_ref()
                        .is_some_and(|items| !items.is_empty())
                        && let Some(enabled) = existing_enabled_items(
                            room,
                            &directories,
                            existing_mod_settings.as_ref(),
                        )?
                    {
                        downloads.exclude_enabled(&enabled);
                    }
                    configuration.downloads = Some(downloads.0);
                }
                Err(_) => preserve_download_script = true,
            }
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
        if inherited_settings.is_some() {
            files.remove(Path::new("mods/modsettings.lua"));
        }
        if preserve_download_script {
            files.remove(Path::new("mods/dedicated_server_mods_setup.lua"));
        }
        let mut removed = Vec::new();
        for name in directories {
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

fn existing_enabled_items(
    room: &files::RoomReader,
    directories: &[String],
    mod_settings: Option<&Result<ModSettings>>,
) -> Result<Option<BTreeSet<u64>>> {
    let mut enabled = match mod_settings {
        Some(Ok(settings)) => settings.workshop_items(),
        Some(Err(_)) => return Ok(None),
        None => BTreeSet::new(),
    };
    for name in directories {
        let directory = Path::new(name);
        if room
            .read_optional_bytes(directory.join("server.ini"))?
            .is_none()
        {
            continue;
        }
        if let Some(source) = room.read_optional_bytes(directory.join("modoverrides.lua"))? {
            let Ok(mods) = ModOverrides::parse(&String::from_utf8_lossy(&source)) else {
                return Ok(None);
            };
            enabled.extend(mods.workshop_items());
        }
    }
    Ok(Some(enabled))
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
) -> Result<Value> {
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
    let mut enabled_items = BTreeSet::new();
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
                    "mods" => {
                        let mods = ModOverrides::parse(&source)?;
                        enabled_items.extend(mods.workshop_items());
                        mods.into_value()
                    }
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
    configuration::check_native_roles(
        settings["shard_enabled"].as_bool().unwrap_or(false),
        shards
            .values()
            .map(|shard| shard["settings"]["is_master"].as_bool()),
    )?;
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
        let settings = ModSettings::parse(&source)?;
        enabled_items.extend(settings.workshop_items());
        cluster["mod_settings"] = settings.into_value();
    }
    if let Some(source) = read(Path::new("mods/dedicated_server_mods_setup.lua"))? {
        let mut downloads = WorkshopDownloads::parse(&source)?;
        downloads.exclude_enabled(&enabled_items);
        cluster["downloads"] = downloads.into_value();
    }
    Ok(cluster)
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
                        if let (Some(existing), Some(overrides)) =
                            (world.get("overrides"), patch["world"].get("overrides"))
                        {
                            ensure!(
                                existing["kind"] == overrides["kind"],
                                "cannot compose different world override kinds"
                            );
                            let mut values = existing["values"].clone();
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
        settings: Option<Map<String, Value>>,
    ) -> Result<ClusterConfig> {
        let value = self.as_value();
        let mut configuration = json!({"settings": value.get("settings").cloned().unwrap_or_else(|| json!({})), "shards":value.get("shards").cloned().unwrap_or_else(|| json!({})), "token":token});
        if let Some(settings) = settings {
            configuration["settings"]
                .as_object_mut()
                .expect("preset settings")
                .extend(settings);
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
        let templates = crate::rooms::template_names();
        assert_eq!(templates.len(), 12);
        for name in templates {
            let directory = tempfile::tempdir().unwrap();
            let cluster =
                crate::rooms::build_template(name, 0, "private-token", Some("private-key"), None)
                    .unwrap_or_else(|error| panic!("{name}: {error}"));
            let expected = cluster.files().unwrap();
            let expanded = ClusterConfig::from_value(cluster.resolved())
                .unwrap()
                .files()
                .unwrap();
            for (path, source) in &expected {
                if matches!(
                    path.file_name().and_then(|name| name.to_str()),
                    Some("worldgenoverride.lua" | "leveldataoverride.lua")
                ) {
                    assert_eq!(
                        lua::parse_return_table(&expanded[path]).unwrap(),
                        lua::parse_return_table(source).unwrap(),
                        "{name}: {}",
                        path.display()
                    );
                }
            }
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
    fn resolved_sharding_matches_the_native_files_when_the_flag_is_omitted() {
        let mut value = json!({
            "settings":{"master_ip":"127.0.0.1"},
            "shards":{
                "Master":{"settings":{}},
                "Caves":{"settings":{"is_master":false,"name":"Caves","server_port":11000,"master_server_port":27017}}
            }
        });
        let cluster = ClusterConfig::from_value(value.clone()).unwrap();
        assert!(
            cluster.as_value()["settings"]
                .get("shard_enabled")
                .is_none()
        );
        let expanded = cluster.resolved();
        assert_eq!(expanded["settings"]["shard_enabled"], true);
        let directory = tempfile::tempdir().unwrap();
        let mut room = RoomLock::try_acquire(directory.path()).unwrap();
        ClusterConfig::from_value(expanded)
            .unwrap()
            .save(&mut room.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        assert_eq!(
            ClusterConfig::load(&room).unwrap().as_value()["settings"]["shard_enabled"],
            true
        );
        value["settings"]["shard_enabled"] = json!(false);
        assert!(ClusterConfig::from_value(value).is_err());
    }

    #[test]
    fn expanding_a_preset_keeps_inherited_room_settings() {
        let base = crate::rooms::preset("FOREST_CAVES").unwrap();
        let weekend =
            json!({"settings":{"cluster_name":"Weekend","cluster_password":"fixture","pvp":true}});
        let capacity = json!({"settings":{"max_players":12,"cluster_password":null}});
        let parts = [
            base,
            RoomPreset::from_value(weekend).unwrap(),
            RoomPreset::from_value(resolved("RoomPreset", &capacity).unwrap()).unwrap(),
        ];
        let cluster = RoomPreset::compose(&parts)
            .unwrap()
            .build("", None, None)
            .unwrap();
        let settings = cluster.data().settings();
        assert_eq!(settings.cluster_name().as_deref(), Some("Weekend"));
        assert_eq!(settings.cluster_password, Some(None));
        assert!(settings.pvp() && settings.shard_enabled());
        assert_eq!(settings.max_players(), 12);
    }

    #[test]
    fn preset_relationships_are_checked_when_the_cluster_is_built() {
        let base = RoomPreset::from_value(json!({
            "settings":{"max_players":32, "steam_group_id":123, "master_ip":"localhost"},
            "shards":{
                "forest":{"settings":{}},
                "caves":{"settings":{
                    "is_master":false, "name":"Caves", "server_port":11000, "master_server_port":27017
                }}
            }
        }))
        .unwrap();
        let patch = RoomPreset::from_value(json!({
            "settings":{"whitelist_slots":20, "steam_group_only":true},
            "shards":{"caves":{"settings":{"id":2}}}
        }))
        .unwrap();
        let composed = RoomPreset::compose(&[base, patch]).unwrap();
        let final_settings = Map::from_iter([("steam_group_admins".into(), true.into())]);
        let cluster = composed
            .build("", None, Some(final_settings))
            .unwrap()
            .into_value();
        assert_eq!(cluster["settings"]["whitelist_slots"], 20);
        assert_eq!(cluster["settings"]["steam_group_id"], 123);
        assert_eq!(cluster["settings"]["steam_group_admins"], true);
        assert_eq!(cluster["shards"]["caves"]["settings"]["id"], 2);

        let invalid = RoomPreset::from_value(json!({"settings":{"steam_group_id":0}})).unwrap();
        let invalid = RoomPreset::compose(&[composed, invalid]).unwrap();
        assert!(invalid.build("", None, None).is_err());
        for fields in [
            json!({"settings":{"whitelist_slots":"twenty"}}),
            json!({"settings":{"master_ip":"not a host"}}),
            json!({"shards":{"..":{"settings":{}}}}),
            json!({"mods":{"entries":{"bad/name":{}}}}),
        ] {
            assert!(RoomPreset::from_value(fields).is_err());
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
        let preset_only = RoomPreset::from_value(json!({"shards":{"forest":{
            "settings":{}, "world":{"worldgen_preset":"ENDLESS","settings_preset":"ENDLESS"}
        }}}))
        .unwrap();
        let expanded = RoomPreset::from_value(preset_only.resolved()).unwrap();
        for preset in [preset_only, expanded] {
            let world = &RoomPreset::compose(&[preset, base.clone()])
                .unwrap()
                .as_value()["shards"]["forest"]["world"];
            assert_eq!(world["worldgen_preset"], "ENDLESS");
            assert_eq!(world["settings_preset"], "ENDLESS");
            assert_eq!(
                world["overrides"],
                json!({"kind":"forest","values":{"day":"onlynight"}})
            );
        }
        let caves = RoomPreset::from_value(json!({"shards":{"forest":{
            "settings":{}, "world":{"overrides":{"kind":"cave","values":{}}}
        }}}))
        .unwrap();
        assert!(
            RoomPreset::compose(&[base.clone(), caves])
                .unwrap_err()
                .to_string()
                .contains("different world override kinds")
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
            json!({"cluster_key":"PRIVATE\u{00a0}"}),
            json!({"cluster_password":" PRIVATE"}),
            json!({"max_players":1.0}),
            json!({"steam_group_id":18446744073709551616_f64}),
        ] {
            let error = ClusterSettings::from_value(value).unwrap_err().to_string();
            assert!(!error.contains("PRIVATE"));
        }
        assert!(ShardSettings::from_value(json!({"name":"\u{3000}Caves"})).is_err());
        let settings = ClusterSettings::from_value(json!({"cluster_name":"洞 穴"})).unwrap();
        assert_eq!(
            ClusterSettings::parse(&settings.render(false).unwrap())
                .unwrap()
                .as_value()["cluster_name"],
            "洞 穴"
        );
        let shard = ShardSettings::parse(
            "[STEAM]\nmaster_server_port=27016\n[ACCOUNT]\nencode_user_path=false\n",
        )
        .unwrap();
        assert_eq!(
            shard.as_value(),
            json!({"master_server_port":27016,"encode_user_path":false})
        );
        assert!(ShardSettings::parse("[STEAM]\nauthentication_port=8766\n").is_err());
        assert!(ShardSettings::from_value(json!({"authentication_port":8766})).is_err());
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
        let untyped = WorldgenOverride::parse("return {overrides={day='onlynight'}}").unwrap();
        assert_eq!(
            untyped.as_value()["overrides"],
            json!({"kind":"custom","values":{"day":"onlynight"}})
        );
        let event =
            WorldgenOverride::parse("return {overrides={task_set='quagmire_taskset'}}").unwrap();
        assert_eq!(event.as_value()["overrides"]["kind"], "quagmire");
        let modded = WorldgenOverride::parse(
            "return {preset='SURVIVAL_TOGETHER',overrides={custom_density=0.25}}",
        )
        .unwrap();
        assert_eq!(modded.as_value()["overrides"]["kind"], "loaded_forest");
        for (name, kind) in [
            ("ENDLESS", "forest"),
            ("DST_CAVE", "cave"),
            ("MOD_PRESET", "world"),
        ] {
            let preset = WorldgenOverride::from_value(json!({"worldgen_preset":name})).unwrap();
            let source = preset.render().unwrap();
            assert_eq!(
                WorldgenOverride::parse(&source).unwrap().as_value()["overrides"]["kind"],
                kind
            );
            let expanded = preset.resolved();
            assert!(expanded.get("overrides").is_none());
            assert_eq!(
                WorldgenOverride::from_value(expanded)
                    .unwrap()
                    .render()
                    .unwrap(),
                source
            );
        }
        let world = WorldgenOverride::parse_with_kind(
            "return {overrides={day='onlynight'}}",
            Some("forest"),
        )
        .unwrap();
        assert_eq!(world.as_value()["enabled"], false);
        assert_eq!(
            WorldgenOverride::parse(&world.render().unwrap().replace('\n', "\r")).unwrap(),
            world
        );
        let native = WorldgenOverride::parse("return {preset='SURVIVAL_TOGETHER', override_enabled=true, overrides={day='onlynight',grass='often'}}").unwrap();
        assert_eq!(
            native.as_value()["overrides"]["values"],
            json!({"day":"onlynight","grass":"often"})
        );
        assert_eq!(native.as_value()["worldgen_preset"], "SURVIVAL_TOGETHER");
        assert_eq!(native.as_value()["settings_preset"], "SURVIVAL_TOGETHER");
        assert_eq!(
            WorldgenOverride::parse(&native.render().unwrap()).unwrap(),
            native
        );
        for source in [
            "return {actualpreset='SURVIVAL_TOGETHER'}",
            "return {preset='SURVIVAL_TOGETHER',presetdata={overrides={{'day','onlynight'}}}}",
            "return {preset='SURVIVAL_TOGETHER',resources={grass='often'}}",
            "return {preset='SURVIVAL_TOGETHER',enabled=true}",
        ] {
            assert!(WorldgenOverride::parse(source).is_err(), "{source}");
        }
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
    fn explicit_downloads_replace_extras_and_follow_current_enabled_mods() {
        let directory = tempfile::tempdir().unwrap();
        let mut lock = RoomLock::try_acquire(directory.path()).unwrap();
        let reader = files::RoomReader::open(directory.path()).unwrap();
        let mut save = |fields: &Value| {
            ClusterConfig::from_ref(fields)?
                .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
        };
        let setup = || {
            let source = reader
                .read_text("mods/dedicated_server_mods_setup.lua")
                .unwrap();
            WorkshopDownloads::parse(&source).unwrap().into_value()
        };
        let shard_mods = json!({"entries":{"workshop-20":{"enabled":true}}});
        let mut fields = json!({
            "settings":{"master_ip":"localhost"},
            "shards":{
                "forest":{"settings":{}, "mods":shard_mods},
                "caves":{"settings":{"is_master":false,"name":"Caves","server_port":11000,"master_server_port":27017}, "mods":shard_mods}
            },
            "mod_settings":{"force_enabled":["30"]}, "downloads":{"items":[11],"collections":[41]}
        });
        save(&fields).unwrap();
        let fresh = json!({"shards":{"forest":{"settings":{}}}, "mod_settings":{}});
        save(&fresh).unwrap();
        assert_eq!(setup(), json!({"items":[11],"collections":[41]}));
        save(&fields).unwrap();
        fields.as_object_mut().unwrap().remove("mod_settings");
        fields["downloads"] = json!({"items":[12],"collections":[42]});
        save(&fields).unwrap();
        assert_eq!(setup(), json!({"items":[12,20,30],"collections":[42]}));

        fields.as_object_mut().unwrap().remove("downloads");
        save(&resolved("ClusterConfig", &fields).unwrap()).unwrap();
        assert_eq!(setup(), json!({"items":[12,20,30],"collections":[42]}));
        fields["downloads"] = json!({});
        save(&fields).unwrap();
        assert_eq!(setup(), json!({"items":[20,30],"collections":[]}));

        let mut loaded = ClusterConfig::load(&reader).unwrap().into_value();
        assert_eq!(loaded["downloads"]["items"], json!([]));
        loaded["shards"]["forest"]["mods"] = json!({});
        save(&loaded).unwrap();
        assert_eq!(setup(), json!({"items":[20,30],"collections":[]}));
        loaded["shards"]["caves"]["mods"] = json!({});
        save(&loaded).unwrap();
        assert_eq!(setup(), json!({"items":[30],"collections":[]}));
        loaded["mod_settings"] = json!({});
        save(&loaded).unwrap();
        assert_eq!(setup(), json!({"items":[],"collections":[]}));
    }

    #[test]
    fn expanded_config_preserves_omitted_launch_token_and_mod_settings_on_disk() {
        let directory = tempfile::tempdir().unwrap();
        let mut lock = RoomLock::try_acquire(directory.path()).unwrap();
        let token = directory.path().join("cluster_token.txt");
        let mods = directory.path().join("mods/modsettings.lua");
        fs::create_dir(directory.path().join("mods")).unwrap();
        let existing_token = "fixture-token\n";
        let existing_mods = "local name = '42'\nForceEnableMod(name)\n";
        fs::write(&token, existing_token).unwrap();
        fs::write(&mods, existing_mods).unwrap();
        let setup = directory
            .path()
            .join("mods/dedicated_server_mods_setup.lua");
        let existing_setup = "local id = '42'\nServerModSetup(id)\n";
        fs::write(&setup, existing_setup).unwrap();

        let replacement = ClusterConfig::from_value(json!({
            "shards":{"forest":{"settings":{}}}, "downloads":{}
        }))
        .unwrap();
        let error = replacement
            .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap_err();
        assert!(error.to_string().contains("provide mod_settings"));
        assert_eq!(fs::read_to_string(&setup).unwrap(), existing_setup);

        for (mut fields, expected_token, expected_mods) in [
            (json!({}), existing_token, existing_mods),
            (json!({"token":"","mod_settings":{}}), "", ""),
        ] {
            fields["shards"] = json!({"forest":{"settings":{}}});
            let expanded = resolved("ClusterConfig", &fields).unwrap();
            ClusterConfig::from_value(expanded)
                .unwrap()
                .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
                .unwrap();
            assert_eq!(fs::read_to_string(&token).unwrap(), expected_token);
            assert_eq!(fs::read_to_string(&mods).unwrap(), expected_mods);
            assert_eq!(fs::read_to_string(&setup).unwrap(), existing_setup);
        }
        replacement
            .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        assert_eq!(fs::read_to_string(&setup).unwrap(), "");
    }

    #[test]
    fn offline_save_repairs_a_missing_native_shard_key() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path();
        let mut lock = RoomLock::try_acquire(root).unwrap();
        fs::create_dir(root.join("Master")).unwrap();
        fs::write(
            root.join("Master/server.ini"),
            "[SHARD]\nis_master = true\nname =\n",
        )
        .unwrap();
        fs::write(root.join("cluster.ini"), "[SHARD]\ncluster_key =\n").unwrap();
        assert!(!configuration::discover(root).unwrap().shard_enabled);
        let standalone = ClusterConfig::load(&lock).unwrap();
        assert_eq!(standalone.as_value()["settings"]["cluster_key"], "");
        assert_eq!(
            standalone.as_value()["shards"]["Master"]["settings"]["name"],
            ""
        );

        fs::write(
            root.join("cluster.ini"),
            "[SHARD]\nshard_enabled = true\nmaster_ip = localhost\n",
        )
        .unwrap();
        assert!(
            configuration::discover(root)
                .unwrap_err()
                .to_string()
                .contains("cluster_key")
        );
        fs::create_dir(root.join("Caves")).unwrap();
        fs::write(root.join("Caves/server.ini"), "[SHARD]\nis_master = false\nname = Caves\n[NETWORK]\nserver_port = 11000\n[STEAM]\nmaster_server_port = 27017\n").unwrap();
        assert!(
            configuration::discover(root)
                .unwrap_err()
                .to_string()
                .contains("cluster_key")
        );
        let editable = ClusterConfig::load(&lock).unwrap();
        editable
            .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        assert_eq!(configuration::discover(root).unwrap().shards.len(), 2);
        assert!(
            ClusterConfig::load(&lock).unwrap().as_value()["settings"]["cluster_key"]
                .as_str()
                .is_some_and(|key| !key.is_empty())
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
        assert_eq!(
            ClusterConfig::load(&lock).unwrap().as_value()["shards"]["A"]["world"]["overrides"]["kind"],
            "custom"
        );
        let kinds = BTreeMap::from([("A".to_owned(), "forest".to_owned())]);
        assert_eq!(
            ClusterConfig::load_with_kinds(&lock, &kinds, &BTreeMap::new())
                .unwrap()
                .as_value()["shards"]["A"]["world"]["overrides"]["kind"],
            "forest"
        );
        fs::write(
            directory.path().join("A/modoverrides.lua"),
            "return generate_mods()",
        )
        .unwrap();
        assert!(ClusterConfig::load(&lock).is_err());
        assert!(configuration::discover(directory.path()).is_ok());
    }

    #[tokio::test]
    async fn native_room_and_archive_keep_world_and_level_override_models() {
        use crate::archive::{ImportSource, import_cluster};

        let world = json!({"kind":"forest","values":{"day":"onlynight"}});
        let level = json!({"kind":"forest","values":{"task_set":"default"}});
        let cluster = ClusterConfig::from_value(json!({"settings":{}, "shards":{
            "Master":{"settings":{"is_master":true}, "world":{"overrides":world},
                "level":{"id":"SAVED","name":"world","desc":"","location":"forest","overrides":level}}
        }})).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let mut lock = RoomLock::try_acquire(directory.path()).unwrap();
        cluster
            .save(&mut lock.while_stopped(), files::PermissionFiles::Preserve)
            .unwrap();
        let from_room = ClusterConfig::load(&lock).unwrap();
        drop(lock);
        let archive = import_cluster(ImportSource::Path(directory.path().to_owned()))
            .await
            .unwrap();
        for read in [&from_room, archive.configuration()] {
            assert_eq!(
                read.as_value()["shards"]["Master"]["world"]["overrides"],
                world
            );
            assert_eq!(
                read.as_value()["shards"]["Master"]["level"]["overrides"],
                level
            );
        }
    }
}
