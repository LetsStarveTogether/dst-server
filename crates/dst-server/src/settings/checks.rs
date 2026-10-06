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
        ensure!(
            self.is_master() || self.name().as_deref() != Some(""),
            "secondary shards require SHARD.name"
        );
        if let Some(id) = self.id() {
            ensure!(
                (self.is_master() && id == 1) || (!self.is_master() && id >= 2),
                "master shard id must be 1; secondary shards require is_master = false and an id of at least 2"
            );
        }
        Ok(())
    }
}

impl Check for ShardConfig {
    fn check(&self) -> Result<()> {
        self.settings.check()?;
        if let Some(world) = self.world.as_ref().and_then(Option::as_ref) {
            world.check()?;
        }
        self.check_mods_and_level()
    }
}

impl ShardConfig {
    fn check_mods_and_level(&self) -> Result<()> {
        self.mods().check()?;
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
        let event_game = super::event_game_mode(&settings.game_mode()).is_some();
        for shard in self.shards.values() {
            shard.check()?;
            if event_game {
                ensure!(
                    shard.level.as_ref().is_some_and(Option::is_some),
                    "event game modes require level data for every shard"
                );
            }
        }
        settings.check_topology(
            self.shards
                .iter()
                .map(|(name, shard)| (name, &shard.settings)),
            configuration::ClusterKey::GenerateOnSave,
        )?;
        Ok(())
    }
}

impl ClusterSettings {
    /// A missing shared key can be generated when an editable configuration is saved.
    pub(crate) fn check_topology<'a>(
        &self,
        shards: impl ExactSizeIterator<Item = (&'a String, &'a ShardSettings)>,
        key: configuration::ClusterKey,
    ) -> Result<u16> {
        let sharded = self.shard_enabled();
        ensure!(
            shards.len() <= 1 || sharded,
            "shard_enabled must be true for multiple shards"
        );
        let mut names = BTreeSet::new();
        let mut ids = BTreeSet::new();
        let mut ports = BTreeSet::new();
        let mut master_ports = BTreeSet::new();
        let mut keys = BTreeSet::new();
        let mut masters = 0;
        for (directory, shard) in shards {
            configuration::validate_shard_name(directory)?;
            ensure!(
                names.insert(directory.to_lowercase()),
                "duplicate shard directory names"
            );
            if shard.is_master() {
                masters += 1;
            } else {
                ensure!(
                    shard.name().is_some(),
                    "secondary shards require SHARD.name"
                );
                ensure!(
                    shard.master_ip().or_else(|| self.master_ip()).is_some(),
                    "secondary shards require master_ip"
                );
            }
            if let Some(id) = shard.id() {
                ensure!(ids.insert(id), "shard ids must be unique");
            }
            for port in [shard.server_port(), shard.master_server_port()] {
                ensure!(ports.insert(port), "UDP port {port} is used more than once");
            }
            master_ports.insert(shard.master_port().unwrap_or_else(|| self.master_port()));
            if sharded {
                keys.insert(shard.cluster_key().or_else(|| self.cluster_key()));
            }
        }
        ensure!(masters == 1, "expected exactly one master shard");
        ensure!(
            master_ports.len() == 1,
            "all shards must use the same master_port"
        );
        let master_port = *master_ports.first().expect("a master port was validated");
        ensure!(
            !ports.contains(&master_port),
            "master_port conflicts with another UDP port"
        );
        ensure!(
            keys.len() <= 1 && !keys.contains(&Some(String::new())),
            "all enabled shards require the same non-empty cluster_key or must omit it"
        );
        ensure!(
            !sharded || key == configuration::ClusterKey::GenerateOnSave || !keys.contains(&None),
            "enabled shards require a non-empty shared cluster_key"
        );
        Ok(master_port.try_into()?)
    }
}

impl Check for RoomPreset {
    fn check(&self) -> Result<()> {
        if let Some(mods) = &self.mods {
            mods.check()?;
        }
        for (name, shard) in self.shards.iter().flatten() {
            configuration::validate_shard_name(name)?;
            shard.check_mods_and_level()?;
            if let Some(world) = shard.world.as_ref().and_then(Option::as_ref) {
                validate_world_values(&world.overrides().values())?;
            }
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
