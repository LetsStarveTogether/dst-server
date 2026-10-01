//! Python conversion only; validation and native file operations live in the core.

use std::{collections::BTreeMap, path::PathBuf};

use dst_server::{
    annotations,
    files::{self, PermissionFiles, RoomLock},
    lua, rooms, scripts, settings as native,
};
use pyo3::{
    exceptions::{PyOSError, PyValueError},
    prelude::*,
    types::PyDict,
};
use serde_json::{Value, json};

use crate::{from_python, to_python};

fn error(error: anyhow::Error) -> PyErr {
    if let Some(io) = error.downcast_ref::<std::io::Error>() {
        PyOSError::new_err((io.raw_os_error().unwrap_or(0), error.to_string()))
    } else {
        PyValueError::new_err(error.to_string())
    }
}

fn input(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    from_python(value, 0, &mut 0)
}

fn configuration_input(model: &str, source: &Bound<'_, PyAny>) -> PyResult<Value> {
    let mut value = input(source)?;
    if matches!(model, "WorldgenOverride" | "LevelDataOverride")
        && let Ok(fields) = source.cast::<PyDict>()
        && let Some(overrides) = fields.get_item("overrides")?
        && let Ok(overrides) = overrides.extract::<PyRef<'_, Configuration>>()
    {
        let kind = match overrides.model.as_str() {
            "WorldOverrides" => "world",
            "ForestOverrides" => "forest",
            "CaveOverrides" => "cave",
            "QuagmireOverrides" => "quagmire",
            "LavaArenaOverrides" => "lavaarena",
            "CustomWorldOverrides" => "custom",
            "_LoadedForestOverrides" => "loaded_forest",
            "_LoadedCaveOverrides" => "loaded_cave",
            _ => {
                return Err(PyValueError::new_err(
                    "overrides requires a world override model",
                ));
            }
        };
        value["overrides"] = json!({"kind":kind,"values":overrides.value});
    }
    Ok(value)
}

pub(crate) fn configuration_value(value: &Bound<'_, PyAny>) -> Option<Value> {
    value
        .extract::<PyRef<'_, Configuration>>()
        .ok()
        .map(|value| value.value.clone())
}

fn checked(model: &str, value: Value) -> PyResult<Configuration> {
    match model {
        "Policy" => serde_json::from_value::<dst_server::policy::Policy>(value.clone())
            .map_err(|_| PyValueError::new_err("invalid Policy configuration"))?
            .validate()
            .map_err(error)?,
        "DeploymentOptions" => {
            serde_json::from_value::<dst_server::deployment::DeploymentOptions>(value.clone())
                .map_err(|_| PyValueError::new_err("invalid DeploymentOptions configuration"))?
                .validate()
                .map_err(error)?
        }
        "DailyWindow" => serde_json::from_value::<dst_server::policy::DailyWindow>(value.clone())
            .map_err(|_| PyValueError::new_err("invalid DailyWindow configuration"))?
            .validate()
            .map_err(error)?,
        "Room" | "RoomDeployment" => {
            return Err(PyValueError::new_err("use Room or DeploymentOptions"));
        }
        _ => native::validate(model, &value).map_err(error)?,
    }
    Ok(Configuration {
        model: model.into(),
        value,
    })
}

#[derive(Clone)]
#[pyclass(
    name = "Configuration",
    module = "dst_server._native",
    frozen,
    skip_from_py_object
)]
pub(crate) struct Configuration {
    pub(crate) model: String,
    pub(crate) value: Value,
}

impl Configuration {
    pub(crate) fn from_cluster(cluster: native::ClusterConfig) -> Self {
        Self {
            model: "ClusterConfig".into(),
            value: cluster.into_value(),
        }
    }
    fn defaults(&self) -> PyResult<Value> {
        Ok(match self.model.as_str() {
            "Policy" => serde_json::to_value(
                serde_json::from_value::<dst_server::policy::Policy>(self.value.clone())
                    .map_err(|_| PyValueError::new_err("invalid policy"))?,
            )
            .unwrap(),
            "DeploymentOptions" => serde_json::to_value(
                serde_json::from_value::<dst_server::deployment::DeploymentOptions>(
                    self.value.clone(),
                )
                .map_err(|_| PyValueError::new_err("invalid deployment"))?,
            )
            .unwrap(),
            "DailyWindow" => self.value.clone(),
            model => native::resolved(model, &self.value),
        })
    }
}

