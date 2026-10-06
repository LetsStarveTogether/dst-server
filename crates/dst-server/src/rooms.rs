//! Numbered native rooms, gameplay templates and validated configuration edits.

use std::{
    collections::BTreeMap,
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::{LazyLock, OnceLock},
};

use anyhow::{Context, Result, bail, ensure};
use chrono::Timelike;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value, json};

use crate::{
    deployment::{ContainerUnit, DeploymentOptions, PortMapping, configured_ports},
    files::{self, PermissionFiles, RoomLock, RoomReader},
    policy::{DailyWindow, Policy},
    settings::{self, ClusterConfig, RoomPreset, types::Resolve},
};

pub const MAX_ROOM_SLOT: u16 = 299;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Presets {
    deployment: DeploymentOptions,
    schedules: Vec<FleetSchedule>,
    parts: BTreeMap<String, RoomPreset>,
    shared: BTreeMap<String, RoomPreset>,
    templates: Vec<Template>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FleetSchedule {
    label: String,
    window: DailyWindow,
    rooms: Vec<[u16; 2]>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Template {
    name: String,
    label: String,
    rooms: Vec<[u16; 2]>,
    #[serde(default)]
    parts: Vec<String>,
    #[serde(rename = "preset")]
    changes: RoomPreset,
    #[serde(skip)]
    composed: OnceLock<RoomPreset>,
}

impl Template {
    fn preset(&self) -> &RoomPreset {
        self.composed.get_or_init(|| {
            let mut parts = vec![PRESETS.shared["common"].clone()];
            for name in &self.parts {
                parts.push(
                    PRESETS
                        .parts
                        .get(name)
                        .or_else(|| PRESETS.shared.get(name))
                        .expect("known template part")
                        .clone(),
                );
            }
            parts.push(self.changes.clone());
            RoomPreset::compose(&parts).expect("valid bundled template")
        })
    }
}

static PRESETS: LazyLock<Presets> = LazyLock::new(|| {
    let mut presets: Presets =
        serde_json::from_str(include_str!("../resources/settings/presets.json"))
            .expect("valid bundled room presets");
    for schedule in &presets.schedules {
        schedule.window.validate().expect("valid fleet window");
        assert!(
            schedule.window.start.minute() == 0 && schedule.window.end.minute() == 0,
            "fleet opening hours must be whole hours"
        );
    }
    for (name, first, second) in [
        ("FOREST_CAVES", "FOREST", "CAVES"),
        ("ENDLESS", "ENDLESS_SETTINGS", "ENDLESS_GENERATION"),
    ] {
        let parts = [presets.parts[first].clone(), presets.parts[second].clone()];
        presets.parts.insert(
            name.into(),
            RoomPreset::compose(&parts).expect("valid bundled preset"),
        );
    }
    presets
});

#[derive(Clone)]
pub struct Room {
    pub number: u16,
    pub template: Option<String>,
    pub cluster: ClusterConfig,
    pub deployment: DeploymentOptions,
    pub policy: Policy,
}

pub(crate) fn without_permissions(mut cluster: Value) -> Value {
    if let Some(fields) = cluster.as_object_mut() {
        for field in settings::PERMISSION_FIELDS {
            fields.remove(field);
        }
    }
    cluster
}

impl Serialize for Room {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        settings::redact(self.as_value()).serialize(serializer)
    }
}

impl std::fmt::Debug for Room {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Room({})", settings::redact(self.as_value()))
    }
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
        ensure!(
            self.template
                .as_ref()
                .is_none_or(|label| label.len() <= 256),
            "room template label exceeds 256 UTF-8 bytes"
        );
        self.deployment.validate()?;
        self.policy.validate()?;
        Ok(())
    }

    /// The editable representation retains sparse native fields and secret values.
    pub fn as_value(&self) -> Value {
        json!({
            "number": self.number,
            "template": self.template,
            "cluster": without_permissions(self.cluster.as_value()),
            "deployment": self.deployment,
            "policy": self.policy,
        })
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
        self.edited(changes, unset)
    }

    pub(crate) fn edited<S: AsRef<str>, U: AsRef<str>>(
        &self,
        changes: &[(S, Value)],
        unset: &[U],
    ) -> Result<Self> {
        self.validate()?;
        let mut edited = Self::from_value(edit_fields(
            self.as_value(),
            self.resolved(),
            changes,
            unset,
        )?)?;
        edited.cluster.copy_permissions_from(&self.cluster);
        Ok(edited)
    }
}

