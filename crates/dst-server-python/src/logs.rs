//! Python handles retain native reader ownership across cancelled Python tasks.

use std::{path::PathBuf, sync::Arc, time::Duration};

use dst_server::logs as native;
use pyo3::{
    exceptions::{PyOSError, PyRuntimeError, PyTimeoutError, PyValueError},
    prelude::*,
};
use serde_json::{Value, json};
use tokio::sync::{Mutex, watch};

use crate::{from_python, to_python};

pyo3::create_exception!(_native, LogProcessError, PyRuntimeError);
pyo3::create_exception!(_native, JournalCursorError, PyRuntimeError);

pub(crate) fn error(error: anyhow::Error) -> PyErr {
    Python::attach(|py| {
        if let Some(process) = error.downcast_ref::<native::LogProcessError>() {
            let result = LogProcessError::new_err(process.to_string());
            let value = result.value(py);
            let _ = value.setattr("returncode", process.returncode);
            let _ = value.setattr("diagnostics", &process.diagnostics);
            let _ = value.setattr("diagnostics_truncated", process.diagnostics_truncated);
            let _ = value.setattr(
                "command",
                process
                    .command
                    .iter()
                    .map(|part| part.to_string_lossy().into_owned())
                    .collect::<Vec<_>>(),
            );
            result
        } else if let Some(cursor) = error.downcast_ref::<native::JournalCursorError>() {
            let result = JournalCursorError::new_err(cursor.to_string());
            let _ = result.value(py).setattr("cursor", &cursor.cursor);
            result
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
    })
}

fn positive_count(value: &Bound<'_, PyAny>) -> PyResult<usize> {
    from_python(value, 0, &mut 0)?
        .as_u64()
        .and_then(|value| usize::try_from(value).ok())
        .filter(|value| *value > 0)
        .ok_or_else(|| PyValueError::new_err("log reader limits must be positive integers"))
}
fn deadline(value: &Bound<'_, PyAny>) -> PyResult<Duration> {
    let value = from_python(value, 0, &mut 0)?
        .as_f64()
        .filter(|value| value.is_finite() && *value > 0.0)
        .ok_or_else(|| {
            PyValueError::new_err("log completion timeout must be positive and finite")
        })?;
    Duration::try_from_secs_f64(value)
        .map_err(|_| PyValueError::new_err("log completion timeout is too large"))
}
pub(crate) fn journal_query(value: &Bound<'_, PyAny>) -> PyResult<native::JournalQuery> {
    let request: native::JournalQuery = serde_json::from_value(from_python(value, 0, &mut 0)?)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    request.validate().map_err(error)?;
    Ok(request)
}
fn journal_record(record: &native::JournalRecord) -> Value {
    json!({"fields":record.fields, "cursor":record.cursor(), "timestamp_us":record.timestamp_us(), "unit":record.unit(), "message":record.message()})
}

pub(crate) fn journal_result(result: &native::JournalResult) -> Value {
    json!({"records":result.records.iter().map(journal_record).collect::<Vec<_>>(), "next_cursor":result.next_cursor, "has_more":result.has_more, "diagnostics":result.diagnostics, "diagnostics_truncated":result.diagnostics_truncated})
}

pub(crate) fn wrap_stream(
    py: Python<'_>,
    stream: native::JournalStream,
) -> PyResult<Py<JournalStream>> {
    let diagnostics = stream.diagnostics_handle();
    let pid = stream.pid();
    let (closing, _) = watch::channel(false);
    Py::new(
        py,
        JournalStream {
            inner: Arc::new(Mutex::new(stream)),
            diagnostics,
            pid,
            closing,
        },
    )
}

#[pyclass(name = "JournalLogs", module = "dst_server._native")]
struct JournalLogs {
    inner: Arc<native::JournalLogs>,
}
#[pymethods]
impl JournalLogs {
    #[new]
    fn new(
        executable: PathBuf,
        max_record_bytes: &Bound<'_, PyAny>,
        max_output_bytes: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: Arc::new(native::JournalLogs {
                executable,
                max_record_bytes: positive_count(max_record_bytes)?,
                max_output_bytes: positive_count(max_output_bytes)?,
            }),
        })
    }

    #[pyo3(signature = (units, request, completion_timeout))]
    fn query<'py>(
        &self,
        py: Python<'py>,
        units: Option<Vec<String>>,
        request: &Bound<'_, PyAny>,
        completion_timeout: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request = journal_query(request)?;
        let completion_timeout = deadline(completion_timeout)?;
        let reader = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = reader
                .query(units.as_deref(), &request, completion_timeout)
                .await
                .map_err(error)?;
            Python::attach(|py| to_python(py, journal_result(&result)))
        })
    }

    #[pyo3(signature = (units, request))]
    fn follow<'py>(
        &self,
        py: Python<'py>,
        units: Option<Vec<String>>,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request = journal_query(request)?;
        let reader = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let stream = reader
                .follow(units.as_deref(), &request)
                .await
                .map_err(error)?;
            Python::attach(|py| wrap_stream(py, stream))
        })
    }
}