#[pymethods]
impl Configuration {
    #[new]
    #[pyo3(signature = (model, values=None, **fields))]
    fn new(
        model: &str,
        values: Option<&Bound<'_, PyAny>>,
        fields: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let mut value = values
            .map(|value| configuration_input(model, value))
            .transpose()?
            .unwrap_or_else(|| json!({}));
        let object = value
            .as_object_mut()
            .ok_or_else(|| PyValueError::new_err("configuration must be an object"))?;
        if let Some(fields) = fields {
            object.extend(
                configuration_input(model, fields.as_any())?
                    .as_object()
                    .unwrap()
                    .clone(),
            );
        }
        checked(model, value)
    }

    #[getter]
    fn model(&self) -> &str {
        &self.model
    }

    #[pyo3(signature = (*, defaults=false, secrets=false))]
    fn dump(&self, py: Python<'_>, defaults: bool, secrets: bool) -> PyResult<Py<PyAny>> {
        let value = if defaults {
            self.defaults()?
        } else {
            self.value.clone()
        };
        to_python(
            py,
            if secrets {
                value
            } else {
                native::redact(value)
            },
        )
    }

    fn schema(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        configuration_schema(py, &self.model)
    }

    #[pyo3(signature = (**changes))]
    fn replace(&self, changes: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let mut value = self.value.clone();
        if let Some(changes) = changes {
            value.as_object_mut().unwrap().extend(
                configuration_input(&self.model, changes.as_any())?
                    .as_object()
                    .unwrap()
                    .clone(),
            );
        }
        checked(&self.model, value)
    }

    #[staticmethod]
    #[pyo3(signature = (model, source, *, kind=None))]
    fn parse(model: &str, source: &str, kind: Option<&str>) -> PyResult<Self> {
        let result = match model {
            "ClusterSettings" | "ShardSettings" => native::parse_ini(model, source),
            "WorldgenOverride" => native::WorldgenOverride::parse_with_kind(source, kind)
                .map(native::WorldgenOverride::into_value),
            "LevelDataOverride" => native::LevelDataOverride::parse_with_kind(source, kind)
                .map(native::LevelDataOverride::into_value),
            "ModOverrides" => {
                native::ModOverrides::parse(source).map(native::ModOverrides::into_value)
            }
            "ModSettings" => {
                native::ModSettings::parse(source).map(native::ModSettings::into_value)
            }
            "WorkshopDownloads" => {
                native::WorkshopDownloads::parse(source).map(native::WorkshopDownloads::into_value)
            }
            _ => {
                return Err(PyValueError::new_err(
                    "this model has no standalone native file",
                ));
            }
        };
        checked(model, result.map_err(error)?)
    }

    #[staticmethod]
    #[pyo3(signature = (directory, *, world_kinds=None, level_kinds=None))]
    fn load(
        py: Python<'_>,
        directory: PathBuf,
        world_kinds: Option<BTreeMap<String, String>>,
        level_kinds: Option<BTreeMap<String, String>>,
    ) -> PyResult<Self> {
        py.detach(|| {
            let room = RoomLock::try_acquire(directory)?;
            native::ClusterConfig::load_with_kinds(
                &room,
                &world_kinds.unwrap_or_default(),
                &level_kinds.unwrap_or_default(),
            )
        })
        .map(Self::from_cluster)
        .map_err(error)
    }