pub(crate) fn edit_fields<S: AsRef<str>, U: AsRef<str>>(
    mut value: Value,
    mut defaults: Value,
    changes: &[(S, Value)],
    unset: &[U],
) -> Result<Value> {
    if let Some(cluster) = defaults.get_mut("cluster").and_then(Value::as_object_mut) {
        // A field edit can supply part of a previously inherited native file.
        cluster
            .entry("mod_settings")
            .or_insert_with(|| settings::types::ModSettings::default().resolved());
        cluster
            .entry("downloads")
            .or_insert_with(|| settings::types::WorkshopDownloads::default().resolved());
    }
    for (pointer, change) in changes {
        edit_pointer(&mut value, &defaults, pointer.as_ref(), change, false)?;
    }
    for pointer in unset {
        edit_pointer(&mut value, &defaults, pointer.as_ref(), &Value::Null, true)?;
    }
    Ok(value)
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
    let mut fallback = Some(defaults);
    for part in &parts[..parts.len() - 1] {
        fallback = fallback.and_then(|parent| pointer_item(parent, part).ok());
        if let Value::Object(object) = target
            && !object.contains_key(part)
        {
            match fallback {
                Some(Value::Object(_)) => {
                    object.insert(part.clone(), json!({}));
                }
                Some(Value::Array(array)) => {
                    object.insert(part.clone(), Value::Array(array.clone()));
                }
                _ => {}
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

fn saved_template(state: &serde_json::Map<String, Value>) -> Result<Option<String>> {
    Ok(serde_json::from_value(
        state.get("template").cloned().unwrap_or(Value::Null),
    )?)
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
        let root = RoomReader::open(&self.root)?;
        let mut numbers = Vec::new();
        for (name, kind) in root.list_directory("")? {
            if !kind.is_dir() || name.len() != 3 || !name.bytes().all(|byte| byte.is_ascii_digit())
            {
                continue;
            }
            let number: u16 = name.parse()?;
            if number <= MAX_ROOM_SLOT && root.has_file(&Path::new(&name).join("cluster.ini"))? {
                numbers.push(number);
            }
        }
        Ok(numbers)
    }

    pub(crate) fn template(&self, number: u16) -> Result<Option<String>> {
        let state = RoomReader::open(self.path(number)?)?.read_state()?;
        saved_template(&state)
    }

    pub fn load(&self, number: u16) -> Result<Room> {
        let guard = RoomLock::try_acquire(self.path(number)?)?;
        self.load_locked(number, &guard)
    }

    pub(crate) fn load_locked(&self, number: u16, guard: &RoomLock) -> Result<Room> {
        self.load_from_state(number, guard, &guard.read_state()?)
    }

    pub(crate) fn load_from_state(
        &self,
        number: u16,
        guard: &RoomLock,
        control: &serde_json::Map<String, Value>,
    ) -> Result<Room> {
        self.read_room(number, guard, control, true)
    }

    pub(crate) fn load_for_edit(&self, number: u16, guard: &RoomLock) -> Result<Room> {
        self.read_room(number, guard, &guard.read_state()?, false)
    }

    fn read_room(
        &self,
        number: u16,
        guard: &RoomLock,
        control: &serde_json::Map<String, Value>,
        check_ports: bool,
    ) -> Result<Room> {
        let mut room = Room {
            number,
            template: saved_template(control)?,
            cluster: ClusterConfig::load(guard)?,
            deployment: DeploymentOptions::default(),
            policy: serde_json::from_value(
                control.get("policy").cloned().unwrap_or_else(|| json!({})),
            )?,
        };
        if let Some(unit) = self.deployment(number, guard.directory())? {
            if check_ports {
                unit.check_ports(configured_ports(&room.cluster)?)?;
            }
            room.deployment = unit.options;
        }
        room.validate()?;
        Ok(room)
    }

    pub(crate) fn deployment(&self, number: u16, room: &Path) -> Result<Option<ContainerUnit>> {
        let Some(directory) = &self.quadlet_dir else {
            return Ok(None);
        };
        let path = directory.join(format!("dst-{number:03}.container"));
        match fs::symlink_metadata(&path) {
            Ok(_) => Ok(Some(ContainerUnit::load_for_room(path, room)?)),
            Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
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
        if let Some(unit) = self.deployment(room.number, &directory)? {
            ensure!(
                room.deployment == unit.options,
                "use Host.edit to change a managed deployment"
            );
            unit.check_ports(configured_ports(&room.cluster)?)?;
        } else {
            ensure!(
                room.deployment == DeploymentOptions::default(),
                "use Host.create or Host.edit to save deployment options"
            );
        }
        guard.read_state()?;
        let changed = {
            let mut stopped = guard.while_stopped();
            room.cluster.save(&mut stopped, PermissionFiles::Preserve)?
        };
        save_definition_fields(&mut guard, room)?;
        Ok(changed
            .into_iter()
            .map(|path| directory.join(path))
            .collect())
    }

    pub fn save_policy(&self, room: &Room) -> Result<()> {
        let mut guard = RoomLock::try_acquire(self.path(room.number)?)?;
        room.policy.save(&mut guard)
    }
}

pub(crate) fn save_definition_fields(guard: &mut RoomLock, room: &Room) -> Result<()> {
    guard.update_state(|control| {
        control.insert("template".into(), serde_json::to_value(&room.template)?);
        control.insert("policy".into(), serde_json::to_value(&room.policy)?);
        Ok(())
    })
}

pub fn template_names() -> Vec<&'static str> {
    PRESETS
        .templates
        .iter()
        .map(|template| template.name.as_str())
        .collect()
}

pub fn preset_names() -> Vec<&'static str> {
    PRESETS.parts.keys().map(String::as_str).collect()
}

pub fn preset(name: &str) -> Result<RoomPreset> {
    PRESETS
        .parts
        .get(name)
        .cloned()
        .context("unknown room preset")
}

fn cluster_name(number: u16, label: &str) -> String {
    format!("LST-朗诵团-{number:03}-{label}")
}

pub fn build_template(
    name: &str,
    number: u16,
    token: &str,
    cluster_key: Option<&str>,
    settings: Option<Map<String, Value>>,
) -> Result<ClusterConfig> {
    validate_number(number)?;
    let template = PRESETS
        .templates
        .iter()
        .find(|template| template.name == name)
        .context("unknown gameplay template")?;
    let mut values = settings.unwrap_or_default();
    values
        .entry("cluster_name")
        .or_insert_with(|| json!(cluster_name(number, &template.label)));
    template.preset().build(token, cluster_key, Some(values))
}

fn contains_room(ranges: &[[u16; 2]], number: u16) -> bool {
    ranges
        .iter()
        .any(|&[start, end]| (start..=end).contains(&number))
}

fn fleet_template(number: u16) -> Result<&'static Template> {
    PRESETS
        .templates
        .iter()
        .find(|template| contains_room(&template.rooms, number))
        .context("LST room number must be in 000-100 or 200-219")
}

pub fn room_numbers() -> Vec<u16> {
    let mut numbers = PRESETS
        .templates
        .iter()
        .flat_map(|template| template.rooms.iter().flat_map(|&[start, end]| start..=end))
        .collect::<Vec<_>>();
    numbers.sort_unstable();
    numbers
}

pub fn room(number: u16) -> Result<(&'static str, &'static str, u8)> {
    let template = fleet_template(number)?;
    let capacity = template
        .preset()
        .data()
        .settings
        .as_ref()
        .expect("fleet template has shared settings")
        .max_players()
        .try_into()?;
    Ok((&template.name, &template.label, capacity))
}

const BETA_ROOM: u16 = 100;

fn fleet_schedule(number: u16) -> Option<&'static FleetSchedule> {
    PRESETS
        .schedules
        .iter()
        .find(|schedule| contains_room(&schedule.rooms, number))
}

