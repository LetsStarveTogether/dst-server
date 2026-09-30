use std::{collections::HashMap, sync::Arc, time::Duration};

use dst_server::external::{self, KleiConfig, Platform, Region, RoomQuery};
use pyo3::{exceptions::PyValueError, prelude::*};
use serde_json::Value;

use crate::{from_python, py_error, to_python};

#[pyclass(name = "KleiClient", module = "dst_server._native")]
struct KleiClient {
    inner: Arc<external::KleiClient>,
}

fn duration(seconds: f64, field: &str) -> PyResult<Duration> {
    Duration::try_from_secs_f64(seconds).map_err(|_| {
        PyValueError::new_err(format!("{field} must be a finite, nonnegative duration"))
    })
}

fn regions(values: Option<Vec<String>>) -> PyResult<Vec<Region>> {
    values.map_or_else(
        || Ok(Region::ALL.to_vec()),
        |values| {
            values
                .iter()
                .map(|value| value.parse().map_err(py_error))
                .collect()
        },
    )
}

fn platforms(values: Option<Vec<String>>) -> PyResult<Vec<Platform>> {
    values.map_or_else(
        || Ok(Platform::ALL.to_vec()),
        |values| {
            values
                .iter()
                .map(|value| value.parse().map_err(py_error))
                .collect()
        },
    )
}

fn serialized(value: serde_json::Result<Value>) -> PyResult<Py<PyAny>> {
    let value = value.map_err(|_| PyValueError::new_err("Klei response could not be converted"))?;
    Python::attach(|py| to_python(py, value))
}

#[pymethods]
impl KleiClient {
    #[new]
    #[pyo3(signature = (access_token=None, *, endpoints=None, request_timeout=30.0, connect_timeout=10.0, max_response_bytes=33_554_432, lobby_concurrency=8, room_concurrency=24))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        access_token: Option<String>,
        endpoints: Option<HashMap<String, String>>,
        request_timeout: f64,
        connect_timeout: f64,
        max_response_bytes: usize,
        lobby_concurrency: usize,
        room_concurrency: usize,
    ) -> PyResult<Self> {
        let mut config = KleiConfig {
            access_token,
            request_timeout: duration(request_timeout, "request_timeout")?,
            connect_timeout: duration(connect_timeout, "connect_timeout")?,
            max_response_bytes,
            lobby_concurrency,
            room_concurrency,
            ..KleiConfig::default()
        };
        for (key, value) in endpoints.unwrap_or_default() {
            let field = match key.as_str() {
                "builds" => &mut config.endpoints.builds,
                "versions" => &mut config.endpoints.versions,
                "regions" => &mut config.endpoints.regions,
                "lobby" => &mut config.endpoints.lobby,
                "room" => &mut config.endpoints.room,
                _ => return Err(PyValueError::new_err("unknown Klei endpoint name")),
            };
            *field = value;
        }
        Ok(Self {
            inner: Arc::new(external::KleiClient::new(config).map_err(py_error)?),
        })
    }

    #[pyo3(signature = (version_type="release"))]
    fn get_latest_build<'py>(
        &self,
        py: Python<'py>,
        version_type: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let version_type = version_type.to_owned();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .get_latest_build(&version_type)
                .await
                .map_err(py_error)
        })
    }

    fn get_versions<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            serialized(serde_json::to_value(
                client.get_versions().await.map_err(py_error)?,
            ))
        })
    }

    fn get_regions<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client.get_regions().await.map_err(py_error)
        })
    }

    #[pyo3(signature = (region, platform="Steam"))]
    fn lobby<'py>(
        &self,
        py: Python<'py>,
        region: &str,
        platform: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let region = region.parse().map_err(py_error)?;
        let platform = platform.parse().map_err(py_error)?;
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            serialized(serde_json::to_value(
                client.lobby(region, platform).await.map_err(py_error)?,
            ))
        })
    }

    fn room<'py>(
        &self,
        py: Python<'py>,
        row_id: String,
        region: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let region = region.parse().map_err(py_error)?;
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            serialized(serde_json::to_value(
                client.room(&row_id, region).await.map_err(py_error)?,
            ))
        })
    }

    #[pyo3(signature = (regions=None, platforms=None))]
    fn get_lobbies<'py>(
        &self,
        py: Python<'py>,
        regions: Option<Vec<String>>,
        platforms: Option<Vec<String>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let regions = self::regions(regions)?;
        let platforms = self::platforms(platforms)?;
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            serialized(serde_json::to_value(
                client.get_lobbies(&regions, &platforms).await,
            ))
        })
    }

    fn get_rooms<'py>(
        &self,
        py: Python<'py>,
        rooms: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let value = from_python(rooms, 0, &mut 0)?;
        let rooms: Vec<RoomQuery> = serde_json::from_value(value).map_err(|_| {
            PyValueError::new_err("rooms must contain row_id and supported region fields")
        })?;
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            serialized(serde_json::to_value(
                client.get_rooms(rooms).await.map_err(py_error)?,
            ))
        })
    }

    fn discover_rooms<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            serialized(serde_json::to_value(
                client.discover_rooms().await.map_err(py_error)?,
            ))
        })
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<KleiClient>()
}