    #[pyo3(signature = (*, multi_shard=false))]
    fn render(&self, multi_shard: bool) -> PyResult<String> {
        match self.model.as_str() {
            "ClusterSettings" | "ShardSettings" => {
                native::render_ini(&self.model, &self.value, multi_shard)
            }
            "WorldgenOverride" => native::WorldgenOverride::from_value(self.value.clone())
                .and_then(|value| value.render()),
            "LevelDataOverride" => native::LevelDataOverride::from_value(self.value.clone())
                .and_then(|value| value.render()),
            "ModOverrides" => native::ModOverrides::from_value(self.value.clone())
                .and_then(|value| value.render()),
            "ModSettings" => {
                native::ModSettings::from_value(self.value.clone()).and_then(|value| value.render())
            }
            "WorkshopDownloads" => native::WorkshopDownloads::from_value(self.value.clone())
                .and_then(|value| value.render()),
            _ => {
                return Err(PyValueError::new_err(
                    "use files() for complete cluster configuration",
                ));
            }
        }
        .map_err(error)
    }

    fn files(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.model != "ClusterConfig" {
            return Err(PyValueError::new_err("files requires ClusterConfig"));
        }
        let files = native::ClusterConfig::from_value(self.value.clone())
            .and_then(|cluster| cluster.files())
            .map_err(error)?;
        to_python(
            py,
            serde_json::to_value(files)
                .map_err(|_| PyValueError::new_err("configuration path is not UTF-8"))?,
        )
    }

    #[pyo3(signature = (directory, *, replace_permissions=false))]
    fn save(
        &self,
        py: Python<'_>,
        directory: PathBuf,
        replace_permissions: bool,
    ) -> PyResult<Vec<PathBuf>> {
        if self.model != "ClusterConfig" {
            return Err(PyValueError::new_err(
                "save requires complete ClusterConfig",
            ));
        }
        let cluster = native::ClusterConfig::from_value(self.value.clone()).map_err(error)?;
        py.detach(|| {
            files::create_directory(&directory)?;
            let mut room = RoomLock::try_acquire(&directory)?;
            let mut stopped = room.while_stopped();
            stopped.recover()?;
            let changed = cluster.save(
                &mut stopped,
                if replace_permissions {
                    PermissionFiles::ReplaceOffline
                } else {
                    PermissionFiles::Preserve
                },
            )?;
            Ok::<_, anyhow::Error>(
                changed
                    .into_iter()
                    .map(|path| directory.join(path))
                    .collect(),
            )
        })
        .map_err(error)
    }

    #[staticmethod]
    fn compose(py: Python<'_>, parts: Vec<Py<Configuration>>) -> PyResult<Self> {
        let parts = parts
            .iter()
            .map(|part| {
                let part = part.borrow(py);
                if part.model != "RoomPreset" {
                    return Err(PyValueError::new_err("compose requires RoomPreset values"));
                }
                native::RoomPreset::from_value(part.value.clone()).map_err(error)
            })
            .collect::<PyResult<Vec<_>>>()?;
        native::RoomPreset::compose(&parts)
            .map(|value| Self {
                model: "RoomPreset".into(),
                value: value.into_value(),
            })
            .map_err(error)
    }

    #[pyo3(signature = (*, token="", cluster_key=None, settings=None))]
    fn build(
        &self,
        token: &str,
        cluster_key: Option<&str>,
        settings: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        if self.model != "RoomPreset" {
            return Err(PyValueError::new_err("build requires a RoomPreset"));
        }
        let settings = settings
            .map(input)
            .transpose()?
            .map(native::ClusterSettings::from_value)
            .transpose()
            .map_err(error)?;
        native::RoomPreset::from_value(self.value.clone())
            .and_then(|preset| preset.build(token, cluster_key, settings.as_ref()))
            .map(Self::from_cluster)
            .map_err(error)
    }

    fn __repr__(&self) -> String {
        format!("{}({})", self.model, native::redact(self.value.clone()))
    }
}

#[derive(Clone)]
#[pyclass(
    name = "Room",
    module = "dst_server._native",
    frozen,
    skip_from_py_object
)]
pub(crate) struct Room {
    pub(crate) inner: rooms::Room,
}