pub fn room_name(number: u16) -> Result<String> {
    let template = fleet_template(number)?;
    let label = if number == BETA_ROOM {
        "测试尝鲜"
    } else {
        &template.label
    };
    let name = cluster_name(number, label);
    let Some(schedule) = fleet_schedule(number) else {
        return Ok(name);
    };
    let start = schedule.window.start.hour();
    let end = schedule.window.end.hour();
    Ok(format!(
        "{name}-{} | 每日 {start}-{} 开放",
        schedule.label,
        if end == 0 { 24 } else { end }
    ))
}

pub fn room_schedule(number: u16) -> Result<Option<(&'static str, u8, u8)>> {
    fleet_template(number)?;
    Ok(fleet_schedule(number).map(|schedule| {
        (
            schedule.label.as_str(),
            schedule.window.start.hour() as u8,
            schedule.window.end.hour() as u8,
        )
    }))
}

pub fn build(number: u16, token: &str, cluster_key: Option<&str>) -> Result<ClusterConfig> {
    let template = fleet_template(number)?;
    let settings = Map::from_iter([("cluster_name".into(), room_name(number)?.into())]);
    template.preset().build(token, cluster_key, Some(settings))
}

pub fn fleet_room(number: u16, token: &str, cluster_key: Option<&str>) -> Result<Room> {
    let cluster = build(number, token, cluster_key)?;
    let mut deployment = PRESETS.deployment.clone();
    if number == BETA_ROOM {
        deployment.image = crate::deployment::BETA_IMAGE.into();
    }
    for shard in cluster.data().shards.values() {
        let settings = &shard.settings;
        let base = 30000 + number * 10 + if settings.is_master() { 0 } else { 2 };
        for (offset, port) in [
            (0, settings.server_port()),
            (1, settings.master_server_port()),
        ] {
            deployment.ports.push(PortMapping {
                host: base + offset,
                container: port.try_into()?,
            });
        }
    }
    let room = Room {
        number,
        template: Some(fleet_template(number)?.name.clone()),
        cluster,
        deployment,
        policy: Policy {
            idle_regeneration: number <= 100,
            schedule: fleet_schedule(number)
                .map(|schedule| schedule.window.clone())
                .into_iter()
                .collect(),
            ..Policy::default()
        },
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
        assert!(Room::from_value(ordinary().as_value()).is_ok());
        let mut old_policy = ordinary().as_value();
        old_policy["schedule"] = json!([]);
        assert!(Room::from_value(old_policy).is_err());
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
        assert_eq!(
            room_numbers(),
            (0..=100).chain(200..220).collect::<Vec<_>>()
        );
        for template in template_names() {
            let cluster =
                build_template(template, 299, "test-token", Some("shared-key"), None).unwrap();
            let files = cluster.files().unwrap();
            assert!(files[Path::new("cluster.ini")].contains("LST-朗诵团-299-"));
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
            assert_eq!(fleet.policy.idle_regeneration, number <= 100);
            assert!(fleet.deployment.start_on_boot);
            assert_eq!(
                fleet.cluster.resolved()["settings"]["cluster_name"],
                room_name(number).unwrap()
            );
            if let Some((label, start, end)) = room_schedule(number).unwrap() {
                assert!(!label.is_empty());
                assert_eq!(fleet.policy.schedule.len(), 1);
                let window = &fleet.policy.schedule[0];
                assert_eq!(window.start.hour(), u32::from(start));
                assert_eq!(window.end.hour(), u32::from(end));
                assert_eq!(window.start.minute(), 0);
                assert_eq!(window.end.minute(), 0);
            } else {
                assert!(fleet.policy.schedule.is_empty());
            }
        }
        assert_eq!(room_schedule(16).unwrap(), Some(("白饭", 10, 18)));
        assert_eq!(room_schedule(20).unwrap(), Some(("晚宴", 18, 0)));
        assert_eq!(room_schedule(28).unwrap(), Some(("夜饮", 0, 8)));
        assert!(room_name(20).unwrap().contains("18-24"));
        assert!(ordinary().policy.schedule.is_empty());
        assert!(!ordinary().policy.idle_regeneration);
        for number in [101, 199, 220, 299] {
            assert!(fleet_room(number, "", None).is_err());
            assert!(room_schedule(number).is_err());
        }
        assert!(build_template("pure_survival", 300, "", None, None).is_err());
        assert!(build_template("unknown", 0, "", None, None).is_err());
        let settings = Map::from_iter([
            ("cluster_name".into(), "Custom".into()),
            ("max_players".into(), 7.into()),
            ("steam_group_only".into(), true.into()),
        ]);
        let customized = build_template("afk", 250, "test-token", None, Some(settings)).unwrap();
        assert_eq!(customized.resolved()["settings"]["cluster_name"], "Custom");
        assert_eq!(customized.resolved()["settings"]["max_players"], 7);
        assert_eq!(customized.resolved()["settings"]["tick_rate"], 1);
    }

    #[test]
    fn edits_validate_once_preserve_secrets_and_handle_defaults_arrays_and_escapes() {
        let mut original = ordinary();
        original.deployment.environment.insert(
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS".into(),
            "authorization=fixture-credential".into(),
        );
        let changed = original
            .edit_many(
                &[
                    ("/cluster/settings/whitelist_slots", json!(12)),
                    ("/cluster/settings/max_players", json!(12)),
                    ("/cluster/mod_settings/force_enabled/-", json!("local")),
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
        assert_eq!(
            changed
                .get("/cluster/mod_settings/force_enabled/0")
                .unwrap(),
            "local"
        );
        assert!(
            !serde_json::to_string(&changed)
                .unwrap()
                .contains("fixture-credential")
        );
        assert_eq!(
            changed.as_value()["deployment"]["environment"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"],
            "authorization=fixture-credential"
        );
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
        let mut room = fleet_room(16, "test-token", None).unwrap();
        assert!(store.save(&room).is_err());
        room.deployment = DeploymentOptions::default();
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
        let mut state = crate::policy::PolicyState::load(&guard).unwrap();
        state.queue_mod_update(false).unwrap();
        state.save(&mut guard).unwrap();
        crate::recovery::begin_run(&mut guard, "first-run".into()).unwrap();
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
        let new_label = loaded.edit("/template", json!("custom"), false).unwrap();
        store.save(&new_label).unwrap();
        let policy_edit = loaded
            .edit("/policy/mod_auto_update", json!(false), false)
            .unwrap();
        let native = fs::read(root.join("cluster.ini")).unwrap();
        store.save_policy(&policy_edit).unwrap();
        assert_eq!(fs::read(root.join("cluster.ini")).unwrap(), native);
        let guard = RoomLock::try_acquire(&root).unwrap();
        let control = guard.read_state().unwrap();
        assert_eq!(control["template"], "custom");
        assert_eq!(control["policy"]["mod_auto_update"], false);
        assert_eq!(crate::policy::PolicyState::load(&guard).unwrap(), state);
        assert_eq!(control["agent_run"]["attempt_id"], "first-run");
        assert_eq!(control["recovery"]["restarts_used"], 0);
        assert!(!control.contains_key("cluster"));
        assert!(!control.contains_key("deployment"));
        drop(guard);
        fs::remove_file(root.join(files::STATE_FILE)).unwrap();
        let loaded = store.load(16).unwrap();
        assert_eq!(loaded.policy, Policy::default());
        assert!(loaded.template.is_none());
        assert!(!root.join(files::STATE_FILE).exists());
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
        fs::write(root.join(files::STATE_FILE), "[]").unwrap();
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
        fs::remove_file(root.join(files::STATE_FILE)).unwrap();
        assert!(store.with_quadlet_dir(quadlets).load(299).is_err());
    }
}
