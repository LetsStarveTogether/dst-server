//! Relationships that require more than a field's declared type and constraints.

use std::{collections::BTreeSet, sync::LazyLock};

use anyhow::{Result, ensure};

use super::{types::*, validate_mod_name, validate_world_values, workshop_id};
use crate::configuration;

pub trait Check {
    fn check(&self) -> Result<()>;
}

impl Check for ClusterSettings {
    fn check(&self) -> Result<()> {
        if let Some(host) = self.master_ip() {
            configuration::validate_host(&host)?;
        }
        ensure!(
            !(self.steam_group_only() || self.steam_group_admins()) || self.steam_group_id() != 0,
            "Steam group restrictions require steam_group_id"
        );
        ensure!(
            self.whitelist_slots() <= self.max_players(),
            "whitelist_slots exceeds max_players"
        );
        Ok(())
    }
}

impl Check for ShardSettings {
    fn check(&self) -> Result<()> {
        if let Some(host) = self.master_ip() {
            configuration::validate_host(&host)?;
        }
        if let Some(id) = self.id() {
            ensure!(
                (self.is_master() && id == 1) || (!self.is_master() && id >= 2),
                "master shard id must be 1; secondary shard ids must be at least 2"
            );
        }
        Ok(())
    }
}

impl Check for ShardConfig {
    fn check(&self) -> Result<()> {
        self.settings.check()?;
        self.mods().check()?;
        if let Some(world) = self.world.as_ref().and_then(Option::as_ref) {
            world.check()?;
        }
        if let Some(level) = self.level.as_ref().and_then(Option::as_ref) {
            level.check()?;
        }
        Ok(())
    }
}

