//! Numbered native rooms, gameplay templates and validated configuration edits.

use std::{
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::LazyLock,
};

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Value, json};

use crate::{
    deployment::{ContainerUnit, DeploymentOptions},
    files::{self, PermissionFiles, RoomLock},
    policy::Policy,
    settings::{self, ClusterConfig, ClusterSettings},
};

pub const MAX_ROOM_SLOT: u16 = 299;

/// Describe the current native Room contract, including Agent policy and ports.
pub fn schema() -> Value {
    let mut schema = settings::schema("Room")
        .expect("bundled Room schema")
        .clone();
    let properties = schema["properties"]
        .as_object_mut()
        .expect("Room properties");
    properties.remove("schedule");
    properties.remove("recycle");
    properties.insert("policy".into(), json!({
        "type":"object", "additionalProperties":false,
        "properties":{
            "timezone":{"type":"string","default":"Asia/Shanghai"},
            "schedule":{"type":"array","default":[],"maxItems":1440,"items":{
                "type":"object","additionalProperties":false,"required":["start","end"],
                "properties":{
                    "start":{"type":"string","pattern":"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::00)?$"},
                    "end":{"type":"string","pattern":"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::00)?$"}
                }
            }},
            "mod_auto_update":{"type":"boolean","default":true},
            "idle_regeneration":{"type":"boolean","default":false}
        }
    }));
    schema["$defs"]["RoomDeployment"]["properties"]["ports"] = json!({
        "type":"array","default":[],"items":{
            "type":"object","additionalProperties":false,"required":["host","container"],
            "properties":{
                "host":{"type":"integer","minimum":1024,"maximum":65535},
                "container":{"type":"integer","minimum":1024,"maximum":65535},
                "protocol":{"const":"udp","default":"udp"}
            }
        }
    });
    schema
}

static PRESETS: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../resources/settings/presets.json"))
        .expect("valid bundled room presets")
});

#[derive(Clone, Debug, Serialize)]
pub struct Room {
    pub number: u16,
    pub template: Option<String>,
    #[serde(serialize_with = "serialize_cluster")]
    pub cluster: ClusterConfig,
    pub deployment: DeploymentOptions,
    pub policy: Policy,
}

fn without_permissions(mut cluster: Value) -> Value {
    if let Some(fields) = cluster.as_object_mut() {
        for field in settings::PERMISSION_FIELDS {
            fields.remove(field);
        }
    }
    cluster
}

fn serialize_cluster<S: Serializer>(
    cluster: &ClusterConfig,
    serializer: S,
) -> std::result::Result<S::Ok, S::Error> {
    without_permissions(settings::redact(cluster.as_value().clone())).serialize(serializer)
}

impl<'de> Deserialize<'de> for Room {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        Self::from_value(Value::deserialize(deserializer)?).map_err(serde::de::Error::custom)
    }
}

impl Room {
    pub fn new(number: u16, cluster: ClusterConfig) -> Result<Self> {
        let room = Self {
            number,
            template: None,
            cluster,
            deployment: DeploymentOptions::default(),
            policy: Policy::default(),
        };
        room.validate()?;
        Ok(room)
    }

    pub fn from_value(value: Value) -> Result<Self> {
        let Value::Object(mut value) = value else {
            bail!("room must be an object");
        };
        ensure!(
            value.keys().all(|key| matches!(
                key.as_str(),
                "number" | "template" | "cluster" | "deployment" | "policy"
            )),
            "unknown room field"
        );
        let room = Self {
            number: serde_json::from_value(
                value.remove("number").context("room number is required")?,
            )?,
            template: serde_json::from_value(value.remove("template").unwrap_or(Value::Null))?,
            cluster: ClusterConfig::from_value(
                value
                    .remove("cluster")
                    .context("room cluster is required")?,
            )?,
            deployment: serde_json::from_value(
                value.remove("deployment").unwrap_or_else(|| json!({})),
            )?,
            policy: serde_json::from_value(value.remove("policy").unwrap_or_else(|| json!({})))?,
        };
        room.validate()?;
        Ok(room)
    }

    pub fn validate(&self) -> Result<()> {
        validate_number(self.number)?;
        self.deployment.validate()?;
        self.policy.validate()?;
        Ok(())
    }

