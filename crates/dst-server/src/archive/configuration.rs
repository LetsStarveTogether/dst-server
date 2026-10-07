//! Reconstruct a room from native INI files and client shard indexes.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

use super::native::NativeFiles;
use crate::{
    lua,
    settings::{
        self, ClusterConfig, LevelDataOverride, ModOverrides, ModSettings, WorkshopDownloads,
        WorldgenOverride,
    },
};

pub(super) fn configuration(files: &NativeFiles) -> Result<(ClusterConfig, BTreeSet<String>)> {
    let names: BTreeSet<_> = files
        .entries
        .keys()
        .filter_map(|path| {
            let parts: Vec<_> = path.iter().collect();
            (parts.len() >= 2 && parts[0] != "mods").then(|| parts[0].to_str().unwrap())
        })
        .collect();
    ensure!(!names.is_empty(), "import contains no shards");
    let mut shards = BTreeMap::new();
    let mut indexes = BTreeMap::new();
    for name in &names {
        let base = Path::new(name);
        let ini = base.join("server.ini");
        let settings = if files.entries.contains_key(&ini) {
            settings::parse_ini("ShardSettings", files.text(&ini)?)?
        } else {
            json!({})
        };
        let mut shard = json!({"settings": settings});
        let index = base.join("save/shardindex");
        if files.entries.contains_key(&index) {
            let data = lua::parse_return_table(files.text(&index)?)?;
            let server = data["server"]
                .as_object()
                .context("shard index must contain a server table")?;
            ensure!(
                server.get("use_legacy_session_path") != Some(&json!(true)),
                "legacy client sessions must be migrated by the game before import"
            );
            indexes.insert(*name, data);
        }
        ensure!(
            files.entries.contains_key(&ini) || indexes.contains_key(name),
            "shard {name} has neither server.ini nor shardindex"
        );
        for (field, filename) in [
            ("world", "worldgenoverride.lua"),
            ("level", "leveldataoverride.lua"),
            ("mods", "modoverrides.lua"),
        ] {
            let path = base.join(filename);
            if files.entries.contains_key(&path) {
                let source = files.text(&path)?;
                shard[field] = match field {
                    "world" => WorldgenOverride::parse(source)?.into_value(),
                    "level" => LevelDataOverride::parse(source)?.into_value(),
                    _ => ModOverrides::parse(source)?.into_value(),
                };
            }
        }
        if shard["world"].is_null()
            && shard["level"].is_null()
            && let Some(data) = indexes.get(name)
        {
            let mut options = data["world"]["options"]
                .as_object()
                .context("shard index has no world options")?
                .clone();
            options.entry("overrides").or_insert_with(|| json!({}));
            shard["level"] = LevelDataOverride::parse(&format!(
                "return {}",
                lua::render_literal(&json!(options))?
            ))?
            .into_value();
        }
        shards.insert(*name, shard);
    }
    let explicit: Vec<_> = shards
        .iter()
        .filter_map(|(name, shard)| (shard["settings"]["is_master"] == true).then_some(*name))
        .collect();
    ensure!(explicit.len() <= 1, "import has multiple master shards");
    let master = if let Some(master) = explicit.first() {
        *master
    } else {
        let eligible: Vec<_> = shards
            .iter()
            .filter_map(|(name, shard)| (shard["settings"]["is_master"] != false).then_some(*name))
            .collect();
        let named: Vec<_> = eligible
            .iter()
            .copied()
            .filter(|name| name.eq_ignore_ascii_case("Master"))
            .collect();
        let candidates = if !named.is_empty() {
            named
        } else if eligible.len() == 1 {
            eligible
        } else {
            eligible
                .into_iter()
                .filter(|name| {
                    shards[name]["level"]["location"] == "forest"
                        || shards[name]["world"]["overrides"]["kind"] == "forest"
                })
                .collect()
        };
        ensure!(
            candidates.len() == 1,
            "cannot identify exactly one master shard; provide SHARD.is_master in server.ini"
        );
        candidates[0]
    };
    let mut settings = if files.entries.contains_key(Path::new("cluster.ini")) {
        settings::parse_ini("ClusterSettings", files.text(Path::new("cluster.ini"))?)?
    } else {
        json!({})
    };
    let settings = settings.as_object_mut().unwrap();
    if let Some(index) = indexes.get(master) {
        let server = index["server"].as_object().unwrap();
        for (client, native) in [
            ("name", "cluster_name"),
            ("description", "cluster_description"),
            ("game_mode", "game_mode"),
            ("server_language", "cluster_language"),
            ("pvp", "pvp"),
            ("max_players", "max_players"),
        ] {
            if let Some(value) = server.get(client) {
                settings.entry(native).or_insert_with(|| value.clone());
            }
        }
        if let Some(value) = server.get("online_mode") {
            let online = value
                .as_bool()
                .context("shard online_mode must be boolean")?;
            settings.entry("offline_cluster").or_insert(json!(!online));
        }
        if let Some(value) = server.get("privacy_type") {
            let privacy = value
                .as_u64()
                .context("shard privacy_type must be an integer")?;
            settings
                .entry("lan_only_cluster")
                .or_insert(json!(privacy == 2));
        }
    }
    if names.len() > 1 {
        settings.insert("shard_enabled".into(), json!(true));
    }
    settings.entry("master_ip").or_insert(json!("127.0.0.1"));
    settings.remove("cluster_key");
    let mut ports: BTreeSet<u64> = shards
        .values()
        .flat_map(|shard| {
            [
                shard["settings"]["server_port"].as_u64(),
                shard["settings"]["master_server_port"].as_u64(),
            ]
            .into_iter()
            .flatten()
        })
        .collect();
    ports.insert(
        settings
            .get("master_port")
            .and_then(Value::as_u64)
            .unwrap_or(10888),
    );
    let explicit_ids = shards
        .iter()
        .filter(|(_, shard)| shard["settings"]["id"].as_u64().is_some())
        .map(|(name, _)| (*name).to_owned())
        .collect();
    let mut ids: BTreeSet<u64> = shards
        .values()
        .filter_map(|shard| shard["settings"]["id"].as_u64())
        .collect();
    ids.insert(1);
    let mut order: Vec<_> = names.into_iter().collect();
    order.sort_by_key(|name| *name != master);
    for name in order {
        let shard = shards.get_mut(name).unwrap();
        let settings = shard["settings"].as_object_mut().unwrap();
        settings.insert("is_master".into(), json!(name == master));
        settings.entry("name").or_insert(json!(name));
        if !settings.contains_key("id") || settings["id"].is_null() {
            let id = if name == master {
                1
            } else {
                (2..=u32::MAX as u64)
                    .find(|id| !ids.contains(id))
                    .context("no available shard ID")?
            };
            ids.insert(id);
            settings.insert("id".into(), json!(id));
        }
        for (field, start) in [("server_port", 10999), ("master_server_port", 27016)] {
            if !settings.contains_key(field) {
                let port = (start..=u16::MAX as u64)
                    .find(|port| !ports.contains(port))
                    .context("no available shard port")?;
                ports.insert(port);
                settings.insert(field.into(), json!(port));
            }
        }
        // Import detects player names directly; stale index/INI encoding flags are irrelevant.
        settings.insert("encode_user_path".into(), json!(false));
        settings.remove("cluster_key");
        if shard["mods"].is_null() {
            let native = files
                .entries
                .contains_key(&Path::new(name).join("server.ini"));
            let mods = indexes
                .get(name)
                .and_then(|index| index.get("enabled_mods"))
                .filter(|mods| native || mods.as_object().is_some_and(|mods| !mods.is_empty()))
                .or_else(|| {
                    if native {
                        None
                    } else {
                        indexes
                            .get(master)
                            .and_then(|index| index.get("enabled_mods"))
                    }
                })
                .cloned()
                .unwrap_or_else(|| json!({}));
            shard["mods"] =
                ModOverrides::parse(&format!("return {}", lua::render_literal(&mods)?))?
                    .into_value();
        }
    }
    let mut cluster = json!({"settings": settings, "shards": shards});
    for (field, path) in [
        ("mod_settings", "mods/modsettings.lua"),
        ("downloads", "mods/dedicated_server_mods_setup.lua"),
    ] {
        if files.entries.contains_key(Path::new(path)) {
            let source = files.text(Path::new(path))?;
            cluster[field] = if field == "mod_settings" {
                ModSettings::parse(source)?.into_value()
            } else {
                WorkshopDownloads::parse(source)?.into_value()
            };
        }
    }
    Ok((ClusterConfig::from_value(cluster)?, explicit_ids))
}