#[pymethods]
impl Room {
    #[new]
    #[pyo3(signature = (value=None, **fields))]
    fn new(value: Option<&Bound<'_, PyAny>>, fields: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let mut value = value.map(input).transpose()?.unwrap_or_else(|| json!({}));
        if let Some(fields) = fields {
            value
                .as_object_mut()
                .ok_or_else(|| PyValueError::new_err("room must be an object"))?
                .extend(input(fields.as_any())?.as_object().unwrap().clone());
        }
        rooms::Room::from_value(value)
            .map(|inner| Self { inner })
            .map_err(error)
    }
    #[getter]
    fn number(&self) -> u16 {
        self.inner.number
    }
    #[getter]
    fn cluster(&self) -> Configuration {
        Configuration::from_cluster(self.inner.cluster.clone())
    }
    #[pyo3(signature = (*, defaults=false, secrets=false))]
    fn dump(&self, py: Python<'_>, defaults: bool, secrets: bool) -> PyResult<Py<PyAny>> {
        let value = if defaults {
            self.inner.resolved()
        } else {
            self.inner.as_value()
        };
        to_python(
            py,
            if secrets {
                value
            } else {
                native::redact(value)
            },
        )
    }
    #[staticmethod]
    fn schema(py: Python<'_>) -> PyResult<Py<PyAny>> {
        to_python(py, rooms::schema())
    }
    #[pyo3(signature = (pointer=""))]
    fn get(&self, py: Python<'_>, pointer: &str) -> PyResult<Py<PyAny>> {
        to_python(py, self.inner.get(pointer).map_err(error)?)
    }
    #[pyo3(signature = (pointer, value=None, *, unset=false))]
    fn edit(&self, pointer: &str, value: Option<&Bound<'_, PyAny>>, unset: bool) -> PyResult<Self> {
        self.inner
            .edit(
                pointer,
                value.map(input).transpose()?.unwrap_or(Value::Null),
                unset,
            )
            .map(|inner| Self { inner })
            .map_err(error)
    }
    #[pyo3(signature = (changes=None, *, unset=None))]
    fn edit_many(
        &self,
        changes: Option<&Bound<'_, PyAny>>,
        unset: Option<Vec<String>>,
    ) -> PyResult<Self> {
        let changes = changes.map(input).transpose()?.unwrap_or_else(|| json!([]));
        let changes = changes
            .as_array()
            .ok_or_else(|| PyValueError::new_err("changes must contain (pointer,value) pairs"))?
            .iter()
            .map(|pair| {
                let pair = pair
                    .as_array()
                    .filter(|pair| pair.len() == 2)
                    .ok_or_else(|| {
                        PyValueError::new_err("changes must contain (pointer,value) pairs")
                    })?;
                Ok((
                    pair[0]
                        .as_str()
                        .ok_or_else(|| PyValueError::new_err("JSON Pointer must be text"))?,
                    pair[1].clone(),
                ))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let unset = unset.unwrap_or_default();
        self.inner
            .edit_many(
                &changes,
                &unset.iter().map(String::as_str).collect::<Vec<_>>(),
            )
            .map(|inner| Self { inner })
            .map_err(error)
    }
    fn __repr__(&self) -> String {
        format!("Room({})", native::redact(self.inner.as_value()))
    }
}

#[derive(Clone)]
#[pyclass(
    name = "RoomStore",
    module = "dst_server._native",
    frozen,
    skip_from_py_object
)]
pub(crate) struct RoomStore {
    pub(crate) inner: rooms::RoomStore,
}

fn number(value: &Bound<'_, PyAny>) -> PyResult<u16> {
    input(value)?
        .as_u64()
        .and_then(|value| value.try_into().ok())
        .ok_or_else(|| PyValueError::new_err("room number must be an unsigned integer"))
}