    /// The editable representation retains sparse native fields and secret values.
    pub fn as_value(&self) -> Value {
        let mut value = serde_json::to_value(self).expect("room JSON serialization");
        value["cluster"] = without_permissions(self.cluster.as_value().clone());
        value
    }

    pub fn resolved(&self) -> Value {
        let mut value = self.as_value();
        value["cluster"] = without_permissions(self.cluster.resolved());
        value
    }

    pub fn get(&self, pointer: &str) -> Result<Value> {
        self.validate()?;
        let value = settings::redact(self.resolved());
        let mut current = &value;
        for part in pointer_parts(pointer)? {
            current = pointer_item(current, &part)?;
        }
        Ok(current.clone())
    }

    pub fn edit(&self, pointer: &str, value: Value, unset: bool) -> Result<Self> {
        if unset {
            self.edit_many(&[], &[pointer])
        } else {
            self.edit_many(&[(pointer, value)], &[])
        }
    }

    pub fn edit_many(&self, changes: &[(&str, Value)], unset: &[&str]) -> Result<Self> {
        self.validate()?;
        let mut value = self.as_value();
        let defaults = self.resolved();
        for (pointer, change) in changes {
            edit_pointer(&mut value, &defaults, pointer, change, false)?;
        }
        for pointer in unset {
            edit_pointer(&mut value, &defaults, pointer, &Value::Null, true)?;
        }
        Self::from_value(value)
    }
}

fn validate_number(number: u16) -> Result<()> {
    ensure!(number <= MAX_ROOM_SLOT, "room number must be in 000-299");
    Ok(())
}

fn pointer_parts(pointer: &str) -> Result<Vec<String>> {
    if pointer.is_empty() {
        return Ok(Vec::new());
    }
    ensure!(pointer.starts_with('/'), "invalid JSON Pointer");
    pointer[1..]
        .split('/')
        .map(|part| {
            let mut decoded = String::with_capacity(part.len());
            let mut characters = part.chars();
            while let Some(character) = characters.next() {
                decoded.push(if character == '~' {
                    match characters.next() {
                        Some('0') => '~',
                        Some('1') => '/',
                        _ => bail!("invalid JSON Pointer escape"),
                    }
                } else {
                    character
                });
            }
            Ok(decoded)
        })
        .collect()
}

fn pointer_index(part: &str, length: usize) -> Result<usize> {
    ensure!(
        !part.is_empty()
            && part.bytes().all(|byte| byte.is_ascii_digit())
            && (part == "0" || !part.starts_with('0')),
        "invalid JSON Pointer array index"
    );
    let index = part
        .parse::<usize>()
        .context("invalid JSON Pointer array index")?;
    ensure!(index < length, "JSON Pointer array index is out of range");
    Ok(index)
}

fn pointer_item<'a>(value: &'a Value, part: &str) -> Result<&'a Value> {
    match value {
        Value::Object(object) => object.get(part).context("JSON Pointer field is absent"),
        Value::Array(array) => Ok(&array[pointer_index(part, array.len())?]),
        _ => bail!("JSON Pointer cannot traverse a scalar"),
    }
}

fn pointer_item_mut<'a>(value: &'a mut Value, part: &str) -> Result<&'a mut Value> {
    match value {
        Value::Object(object) => object.get_mut(part).context("JSON Pointer field is absent"),
        Value::Array(array) => {
            let index = pointer_index(part, array.len())?;
            Ok(&mut array[index])
        }
        _ => bail!("JSON Pointer cannot traverse a scalar"),
    }
}

