//! Async Python access to the shared native host API.

use std::{path::PathBuf, sync::Arc};

use dst_server::{
    host as native,
    host_operations::{self, HostOperation},
    rooms,
};
use pyo3::{
    exceptions::{PyOSError, PyTimeoutError, PyValueError},
    prelude::*,
};
use serde_json::Value;

use crate::{
    Client, from_python, logs, py_error,
    settings::{Room, RoomStore},
    to_python,
};

pub(crate) fn error(error: anyhow::Error) -> PyErr {
    if let Some(error) = error.downcast_ref::<dst_server::model::Error>() {
        py_error(error.clone())
    } else if error
        .downcast_ref::<tokio::time::error::Elapsed>()
        .is_some()
    {
        PyTimeoutError::new_err(error.to_string())
    } else if let Some(io) = error.downcast_ref::<std::io::Error>() {
        PyOSError::new_err((io.raw_os_error().unwrap_or(0), error.to_string()))
    } else {
        PyValueError::new_err(error.to_string())
    }
}

fn operation(value: &Bound<'_, PyAny>) -> PyResult<HostOperation> {
    let operation: HostOperation = serde_json::from_value(from_python(value, 0, &mut 0)?)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    operation.validate().map_err(error)?;
    Ok(operation)
}

pub(crate) fn room_number(value: &Bound<'_, PyAny>) -> PyResult<u16> {
    from_python(value, 0, &mut 0)?
        .as_u64()
        .filter(|number| *number <= u64::from(rooms::MAX_ROOM_SLOT))
        .map(|number| number as u16)
        .ok_or_else(|| PyValueError::new_err("room number must be an integer from 0 through 299"))
}

fn room_numbers(values: Vec<Bound<'_, PyAny>>) -> PyResult<Vec<u16>> {
    values.iter().map(room_number).collect()
}

#[pyclass(name = "Host", module = "dst_server._native")]
pub(crate) struct Host {
    pub(crate) inner: Arc<native::Host>,
}

#[pymethods]
impl Host {
    #[new]
    #[pyo3(signature = (root, quadlet_dir, *, systemctl=None, journalctl=None, user=false, command_timeout=30.0, port_start=30000, port_end=65535))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        root: PathBuf,
        quadlet_dir: PathBuf,
        systemctl: Option<PathBuf>,
        journalctl: Option<PathBuf>,
        user: bool,
        command_timeout: f64,
        port_start: u16,
        port_end: u16,
    ) -> PyResult<Self> {
        if port_start == 0 || port_start > port_end {
            return Err(PyValueError::new_err(
                "port pool must be a nonempty range from 1 to 65535",
            ));
        }
        let mut host = native::Host::new(root, quadlet_dir);
        host.systemd.user = user;
        host.systemd.command_timeout = host_operations::duration(command_timeout).map_err(error)?;
        if let Some(executable) = systemctl {
            host.systemd.executable = executable;
        }
        if let Some(executable) = journalctl {
            host.journals.executable = executable;
        }
        host.port_pool = port_start..=port_end;
        Ok(Self {
            inner: Arc::new(host),
        })
    }

    #[getter]
    fn rooms(&self) -> RoomStore {
        RoomStore {
            inner: self.inner.rooms.clone(),
        }
    }

    fn units(&self, number: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
        self.inner.units(room_number(number)?).map_err(error)
    }

    fn load<'py>(&self, py: Python<'py>, number: &Bound<'_, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let number = room_number(number)?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = host.load(number).await.map_err(error)?;
            Python::attach(|py| Py::new(py, Room { inner }))
        })
    }

    fn create<'py>(
        &self,
        py: Python<'py>,
        definition: PyRef<'_, Room>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let host = self.inner.clone();
        let definition = definition.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = host.create(&definition).await.map_err(error)?;
            Python::attach(|py| Py::new(py, Room { inner }))
        })
    }

    fn edit<'py>(
        &self,
        py: Python<'py>,
        definition: PyRef<'_, Room>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let host = self.inner.clone();
        let definition = definition.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = host.edit(&definition).await.map_err(error)?;
            Python::attach(|py| Py::new(py, Room { inner }))
        })
    }

    fn edit_fields<'py>(
        &self,
        py: Python<'py>,
        number: &Bound<'_, PyAny>,
        changes: &Bound<'_, PyAny>,
        unset: Vec<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let number = room_number(number)?;
        let changes: Vec<(String, Value)> =
            serde_json::from_value(from_python(changes, 0, &mut 0)?)
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = host
                .edit_fields(number, &changes, &unset)
                .await
                .map_err(error)?;
            Python::attach(|py| Py::new(py, Room { inner }))
        })
    }

    fn operate<'py>(
        &self,
        py: Python<'py>,
        number: &Bound<'_, PyAny>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let number = room_number(number)?;
        let request = operation(request)?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = host.operate(number, &request).await.map_err(error)?;
            Python::attach(|py| to_python(py, result))
        })
    }

    fn batch<'py>(
        &self,
        py: Python<'py>,
        numbers: Vec<Bound<'_, PyAny>>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let numbers = room_numbers(numbers)?;
        let request = operation(request)?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = host.batch(&numbers, &request).await.map_err(error)?;
            Python::attach(|py| to_python(py, Value::Array(result)))
        })
    }

    fn provision<'py>(
        &self,
        py: Python<'py>,
        definitions: Vec<PyRef<'_, Room>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let definitions: Vec<rooms::Room> =
            definitions.iter().map(|room| room.inner.clone()).collect();
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = host.provision(&definitions).await.map_err(error)?;
            Python::attach(|py| to_python(py, Value::Array(result)))
        })
    }

    fn list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = host.list().await.map_err(error)?;
            Python::attach(|py| to_python(py, Value::Array(result)))
        })
    }

    fn connect<'py>(
        &self,
        py: Python<'py>,
        number: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let number = room_number(number)?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = host.connect(number).await.map_err(error)?;
            Python::attach(|py| Py::new(py, Client { inner }))
        })
    }

    fn journal<'py>(
        &self,
        py: Python<'py>,
        numbers: Vec<Bound<'_, PyAny>>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let numbers = room_numbers(numbers)?;
        let request = logs::journal_query(request)?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = host
                .journal(&numbers, &request)
                .await
                .map_err(logs::error)?;
            Python::attach(|py| to_python(py, logs::journal_result(&result)))
        })
    }

    fn follow_journal<'py>(
        &self,
        py: Python<'py>,
        numbers: Vec<Bound<'_, PyAny>>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let numbers = room_numbers(numbers)?;
        let request = logs::journal_query(request)?;
        let host = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let stream = host
                .follow_journal(&numbers, &request)
                .await
                .map_err(logs::error)?;
            Python::attach(|py| logs::wrap_stream(py, stream))
        })
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Host>()
}