#[pymethods]
impl RoomStore {
    #[new]
    #[pyo3(signature = (root, *, quadlet_dir=None))]
    fn new(root: PathBuf, quadlet_dir: Option<PathBuf>) -> Self {
        let inner = rooms::RoomStore::new(root);
        Self {
            inner: if let Some(directory) = quadlet_dir {
                inner.with_quadlet_dir(directory)
            } else {
                inner
            },
        }
    }
    #[getter]
    fn root(&self) -> PathBuf {
        self.inner.root().to_owned()
    }
    fn path(&self, room_number: &Bound<'_, PyAny>) -> PyResult<PathBuf> {
        self.inner.path(number(room_number)?).map_err(error)
    }
    fn numbers(&self, py: Python<'_>) -> PyResult<Vec<u16>> {
        py.detach(|| self.inner.numbers()).map_err(error)
    }
    fn load(&self, py: Python<'_>, room_number: &Bound<'_, PyAny>) -> PyResult<Room> {
        let number = number(room_number)?;
        py.detach(|| self.inner.load(number))
            .map(|inner| Room { inner })
            .map_err(error)
    }
    fn list(&self, py: Python<'_>) -> PyResult<Vec<Room>> {
        py.detach(|| self.inner.list())
            .map(|rooms| rooms.into_iter().map(|inner| Room { inner }).collect())
            .map_err(error)
    }
    fn save(&self, py: Python<'_>, room: &Room) -> PyResult<Vec<PathBuf>> {
        py.detach(|| self.inner.save(&room.inner)).map_err(error)
    }
    fn save_policy(&self, py: Python<'_>, room: &Room) -> PyResult<()> {
        py.detach(|| self.inner.save_policy(&room.inner))
            .map_err(error)
    }
}

#[pyfunction]
fn configuration_schema(py: Python<'_>, model: &str) -> PyResult<Py<PyAny>> {
    let room = rooms::schema();
    let schema = match model {
        "Room" => room,
        "Policy" => room["properties"]["policy"].clone(),
        "DailyWindow" => room["properties"]["policy"]["properties"]["schedule"]["items"].clone(),
        "DeploymentOptions" => {
            let mut schema = room["$defs"]["RoomDeployment"].clone();
            schema["$defs"] = room["$defs"].clone();
            schema
        }
        _ => native::schema(model).map_err(error)?.clone(),
    };
    to_python(py, schema)
}