impl Check for ClusterConfig {
    fn check(&self) -> Result<()> {
        let settings = self.settings();
        settings.check()?;
        self.mod_settings().check()?;
        let multi = self.shards.len() > 1;
        ensure!(
            !multi || settings.shard_enabled != Some(false),
            "shard_enabled cannot be false for multiple shards"
        );
        let mut names = BTreeSet::new();
        let mut ids = BTreeSet::new();
        let mut ports = BTreeSet::new();
        let mut master_ports = BTreeSet::new();
        let mut keys = BTreeSet::new();
        let mut masters = 0;
        for (name, shard) in &self.shards {
            configuration::validate_shard_name(name)?;
            ensure!(
                names.insert(name.to_lowercase()),
                "duplicate shard directory names"
            );
            shard.check()?;
            let shard_settings = &shard.settings;
            let master = shard_settings.is_master();
            masters += usize::from(master);
            if !master {
                ensure!(
                    shard_settings.name().is_some_and(|name| !name.is_empty()),
                    "secondary shards require a name"
                );
            }
            if let Some(id) = shard_settings.id() {
                ensure!(ids.insert(id), "shard ids must be unique");
            }
            for port in [
                shard_settings.server_port(),
                shard_settings.master_server_port(),
            ] {
                ensure!(ports.insert(port), "UDP ports must be unique");
            }
            master_ports.insert(
                shard_settings
                    .master_port()
                    .unwrap_or_else(|| settings.master_port()),
            );
            keys.insert(
                shard_settings
                    .cluster_key()
                    .or_else(|| settings.cluster_key()),
            );
            if (multi || settings.shard_enabled()) && !master {
                ensure!(
                    shard_settings
                        .master_ip()
                        .or_else(|| settings.master_ip())
                        .is_some(),
                    "secondary shards require master_ip"
                );
            }
            if matches!(settings.game_mode().as_str(), "quagmire" | "lavaarena") {
                ensure!(
                    shard.level.as_ref().is_some_and(Option::is_some),
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
            !ports.contains(master_ports.first().expect("one master port")),
            "master_port conflicts with another UDP port"
        );
        ensure!(
            keys.len() == 1 && !keys.contains(&Some(String::new())),
            "all shards require the same non-empty cluster_key or must omit it"
        );
        Ok(())
    }
}

impl Check for RoomPreset {
    fn check(&self) -> Result<()> {
        self.settings().check()?;
        self.mods().check()?;
        for (name, shard) in self.shards.iter().flatten() {
            configuration::validate_shard_name(name)?;
            shard.check()?;
        }
        Ok(())
    }
}

impl Check for ModOverrides {
    fn check(&self) -> Result<()> {
        for name in self.entries.iter().flat_map(|entries| entries.keys()) {
            validate_mod_name(name)?;
        }
        Ok(())
    }
}

impl Check for ModSettings {
    fn check(&self) -> Result<()> {
        static LUA_NUMBER: LazyLock<regex::Regex> = LazyLock::new(|| {
            regex::Regex::new(r"(?i)^\s*[+-]?(?:nan|inf(?:inity)?|0x(?:[0-9a-f]+(?:\.[0-9a-f]*)?|\.[0-9a-f]+)(?:p[+-]?[0-9]+)?|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)\s*$").expect("numeric Mod name pattern")
        });
        for name in self.force_enabled.iter().flatten() {
            validate_mod_name(name)?;
            if LUA_NUMBER.is_match(name) {
                workshop_id(name)?;
            }
        }
        Ok(())
    }
}

impl Check for WorldgenOverride {
    fn check(&self) -> Result<()> {
        let kinds: BTreeSet<_> = [self.worldgen_preset(), self.settings_preset()]
            .iter()
            .flatten()
            .filter_map(|preset| super::preset_kind(preset))
            .collect();
        ensure!(
            kinds.len() <= 1,
            "different built-in world types cannot be combined"
        );
        let overrides = self.overrides();
        let kind = overrides.kind().trim_start_matches("loaded_");
        if let Some(expected) = kinds.first() {
            ensure!(
                !matches!(*expected, "quagmire" | "lavaarena") || kind == *expected,
                "event presets require matching overrides"
            );
            ensure!(
                !matches!(kind, "forest" | "cave" | "quagmire" | "lavaarena") || kind == *expected,
                "preset and overrides use different world types"
            );
        }
        validate_world_values(&overrides.values())
    }
}

impl Check for LevelDataOverride {
    fn check(&self) -> Result<()> {
        let values = self.overrides.values();
        validate_world_values(&values)?;
        ensure!(
            values["task_set"]
                .as_str()
                .is_some_and(|value| !value.is_empty()),
            "level data requires explicit task_set"
        );
        if let Some([start, end]) = self.background_node_range() {
            ensure!(start >= 0 && end >= start, "invalid background_node_range");
        }
        if self.numrandom_set_pieces().unwrap_or(0) > 0 {
            ensure!(
                self.random_set_pieces()
                    .is_some_and(|values| !values.is_empty()),
                "positive numrandom_set_pieces requires random_set_pieces"
            );
        }
        let kind = self.overrides.kind().trim_start_matches("loaded_");
        if matches!(kind, "forest" | "cave" | "quagmire" | "lavaarena") {
            ensure!(
                self.location == kind,
                "level location and override kind differ"
            );
        }
        if matches!(kind, "quagmire" | "lavaarena") {
            let id = kind.to_ascii_uppercase();
            ensure!(self.id == id, "event level has an incorrect id");
            for value in [self.settings_id(), self.worldgen_id()]
                .into_iter()
                .flatten()
            {
                ensure!(value == id, "event settings/worldgen ids differ");
            }
            if let Some(prefabs) = self.required_prefabs() {
                ensure!(
                    prefabs.contains(&format!("{kind}_portal")),
                    "event level is missing its portal prefab"
                );
            }
        }
        Ok(())
    }
}

macro_rules! field_checks_only {
    ($($model:ty),+ $(,)?) => {$(
        impl Check for $model {
            fn check(&self) -> Result<()> { Ok(()) }
        }
    )+};
}

field_checks_only!(
    ModOverride,
    WorkshopDownloads,
    WorldOverrides,
    ForestOverrides,
    CaveOverrides,
    QuagmireOverrides,
    LavaArenaOverrides,
    CustomWorldOverrides,
    LoadedForestOverrides,
    LoadedCaveOverrides
);