fn edit_pointer(
    data: &mut Value,
    defaults: &Value,
    pointer: &str,
    value: &Value,
    unset: bool,
) -> Result<()> {
    let parts = pointer_parts(pointer)?;
    ensure!(
        !parts.is_empty() && parts[0] != "number",
        "room identity cannot be changed through a configuration field"
    );
    if parts[0] == "cluster" {
        let permission = if let Some(field) = parts.get(1) {
            settings::PERMISSION_FIELDS.contains(&field.as_str())
        } else {
            value.as_object().is_some_and(|fields| {
                settings::PERMISSION_FIELDS
                    .iter()
                    .any(|field| fields.contains_key(*field))
            })
        };
        ensure!(
            !permission,
            "permission lists are managed through player commands"
        );
    }
    let mut target = data;
    let mut fallback = defaults.clone();
    for part in &parts[..parts.len() - 1] {
        fallback = pointer_item(&fallback, part)
            .or_else(|_| pointer_item(target, part))?
            .clone();
        if let Value::Object(object) = target
            && !object.contains_key(part)
        {
            if fallback.is_object() {
                object.insert(part.clone(), json!({}));
            } else if fallback.is_array() {
                object.insert(part.clone(), fallback.clone());
            }
        }
        target = pointer_item_mut(target, part)?;
    }
    let key = parts.last().expect("nonempty pointer");
    match target {
        Value::Object(object) => {
            if unset {
                ensure!(object.remove(key).is_some(), "JSON Pointer field is absent");
            } else {
                object.insert(key.clone(), value.clone());
            }
        }
        Value::Array(array) if key == "-" && !unset => array.push(value.clone()),
        Value::Array(array) => {
            let index = pointer_index(key, array.len())?;
            if unset {
                array.remove(index);
            } else {
                array[index] = value.clone();
            }
        }
        _ => bail!("JSON Pointer parent is not an object or array"),
    }
    Ok(())
}

#[derive(Clone, Debug)]
pub struct RoomStore {
    root: PathBuf,
    quadlet_dir: Option<PathBuf>,
}

impl RoomStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            quadlet_dir: None,
        }
    }

    pub fn with_quadlet_dir(mut self, directory: impl Into<PathBuf>) -> Self {
        self.quadlet_dir = Some(directory.into());
        self
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn path(&self, number: u16) -> Result<PathBuf> {
        validate_number(number)?;
        Ok(self.root.join(format!("{number:03}")))
    }

    pub fn numbers(&self) -> Result<Vec<u16>> {
        match fs::symlink_metadata(&self.root) {
            Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
            state => {
                state?;
            }
        }
        let mut numbers = Vec::new();
        for name in files::directory_names(&self.root)? {
            if name.len() != 3 || !name.bytes().all(|byte| byte.is_ascii_digit()) {
                continue;
            }
            let number: u16 = name.parse()?;
            if number > MAX_ROOM_SLOT {
                continue;
            }
            match fs::symlink_metadata(self.path(number)?.join("cluster.ini")) {
                Ok(state) => {
                    ensure!(state.is_file(), "cluster.ini must be a regular file");
                    numbers.push(number);
                }
                Err(error) if error.kind() == ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(numbers)
    }

    pub fn load(&self, number: u16) -> Result<Room> {
        let guard = RoomLock::try_acquire(self.path(number)?)?;
        let mut control = guard.read_control()?;
        let mut room = Room {
            number,
            template: serde_json::from_value(control.remove("template").unwrap_or(Value::Null))?,
            cluster: ClusterConfig::load(&guard)?,
            deployment: DeploymentOptions::default(),
            policy: serde_json::from_value(control.remove("policy").unwrap_or_else(|| json!({})))?,
        };
        if let Some(directory) = &self.quadlet_dir {
            let path = directory.join(format!("dst-{number:03}.container"));
            match fs::symlink_metadata(&path) {
                Ok(_) => {
                    let unit = ContainerUnit::load(path)?;
                    ensure!(
                        unit.cluster == std::path::absolute(guard.directory())?,
                        "Quadlet must mount the selected room directory"
                    );
                    room.deployment = unit.options;
                }
                Err(error) if error.kind() == ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
        room.validate()?;
        Ok(room)
    }

    pub fn list(&self) -> Result<Vec<Room>> {
        self.numbers()?
            .into_iter()
            .map(|number| self.load(number))
            .collect()
    }

    pub fn save(&self, room: &Room) -> Result<Vec<PathBuf>> {
        room.validate()?;
        let directory = self.path(room.number)?;
        files::create_directory(&directory)?;
        let mut guard = RoomLock::try_acquire(&directory)?;
        guard.read_control()?;
        let changed = {
            let mut stopped = guard.while_stopped();
            stopped.recover()?;
            room.cluster.save(&mut stopped, PermissionFiles::Preserve)?
        };
        save_policy(&mut guard, room)?;
        Ok(changed
            .into_iter()
            .map(|path| directory.join(path))
            .collect())
    }

    pub fn save_policy(&self, room: &Room) -> Result<()> {
        room.validate()?;
        let mut guard = RoomLock::try_acquire(self.path(room.number)?)?;
        save_policy(&mut guard, room)
    }
}

fn save_policy(guard: &mut RoomLock, room: &Room) -> Result<()> {
    guard.update_control(|control| {
        control.insert("template".into(), serde_json::to_value(&room.template)?);
        control.insert("policy".into(), serde_json::to_value(&room.policy)?);
        Ok(())
    })
}

pub fn template_names() -> Vec<&'static str> {
    PRESETS["template_names"]
        .as_array()
        .expect("template names")
        .iter()
        .map(|value| value.as_str().expect("template name"))
        .collect()
}

pub fn preset_names() -> Vec<&'static str> {
    PRESETS["parts"]
        .as_object()
        .expect("preset parts")
        .keys()
        .map(String::as_str)
        .collect()
}

pub fn preset(name: &str) -> Result<settings::RoomPreset> {
    settings::RoomPreset::from_value(
        PRESETS["parts"]
            .get(name)
            .context("unknown room preset")?
            .clone(),
    )
}

pub fn build_template(
    name: &str,
    number: u16,
    token: &str,
    cluster_key: Option<&str>,
    settings: Option<&ClusterSettings>,
) -> Result<ClusterConfig> {
    validate_number(number)?;
    let template = PRESETS["templates"]
        .get(name)
        .context("unknown gameplay template")?;
    let mut cluster = template["cluster"].clone();
    cluster["token"] = Value::String(token.into());
    cluster["settings"]["cluster_name"] = Value::String(format!(
        "LST-{number:03}-{}",
        template["label"].as_str().expect("template label")
    ));
    if let Some(settings) = settings {
        cluster["settings"]
            .as_object_mut()
            .expect("template settings")
            .extend(
                settings
                    .as_value()
                    .as_object()
                    .expect("validated settings")
                    .clone(),
            );
    }
    if let Some(key) = cluster_key {
        cluster["settings"]["cluster_key"] = Value::String(key.into());
    }
    ClusterConfig::from_value(cluster)
}

fn fleet_definition(number: u16) -> Result<&'static Value> {
    PRESETS["fleet"]
        .as_array()
        .expect("fleet definitions")
        .iter()
        .find(|room| room["number"].as_u64() == Some(u64::from(number)))
        .context("LST room number must be in 000-099 or 200-219")
}

pub fn room_numbers() -> Vec<u16> {
    PRESETS["fleet"]
        .as_array()
        .expect("fleet definitions")
        .iter()
        .map(|room| room["number"].as_u64().expect("room number") as u16)
        .collect()
}

pub fn room(number: u16) -> Result<(&'static str, &'static str, u8)> {
    let definition = fleet_definition(number)?;
    let name = definition["template"].as_str().expect("template name");
    Ok((
        name,
        PRESETS["templates"][name]["label"]
            .as_str()
            .expect("template label"),
        definition["max_players"].as_u64().expect("max players") as u8,
    ))
}

pub fn room_name(number: u16) -> Result<&'static str> {
    Ok(fleet_definition(number)?["name"]
        .as_str()
        .expect("room name"))
}