#[pyfunction]
fn template_names() -> Vec<&'static str> {
    rooms::template_names()
}
#[pyfunction]
fn preset_names() -> Vec<&'static str> {
    rooms::preset_names()
}
#[pyfunction]
fn preset(name: &str) -> PyResult<Configuration> {
    rooms::preset(name)
        .map(|value| Configuration {
            model: "RoomPreset".into(),
            value: value.into_value(),
        })
        .map_err(error)
}
#[pyfunction]
fn room_numbers() -> Vec<u16> {
    rooms::room_numbers()
}
#[pyfunction]
#[pyo3(signature = (name, *, number=None, token="", cluster_key=None, settings=None))]
fn build_template(
    name: &str,
    number: Option<&Bound<'_, PyAny>>,
    token: &str,
    cluster_key: Option<&str>,
    settings: Option<&Bound<'_, PyAny>>,
) -> PyResult<Configuration> {
    let number = number.map(self::number).transpose()?.unwrap_or(0);
    let settings = settings
        .map(input)
        .transpose()?
        .map(native::ClusterSettings::from_value)
        .transpose()
        .map_err(error)?;
    rooms::build_template(name, number, token, cluster_key, settings.as_ref())
        .map(Configuration::from_cluster)
        .map_err(error)
}
#[pyfunction]
#[pyo3(signature = (room_number, *, token="", cluster_key=None))]
fn fleet_room(
    room_number: &Bound<'_, PyAny>,
    token: &str,
    cluster_key: Option<&str>,
) -> PyResult<Room> {
    rooms::fleet_room(number(room_number)?, token, cluster_key)
        .map(|inner| Room { inner })
        .map_err(error)
}
#[pyfunction]
fn room_name(room_number: &Bound<'_, PyAny>) -> PyResult<&'static str> {
    rooms::room_name(number(room_number)?).map_err(error)
}
#[pyfunction]
fn room_schedule(room_number: &Bound<'_, PyAny>) -> PyResult<Option<(&'static str, u8, u8)>> {
    rooms::room_schedule(number(room_number)?).map_err(error)
}

#[pyfunction]
fn build_bundle(py: Python<'_>, source: PathBuf, output: PathBuf) -> PyResult<Py<PyAny>> {
    let bundle = py
        .detach(|| scripts::build_bundle(source, output))
        .map_err(error)?;
    to_python(
        py,
        serde_json::to_value(bundle)
            .map_err(|_| PyValueError::new_err("bundle result is not JSON"))?,
    )
}
#[pyfunction]
#[pyo3(signature = (path, *, source=None))]
fn verify_bundle(py: Python<'_>, path: PathBuf, source: Option<PathBuf>) -> PyResult<Py<PyAny>> {
    let bundle = py
        .detach(|| scripts::verify_bundle(path, source.as_deref()))
        .map_err(error)?;
    to_python(
        py,
        serde_json::to_value(bundle)
            .map_err(|_| PyValueError::new_err("bundle result is not JSON"))?,
    )
}
#[pyfunction]
fn generate_components(py: Python<'_>, directory: PathBuf) -> PyResult<String> {
    py.detach(|| annotations::generate_components(directory))
        .map_err(error)
}
#[pyfunction]
fn generate_modutil(py: Python<'_>, path: PathBuf) -> PyResult<String> {
    py.detach(|| annotations::generate_modutil(path))
        .map_err(error)
}
#[pyfunction]
fn parse_component(
    source: &str,
    filename: &str,
    class_name: &str,
    folder_name: &str,
) -> PyResult<(Vec<String>, Vec<String>)> {
    annotations::parse_component(source, filename, class_name, folder_name).map_err(error)
}
#[pyfunction]
fn parse_modutil(source: &str, filename: &str) -> PyResult<Vec<String>> {
    annotations::parse_modutil(source, filename).map_err(error)
}
#[pyfunction]
fn parse_lua_literal(py: Python<'_>, source: &str) -> PyResult<Py<PyAny>> {
    to_python(py, lua::parse_literal(source).map_err(error)?)
}
#[pyfunction]
fn parse_lua_return_table(py: Python<'_>, source: &str) -> PyResult<Py<PyAny>> {
    to_python(py, lua::parse_return_table(source).map_err(error)?)
}
#[pyfunction]
fn render_lua_literal(value: &Bound<'_, PyAny>) -> PyResult<String> {
    lua::render_literal(&input(value)?).map_err(error)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Configuration>()?;
    module.add_class::<Room>()?;
    module.add_class::<RoomStore>()?;
    module.add_function(wrap_pyfunction!(configuration_schema, module)?)?;
    module.add_function(wrap_pyfunction!(template_names, module)?)?;
    module.add_function(wrap_pyfunction!(preset_names, module)?)?;
    module.add_function(wrap_pyfunction!(preset, module)?)?;
    module.add_function(wrap_pyfunction!(room_numbers, module)?)?;
    module.add_function(wrap_pyfunction!(build_template, module)?)?;
    module.add_function(wrap_pyfunction!(fleet_room, module)?)?;
    module.add_function(wrap_pyfunction!(room_name, module)?)?;
    module.add_function(wrap_pyfunction!(room_schedule, module)?)?;
    module.add_function(wrap_pyfunction!(build_bundle, module)?)?;
    module.add_function(wrap_pyfunction!(verify_bundle, module)?)?;
    module.add_function(wrap_pyfunction!(generate_components, module)?)?;
    module.add_function(wrap_pyfunction!(generate_modutil, module)?)?;
    module.add_function(wrap_pyfunction!(parse_component, module)?)?;
    module.add_function(wrap_pyfunction!(parse_modutil, module)?)?;
    module.add_function(wrap_pyfunction!(parse_lua_literal, module)?)?;
    module.add_function(wrap_pyfunction!(parse_lua_return_table, module)?)?;
    module.add_function(wrap_pyfunction!(render_lua_literal, module)?)?;
    Ok(())
}
