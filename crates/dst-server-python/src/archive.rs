//! Native archive files with Python BinaryIO access and awaited upload cleanup.

use std::{io::Write, os::fd::AsRawFd, path::PathBuf, sync::Mutex};

use dst_server::archive as native;
use pyo3::{
    exceptions::{PyRuntimeError, PyTypeError, PyValueError},
    prelude::*,
    types::PyBytes,
};

use crate::{
    from_python,
    host::{Host, error, room_number},
    to_python,
};

#[pyclass(name = "ClusterArchive", module = "dst_server._native")]
pub(crate) struct ClusterArchive {
    filename: String,
    stream: Py<PyAny>,
    inner: Mutex<Option<native::ClusterArchive>>,
}

impl ClusterArchive {
    fn from_native(py: Python<'_>, mut archive: native::ClusterArchive) -> PyResult<Self> {
        // Opening the owned descriptor through procfs gives Python an independent
        // descriptor/seek position with ordinary file-object lifetime semantics.
        let stream = py
            .import("builtins")?
            .getattr("open")?
            .call1((
                format!("/proc/self/fd/{}", archive.file_mut().as_raw_fd()),
                "rb",
            ))?
            .unbind();
        Ok(Self {
            filename: archive.filename.clone(),
            stream,
            inner: Mutex::new(Some(archive)),
        })
    }

    fn copy(&self) -> PyResult<native::ClusterArchive> {
        self.inner
            .lock()
            .unwrap()
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("archive is closed"))?
            .try_clone()
            .map_err(error)
    }
}

#[pymethods]
impl ClusterArchive {
    #[new]
    fn new(py: Python<'_>, filename: String, stream: &Bound<'_, PyAny>) -> PyResult<Self> {
        let mut file = tempfile::tempfile()
            .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))?;
        stream.call_method1("seek", (0,))?;
        loop {
            let value = stream.call_method1("read", (8 * 1024 * 1024,))?;
            let bytes = value
                .cast::<PyBytes>()
                .map_err(|_| PyTypeError::new_err("archive stream read() must return bytes"))?;
            if bytes.as_bytes().is_empty() {
                break;
            }
            if bytes.as_bytes().len() > 8 * 1024 * 1024 {
                return Err(PyValueError::new_err(
                    "archive stream read() exceeded the requested chunk size",
                ));
            }
            let bytes = bytes.as_bytes();
            py.detach(|| file.write_all(bytes))
                .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))?;
        }
        Self::from_native(
            py,
            native::ClusterArchive::from_file(filename, file).map_err(error)?,
        )
    }

    #[getter]
    fn filename(&self) -> &str {
        &self.filename
    }

    #[getter]
    fn stream(&self, py: Python<'_>) -> Py<PyAny> {
        self.stream.clone_ref(py)
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.lock().unwrap().take();
        self.stream.bind(py).call_method0("close")?;
        Ok(())
    }

    fn save<'py>(&self, py: Python<'py>, path: PathBuf) -> PyResult<Bound<'py, PyAny>> {
        let archive = self.copy()?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            tokio::task::spawn_blocking(move || archive.save(path))
                .await
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?
                .map_err(error)
        })
    }

    #[pyo3(signature = (*, bucket=None, endpoint=None, region=None, access_key_id=None, secret_access_key=None, session_token=None, object_prefix="", url_prefix=None))]
    #[allow(clippy::too_many_arguments)]
    fn start_upload(
        &self,
        py: Python<'_>,
        bucket: Option<String>,
        endpoint: Option<String>,
        region: Option<String>,
        access_key_id: Option<String>,
        secret_access_key: Option<String>,
        session_token: Option<String>,
        object_prefix: &str,
        url_prefix: Option<String>,
    ) -> PyResult<Py<Upload>> {
        let archive = self.copy()?;
        let options = native::S3Options {
            bucket,
            endpoint,
            region,
            access_key_id,
            secret_access_key,
            session_token,
        };
        // Acquire the cancellation handle before Python can suspend. Once an
        // upload starts, every cancellation path can await multipart cleanup.
        let _runtime = pyo3_async_runtimes::tokio::get_runtime().enter();
        let store = options.build().map_err(error)?;
        let task = archive
            .start_upload(store, object_prefix, url_prefix.as_deref())
            .map_err(error)?;
        let (cancel, mut cancelled) = tokio::sync::watch::channel(false);
        let (complete, completion) = tokio::sync::watch::channel(None);
        tokio::spawn(async move {
            let outcome = task
                .wait_until_cancelled(async move {
                    if !*cancelled.borrow_and_update() {
                        let _ = cancelled.changed().await;
                    }
                })
                .await
                .map_err(|error| format!("{error:#}"));
            complete.send_replace(Some(outcome));
        });
        Py::new(py, Upload { cancel, completion })
    }
}

#[pyclass(name = "ArchiveUpload", module = "dst_server._native")]
struct Upload {
    cancel: tokio::sync::watch::Sender<bool>,
    completion:
        tokio::sync::watch::Receiver<Option<std::result::Result<native::UploadResult, String>>>,
}

impl Upload {
    async fn result(
        mut completion: tokio::sync::watch::Receiver<
            Option<std::result::Result<native::UploadResult, String>>,
        >,
    ) -> std::result::Result<native::UploadResult, String> {
        loop {
            if let Some(outcome) = completion.borrow_and_update().clone() {
                return outcome;
            }
            completion
                .changed()
                .await
                .map_err(|_| "archive upload worker ended before confirmation".to_owned())?;
        }
    }
}

#[pymethods]
impl Upload {
    fn wait<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let completion = self.completion.clone();
        let keep_open = self.cancel.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let _keep_open = keep_open;
            let result = Self::result(completion)
                .await
                .map_err(PyValueError::new_err)?;
            Python::attach(|py| to_python(py, serde_json::to_value(result).unwrap()))
        })
    }

    fn cancel<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.cancel.send_replace(true);
        let completion = self.completion.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            match Self::result(completion).await {
                Ok(_) => Ok(()),
                Err(error) if error == "archive upload cancelled" => Ok(()),
                Err(error) => Err(PyValueError::new_err(error)),
            }
        })
    }
}

#[pyfunction]
#[pyo3(signature = (host, number, *, options=None, compression_level=3))]
fn export_archive<'py>(
    py: Python<'py>,
    host: PyRef<'_, Host>,
    number: &Bound<'_, PyAny>,
    options: Option<&Bound<'_, PyAny>>,
    #[pyo3(from_py_with = level)] compression_level: u32,
) -> PyResult<Bound<'py, PyAny>> {
    let number = room_number(number)?;
    let options = options
        .map(|value| from_python(value, 0, &mut 0))
        .transpose()?
        .unwrap_or_else(|| serde_json::json!({}));
    let options = serde_json::from_value::<native::ExportOptions>(options)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let host = host.inner.clone();
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let archive = native::export_host(&host, number, options, compression_level)
            .await
            .map_err(error)?;
        Python::attach(|py| Py::new(py, ClusterArchive::from_native(py, archive)?))
    })
}

fn level(value: &Bound<'_, PyAny>) -> PyResult<u32> {
    from_python(value, 0, &mut 0)?
        .as_u64()
        .filter(|value| (1..=22).contains(value))
        .map(|value| value as u32)
        .ok_or_else(|| {
            PyValueError::new_err("compression_level must be an integer from 1 through 22")
        })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ClusterArchive>()?;
    module.add_class::<Upload>()?;
    module.add_function(wrap_pyfunction!(export_archive, module)?)?;
    Ok(())
}
