//! Reconstruct the native configuration stored in a client's shard indexes.

use std::collections::BTreeSet;
use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde_json::json;

use super::native::NativeFiles;
use crate::{
    lua,
    settings::{
        self, ClusterConfig, LevelDataOverride, ModOverrides, ModSettings, WorkshopDownloads,
        WorldgenOverride,
    },
};

pub(super) fn configuration(files: &NativeFiles) -> Result<ClusterConfig> {
    let names: BTreeSet<_> = files
        .entries
        .keys()
        .filter_map(|path| {
            let parts: Vec<_> = path.iter().collect();
            (parts.len() >= 2 && parts[0] != "mods").then(|| parts[0].to_str().unwrap())
        })
        .collect();
    ensure!(
        names.contains("Master") && names.iter().all(|name| matches!(*name, "Master" | "Caves")),
        "client import requires Master and optionally Caves"
    );
    let master = lua::parse_return_table(files.text(Path::new("Master/save/shardindex"))?)?;
    let server = master["server"]
        .as_object()
        .context("client shard index must contain a server table")?;
    let mut cluster = json!({"settings":{}, "shards":{}});
    if files.entries.contains_key(Path::new("cluster.ini")) {
        cluster["settings"] =
            settings::parse_ini("ClusterSettings", files.text(Path::new("cluster.ini"))?)?;
    }
    for (client, native) in [
        ("name", "cluster_name"),
        ("description", "cluster_description"),
        ("game_mode", "game_mode"),
        ("pvp", "pvp"),
        ("max_players", "max_players"),
    ] {
        if let Some(value) = server.get(client) {
            cluster["settings"][native] = value.clone();
        }
    }
    if let Some(value) = server.get("online_mode") {
        cluster["settings"]["offline_cluster"] = json!(
            !value
                .as_bool()
                .context("client online_mode must be boolean")?
        );
    }
    if let Some(value) = server.get("privacy_type") {
        cluster["settings"]["lan_only_cluster"] = json!(
            value
                .as_u64()
                .context("client privacy_type must be an integer")?
                == 2
        );
    }
    cluster["settings"]["shard_enabled"] = json!(names.len() > 1);
    cluster["settings"]["master_ip"] = json!("127.0.0.1");
    // Imported credentials are removed by the shared normalization step.
    cluster["settings"]
        .as_object_mut()
        .unwrap()
        .remove("cluster_key");
    for (index, name) in ["Master", "Caves"]
        .into_iter()
        .filter(|name| names.contains(name))
        .enumerate()
    {
        let base = Path::new(name);
        let data = lua::parse_return_table(files.text(&base.join("save/shardindex"))?)?;
        let server = data["server"]
            .as_object()
            .context("client shard index must contain a server table")?;
        ensure!(
            server.get("use_legacy_session_path") != Some(&json!(true)),
            "legacy client sessions must be migrated by the game before import"
        );
        let encoded = server
            .get("encode_user_path")
            .map(|value| {
                value
                    .as_bool()
                    .context("client encode_user_path must be boolean")
            })
            .transpose()?
            .unwrap_or(false);
        let mut shard = json!({"settings":{
            "is_master": index == 0, "name":name, "id":index + 1,
            "server_port":10999 + index, "master_server_port":27016 + index,
            "encode_user_path":encoded,
        }});
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
        if shard["world"].is_null() && shard["level"].is_null() {
            let options = data["world"]["options"]
                .as_object()
                .context("client shard index has no world options")?;
            let mut options = options.clone();
            options.entry("overrides").or_insert_with(|| json!({}));
            shard["level"] = LevelDataOverride::parse(&format!(
                "return {}",
                lua::render_literal(&json!(options))?
            ))?
            .into_value();
        }
        if shard["mods"].is_null() {
            let mods = data["enabled_mods"]
                .as_object()
                .filter(|mods| !mods.is_empty())
                .map(|mods| json!(mods))
                .unwrap_or_else(|| {
                    master
                        .get("enabled_mods")
                        .cloned()
                        .unwrap_or_else(|| json!({}))
                });
            shard["mods"] =
                ModOverrides::parse(&format!("return {}", lua::render_literal(&mods)?))?
                    .into_value();
        }
        cluster["shards"][name] = shard;
    }
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
    ClusterConfig::from_value(cluster)
}