#[pyclass(name = "JournalStream", module = "dst_server._native")]
pub(crate) struct JournalStream {
    inner: Arc<Mutex<native::JournalStream>>,
    diagnostics: native::JournalDiagnostics,
    pid: u32,
    closing: watch::Sender<bool>,
}

// A cancelled Python await must not strand a native reader behind its mutex.
struct CloseOnCancel {
    inner: Arc<Mutex<native::JournalStream>>,
    closing: watch::Sender<bool>,
    armed: bool,
}
impl CloseOnCancel {
    fn disarm(&mut self) {
        self.armed = false;
    }
}
impl Drop for CloseOnCancel {
    fn drop(&mut self) {
        if self.armed {
            self.closing.send_replace(true);
            let stream = self.inner.clone();
            pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                let _ = stream.lock().await.close().await;
            });
        }
    }
}
impl Drop for JournalStream {
    fn drop(&mut self) {
        self.closing.send_replace(true);
    }
}

#[pymethods]
impl JournalStream {
    #[getter]
    fn pid(&self) -> u32 {
        self.pid
    }
    #[getter]
    fn diagnostics(&self) -> String {
        self.diagnostics.snapshot().0
    }
    #[getter]
    fn diagnostics_truncated(&self) -> bool {
        self.diagnostics.snapshot().1
    }

    fn next<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let mut cleanup = CloseOnCancel {
            inner: self.inner.clone(),
            closing: self.closing.clone(),
            armed: true,
        };
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut closing = cleanup.closing.subscribe();
            let mut stream = cleanup.inner.lock().await;
            let record = if *closing.borrow_and_update() {
                stream.close().await.map_err(error)?;
                None
            } else {
                tokio::select! {
                    biased;
                    _ = closing.changed() => { stream.close().await.map_err(error)?; None },
                    record = stream.next_record() => record.map_err(error)?,
                }
            };
            drop(stream);
            cleanup.disarm();
            Python::attach(|py| {
                to_python(
                    py,
                    record.as_ref().map(journal_record).unwrap_or(Value::Null),
                )
            })
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.closing.send_replace(true);
        let mut cleanup = CloseOnCancel {
            inner: self.inner.clone(),
            closing: self.closing.clone(),
            armed: true,
        };
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            cleanup.inner.lock().await.close().await.map_err(error)?;
            cleanup.disarm();
            Ok(())
        })
    }
}

#[pyclass(name = "NetdataLogs", module = "dst_server._native")]
struct NetdataLogs {
    inner: Arc<native::NetdataLogs>,
}
#[pymethods]
impl NetdataLogs {
    #[new]
    fn new(
        executable: PathBuf,
        stock_config: PathBuf,
        config: PathBuf,
        max_concurrency: &Bound<'_, PyAny>,
        max_record_bytes: &Bound<'_, PyAny>,
        max_output_bytes: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: Arc::new(
                native::NetdataLogs::new(
                    executable,
                    stock_config,
                    config,
                    positive_count(max_concurrency)?,
                    positive_count(max_record_bytes)?,
                    positive_count(max_output_bytes)?,
                )
                .map_err(error)?,
            ),
        })
    }

    fn query<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'_, PyAny>,
        completion_timeout: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request: native::NetdataLogQuery =
            serde_json::from_value(from_python(request, 0, &mut 0)?)
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
        request.validate().map_err(error)?;
        let completion_timeout = deadline(completion_timeout)?;
        let reader = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = reader
                .query(&request, completion_timeout)
                .await
                .map_err(error)?;
            let mut value = serde_json::to_value(&result)
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            value["truncated"] = json!(result.truncated());
            Python::attach(|py| to_python(py, value))
        })
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<JournalLogs>()?;
    module.add_class::<JournalStream>()?;
    module.add_class::<NetdataLogs>()?;
    module.add("LogProcessError", module.py().get_type::<LogProcessError>())?;
    module.add(
        "JournalCursorError",
        module.py().get_type::<JournalCursorError>(),
    )?;
    Ok(())
}