pub fn room_schedule(number: u16) -> Result<Option<(&'static str, u8, u8)>> {
    Ok(fleet_definition(number)?["schedule"]
        .as_array()
        .map(|schedule| {
            (
                schedule[0].as_str().expect("schedule label"),
                schedule[1].as_u64().expect("start hour") as u8,
                schedule[2].as_u64().expect("end hour") as u8,
            )
        }))
}

pub fn build(number: u16, token: &str, cluster_key: Option<&str>) -> Result<ClusterConfig> {
    let settings = ClusterSettings::from_value(json!({"cluster_name": room_name(number)?}))?;
    build_template(room(number)?.0, number, token, cluster_key, Some(&settings))
}

pub fn fleet_room(number: u16, token: &str, cluster_key: Option<&str>) -> Result<Room> {
    let definition = fleet_definition(number)?;
    let mut deployment: DeploymentOptions =
        serde_json::from_value(definition["deployment"].clone())?;
    // The resident Agent evaluates opening hours, including after host reboot.
    deployment.start_on_boot = true;
    let mut policy = json!({});
    if let Some((_, start, end)) = room_schedule(number)? {
        policy["schedule"] =
            json!([{"start": format!("{start:02}:00"), "end": format!("{end:02}:00")}]);
    }
    if definition["recycle"] == true {
        policy["idle_regeneration"] = json!(true);
    }
    let room = Room {
        number,
        template: Some(room(number)?.0.into()),
        cluster: build(number, token, cluster_key)?,
        deployment,
        policy: serde_json::from_value(policy)?,
    };
    room.validate()?;
    Ok(room)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::symlink;

    use super::*;

    fn ordinary() -> Room {
        Room::new(
            299,
            build_template("pure_survival", 299, "test-token", None, None).unwrap(),
        )
        .unwrap()
    }

    #[test]
    fn templates_and_fleet_keep_game_modes_schedules_and_independent_room_slots() {
        let schema = schema();
        let validator = jsonschema::validator_for(&schema).unwrap();
        assert!(validator.is_valid(&ordinary().as_value()));
        let mut old_policy = ordinary().as_value();
        old_policy["schedule"] = json!([]);
        assert!(!validator.is_valid(&old_policy));
        assert_eq!(
            preset_names(),
            [
                "CAVES",
                "ENDLESS",
                "ENDLESS_GENERATION",
                "ENDLESS_SETTINGS",
                "FOREST",
                "FOREST_CAVES",
                "FOREST_ONLY_NIGHT",
                "LAVAARENA",
                "LIGHTS_OUT_GENERATION",
                "LIGHTS_OUT_SETTINGS",
                "QUAGMIRE",
                "SHARDED"
            ]
        );
        for name in preset_names() {
            preset(name).unwrap();
        }
        assert_eq!(
            preset("SHARDED").unwrap().resolved()["settings"]["shard_enabled"],
            true
        );
        assert_eq!(
            preset("FOREST_CAVES").unwrap().resolved()["shards"]
                .as_object()
                .unwrap()
                .len(),
            2
        );
        assert!(preset("unknown").is_err());
        assert_eq!(
            template_names(),
            [
                "pure_survival",
                "pure_endless",
                "semi_survival",
                "semi_endless",
                "afk",
                "lights_out_survival",
                "lights_out_endless",
                "island_adventure",
                "hamlet",
                "adventure",
                "gorge",
                "forge"
            ]
        );
        assert_eq!(room_numbers(), (0..100).chain(200..220).collect::<Vec<_>>());
        for template in template_names() {
            let cluster =
                build_template(template, 299, "test-token", Some("shared-key"), None).unwrap();
            let files = cluster.files().unwrap();
            assert!(files[Path::new("cluster.ini")].contains("LST-299-"));
            assert_eq!(files[Path::new("cluster_token.txt")], "test-token\n");
            assert_eq!(cluster.resolved()["settings"]["cluster_key"], "shared-key");
            let expected = match template {
                "gorge" => "quagmire",
                "forge" => "lavaarena",
                _ => "survival",
            };
            assert_eq!(cluster.resolved()["settings"]["game_mode"], expected);
            if matches!(template, "forge" | "gorge") {
                assert!(files.keys().any(|path| {
                    path.file_name()
                        .is_some_and(|name| name == "leveldataoverride.lua")
                }));
            }
        }
        for number in room_numbers() {
            let fleet = fleet_room(number, "test-token", None).unwrap();
            assert_eq!(fleet.number, number);
            assert_eq!(fleet.policy.idle_regeneration, number < 100);
            assert!(fleet.deployment.start_on_boot);
            assert_eq!(
                fleet.cluster.resolved()["settings"]["cluster_name"],
                room_name(number).unwrap()
            );
        }
        assert_eq!(room_schedule(16).unwrap(), Some(("白饭", 10, 18)));
        assert_eq!(room_schedule(20).unwrap(), Some(("晚宴", 18, 0)));
        assert_eq!(room_schedule(28).unwrap(), Some(("夜饮", 0, 8)));
        assert!(room_name(20).unwrap().contains("18-24"));
        assert!(ordinary().policy.schedule.is_empty());
        assert!(!ordinary().policy.idle_regeneration);
        for number in [100, 199, 220, 299] {
            assert!(fleet_room(number, "", None).is_err());
        }
        assert!(build_template("pure_survival", 300, "", None, None).is_err());
        assert!(build_template("unknown", 0, "", None, None).is_err());
        let settings =
            ClusterSettings::from_value(json!({"cluster_name":"Custom", "max_players":7})).unwrap();
        let customized = build_template("afk", 250, "test-token", None, Some(&settings)).unwrap();
        assert_eq!(customized.resolved()["settings"]["cluster_name"], "Custom");
        assert_eq!(customized.resolved()["settings"]["max_players"], 7);
        assert_eq!(customized.resolved()["settings"]["tick_rate"], 1);
    }

    #[test]
    fn edits_validate_once_preserve_secrets_and_handle_defaults_arrays_and_escapes() {
        let original = ordinary();
        let changed = original
            .edit_many(
                &[
                    ("/cluster/settings/whitelist_slots", json!(12)),
                    ("/cluster/settings/max_players", json!(12)),
                    (
                        "/policy/schedule/-",
                        json!({"start":"22:00", "end":"05:00"}),
                    ),
                    ("/policy/schedule/0/end", json!("06:00")),
                    ("/deployment/image", json!("quay.io/example/dst:beta")),
                ],
                &[],
            )
            .unwrap();
        assert_eq!(
            changed.get("/cluster/settings/whitelist_slots").unwrap(),
            12
        );
        assert_eq!(changed.get("/policy/schedule/0/end").unwrap(), "06:00");
        assert_eq!(changed.cluster.resolved()["token"], "test-token");
        assert_eq!(original.get("/cluster/settings/max_players").unwrap(), 9);
        assert!(original.policy.schedule.is_empty());
        assert!(
            !serde_json::to_string(&changed)
                .unwrap()
                .contains("test-token")
        );
        assert!(
            original
                .edit("/cluster/settings/whitelist_slots", json!(12), false)
                .is_err()
        );
        assert!(
            changed
                .edit("/cluster/settings/max_players", json!(65), false)
                .is_err()
        );
        let reset = changed
            .edit("/policy/schedule/0", Value::Null, true)
            .unwrap()
            .edit("/deployment/image", Value::Null, true)
            .unwrap()
            .edit("/cluster/settings/max_snapshots", Value::Null, true)
            .unwrap();
        assert!(reset.policy.schedule.is_empty());
        assert_eq!(reset.deployment.image, DeploymentOptions::default().image);
        assert_eq!(reset.get("/cluster/settings/max_snapshots").unwrap(), 6);
        assert!(
            reset
                .edit("/cluster/settings/max_snapshots", Value::Null, true)
                .is_err()
        );
        let changed = changed
            .edit(
                "/cluster/shards/forest/mods/entries/custom",
                json!({"configuration_options":{"a/b~c":"old"}}),
                false,
            )
            .unwrap()
            .edit(
                "/cluster/shards/forest/mods/entries/custom/configuration_options/a~1b~0c",
                json!("new"),
                false,
            )
            .unwrap();
        assert_eq!(
            changed
                .get("/cluster/shards/forest/mods/entries/custom/configuration_options/a~1b~0c")
                .unwrap(),
            "new"
        );
        for pointer in [
            "/number",
            "",
            "/cluster/blocklist",
            "/cluster/adminlist/child",
            "/cluster/settings/~2",
            "/policy/schedule/01",
            "/policy/schedule/-1",
            "/policy/schedule/1",
        ] {
            assert!(
                changed.edit(pointer, Value::Null, true).is_err(),
                "{pointer}"
            );
        }
        assert!(
            changed
                .edit("/cluster", json!({"blocklist":"new"}), false)
                .is_err()
        );
        assert!(
            changed
                .edit("/policy/timezone", json!("invalid"), false)
                .is_err()
        );
    }

    #[test]
    fn store_round_trip_keeps_live_lists_saves_control_state_and_existing_keys() {
        let directory = tempfile::tempdir().unwrap();
        let store = RoomStore::new(directory.path().join("rooms"));
        assert!(store.numbers().unwrap().is_empty());
        assert!(!store.root().exists());
        let room = fleet_room(16, "test-token", None).unwrap();
        let written = store.save(&room).unwrap();
        let root = store.path(16).unwrap();
        assert!(written.contains(&root.join("cluster.ini")));
        let previous = store.load(16).unwrap();
        assert_eq!(previous.template, room.template);
        assert_eq!(previous.policy, room.policy);
        assert_eq!(store.numbers().unwrap(), [16]);
        let key = previous.cluster.resolved()["settings"]["cluster_key"].clone();
        assert!(key.as_str().is_some_and(|value| !value.is_empty()));
        fs::write(root.join("blocklist.txt"), b"KU_newlybanned\r\n").unwrap();
        fs::create_dir_all(root.join("cave/save/session")).unwrap();
        fs::write(root.join("cave/save/session/world"), b"world\0\xff").unwrap();
        let mut guard = RoomLock::try_acquire(&root).unwrap();
        guard
            .update_control(|control| {
                control.insert(
                    "policy_state".into(),
                    json!({"activity":{"sessions":{"forest":"current"}}}),
                );
                control.insert("recovery".into(), json!({"attempt":2}));
                Ok(())
            })
            .unwrap();
        assert!(store.save(&room).is_err());
        assert_eq!(store.numbers().unwrap(), [16]);
        drop(guard);
        let previous = store.load(16).unwrap();
        let changed = previous
            .edit("/cluster/settings/max_players", json!(12), false)
            .unwrap();
        assert_eq!(store.save(&changed).unwrap(), [root.join("cluster.ini")]);
        assert_eq!(
            fs::read(root.join("blocklist.txt")).unwrap(),
            b"KU_newlybanned\r\n"
        );
        assert_eq!(
            fs::read(root.join("cave/save/session/world")).unwrap(),
            b"world\0\xff"
        );
        let loaded = store.load(16).unwrap();
        assert!(
            serde_json::to_value(&loaded).unwrap()["cluster"]
                .get("blocklist")
                .is_none()
        );
        assert_eq!(loaded.cluster.resolved()["settings"]["cluster_key"], key);
        let changed = loaded
            .edit_many(
                &[
                    ("/template", json!("custom")),
                    ("/policy/mod_auto_update", json!(false)),
                ],
                &[],
            )
            .unwrap();
        let native = fs::read(root.join("cluster.ini")).unwrap();
        store.save_policy(&changed).unwrap();
        assert_eq!(fs::read(root.join("cluster.ini")).unwrap(), native);
        let guard = RoomLock::try_acquire(&root).unwrap();
        let control = guard.read_control().unwrap();
        assert_eq!(
            control["policy_state"]["activity"]["sessions"]["forest"],
            "current"
        );
        assert_eq!(control["recovery"]["attempt"], 2);
        assert!(!control.contains_key("cluster"));
        assert!(!control.contains_key("deployment"));
        drop(guard);
        fs::remove_file(root.join(files::CONTROL_FILE)).unwrap();
        let loaded = store.load(16).unwrap();
        assert_eq!(loaded.policy, Policy::default());
        assert!(loaded.template.is_none());
        assert!(!root.join(files::CONTROL_FILE).exists());
    }

    #[test]
    fn store_reads_external_changes_and_rejects_unsafe_paths_and_control_before_writing() {
        let directory = tempfile::tempdir().unwrap();
        let store = RoomStore::new(directory.path().join("rooms"));
        store.save(&ordinary()).unwrap();
        let root = store.path(299).unwrap();
        let cluster = root.join("cluster.ini");
        fs::write(
            &cluster,
            fs::read_to_string(&cluster)
                .unwrap()
                .replace("max_players = 9", "max_players = 11"),
        )
        .unwrap();
        assert_eq!(
            store
                .load(299)
                .unwrap()
                .get("/cluster/settings/max_players")
                .unwrap(),
            11
        );
        let before = fs::read(&cluster).unwrap();
        fs::write(root.join(files::CONTROL_FILE), "[]").unwrap();
        assert!(store.save(&ordinary()).is_err());
        assert_eq!(fs::read(&cluster).unwrap(), before);
        assert!(store.path(300).is_err());
        let alias = directory.path().join("alias");
        symlink(store.root(), &alias).unwrap();
        let alias = RoomStore::new(alias);
        assert!(alias.numbers().is_err());
        assert!(alias.load(299).is_err());
        assert!(alias.save(&ordinary()).is_err());
        let quadlets = directory.path().join("quadlets");
        fs::create_dir(&quadlets).unwrap();
        fs::write(quadlets.join("dst-299.container"), "invalid").unwrap();
        fs::remove_file(root.join(files::CONTROL_FILE)).unwrap();
        assert!(store.with_quadlet_dir(quadlets).load(299).is_err());
    }
}
