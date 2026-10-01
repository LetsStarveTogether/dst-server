use std::path::PathBuf;

use dst_server::{model, rpc};
use pyo3::{
    exceptions::{PyException, PyTypeError, PyValueError},
    prelude::*,
    types::{PyAny, PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple},
};
use serde_json::{Map, Number, Value, json};

mod archive;
mod events;
mod external;
mod host;
mod logs;
mod settings;

pyo3::create_exception!(_native, DstError, PyException);

pub(crate) fn py_error(error: model::Error) -> PyErr {
    Python::attach(|py| {
        let result = DstError::new_err(error.message);
        let instance = result.value(py);
        let code = serde_json::to_value(error.code).unwrap();
        let _ = instance.setattr("code", code.as_str().unwrap());
        if let Ok(details) = to_python(py, error.details) {
            let _ = instance.setattr("details", details);
        }
        result
    })
}

pub(crate) fn from_python(
    value: &Bound<'_, PyAny>,
    depth: usize,
    count: &mut usize,
) -> PyResult<Value> {
    *count += 1;
    if depth > 64 || *count > 65_536 {
        return Err(PyValueError::new_err("value exceeds nesting or item limit"));
    }
    if let Some(value) = settings::configuration_value(value) {
        return Ok(value);
    }
    if value.is_none() {
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(value.extract()?));
    }
    if value.is_instance_of::<PyInt>() {
        let number = value
            .str()?
            .to_str()?
            .parse::<Number>()
            .map_err(|_| PyValueError::new_err("invalid integer"))?;
        return Ok(Value::Number(number));
    }
    if value.is_instance_of::<PyFloat>() {
        return Number::from_f64(value.extract()?)
            .map(Value::Number)
            .ok_or_else(|| PyValueError::new_err("numbers must be finite"));
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(Value::String(text.to_str()?.to_owned()));
    }
    if let Ok(object) = value.cast::<PyDict>() {
        let mut result = Map::new();
        for (key, value) in object.iter() {
            let key = key
                .cast::<PyString>()
                .map_err(|_| PyTypeError::new_err("object keys must be strings"))?;
            result.insert(
                key.to_str()?.to_owned(),
                from_python(&value, depth + 1, count)?,
            );
        }
        return Ok(Value::Object(result));
    }
    if let Ok(values) = value.cast::<PyList>() {
        return values
            .iter()
            .map(|value| from_python(&value, depth + 1, count))
            .collect::<PyResult<Vec<_>>>()
            .map(Value::Array);
    }
    if let Ok(values) = value.cast::<PyTuple>() {
        return values
            .iter()
            .map(|value| from_python(&value, depth + 1, count))
            .collect::<PyResult<Vec<_>>>()
            .map(Value::Array);
    }
    Err(PyTypeError::new_err(
        "expected None, bool, number, text, list, tuple or dict",
    ))
}

pub(crate) fn to_python(py: Python<'_>, value: Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(value) => value.into_pyobject(py)?.to_owned().into_any().unbind(),
        Value::String(value) => value.into_pyobject(py)?.into_any().unbind(),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                value.into_pyobject(py)?.into_any().unbind()
            } else if let Some(value) = value.as_u64() {
                value.into_pyobject(py)?.into_any().unbind()
            } else if value.is_f64() {
                value
                    .as_f64()
                    .unwrap()
                    .into_pyobject(py)?
                    .into_any()
                    .unbind()
            } else {
                py.get_type::<PyInt>().call1((value.to_string(),))?.unbind()
            }
        }
        Value::Array(values) => {
            let result = PyList::empty(py);
            for value in values {
                result.append(to_python(py, value)?)?;
            }
            result.into_any().unbind()
        }
        Value::Object(values) => {
            let result = PyDict::new(py);
            for (key, value) in values {
                result.set_item(key, to_python(py, value)?)?;
            }
            result.into_any().unbind()
        }
    })
}

#[pyclass(name = "Client", module = "dst_server._native")]
struct Client {
    inner: rpc::Client,
}

#[pymethods]
impl Client {
    #[staticmethod]
    fn connect(py: Python<'_>, path: PathBuf) -> PyResult<Bound<'_, PyAny>> {
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = rpc::Client::connect(path).await.map_err(py_error)?;
            Python::attach(|py| Py::new(py, Client { inner }))
        })
    }

    #[pyo3(signature = (method, arguments=None, *, shard=None, timeout=None))]
    fn call<'py>(
        &self,
        py: Python<'py>,
        method: &str,
        arguments: Option<&Bound<'_, PyAny>>,
        shard: Option<String>,
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let arguments = arguments
            .map(|value| from_python(value, 0, &mut 0))
            .transpose()?
            .unwrap_or_else(|| json!({}));
        let request = serde_json::from_value::<model::Request>(
            json!({"method":method,"arguments":arguments}),
        )
        .map_err(|_| {
            PyValueError::new_err(
                "invalid method or arguments; use describe() for the request schema",
            )
        })?;
        let envelope = model::Envelope {
            target: shard.map_or(model::Target::Room, model::Target::Shard),
            request,
            timeout,
        };
        envelope.validate().map_err(py_error)?;
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let value = client.call(envelope).await.map_err(py_error)?;
            Python::attach(|py| to_python(py, value))
        })
    }

    fn describe<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let value = client.describe().await.map_err(py_error)?;
            Python::attach(|py| to_python(py, value))
        })
    }

    fn subscribe<'py>(&self, py: Python<'py>, kind: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let inner = client.subscribe(&kind).await.map_err(py_error)?;
            Python::attach(|py| Py::new(py, Subscription { inner }))
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client.close().await.map_err(py_error)
        })
    }
}

#[pyclass(name = "Subscription", module = "dst_server._native")]
struct Subscription {
    inner: rpc::Subscription,
}

#[pymethods]
impl Subscription {
    #[pyo3(signature = (max_items=512))]
    fn next<'py>(&self, py: Python<'py>, max_items: u16) -> PyResult<Bound<'py, PyAny>> {
        let subscription = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let batch = subscription.next(max_items).await.map_err(py_error)?;
            let value =
                serde_json::to_value(batch).map_err(|e| PyValueError::new_err(e.to_string()))?;
            Python::attach(|py| to_python(py, value))
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let subscription = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            subscription.close().await.map_err(py_error)
        })
    }
}

#[pyfunction]
#[pyo3(signature = (scope="room"))]
fn describe(py: Python<'_>, scope: &str) -> PyResult<Py<PyAny>> {
    let scope = match scope {
        "room" => model::Scope::Room,
        "shard" => model::Scope::Shard,
        _ => return Err(PyValueError::new_err("scope must be room or shard")),
    };
    to_python(py, serde_json::to_value(model::describe(scope)).unwrap())
}

#[pyfunction]
fn inspect(py: Python<'_>, directory: PathBuf) -> PyResult<Py<PyAny>> {
    let cluster = py
        .detach(|| dst_server::configuration::discover(directory))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    to_python(py, serde_json::to_value(cluster).unwrap())
}

#[pyfunction]
fn run_cli(py: Python<'_>, arguments: Vec<String>) -> u8 {
    py.detach(move || {
        dst_server::cli::main(
            std::iter::once("dst-server".into())
                .chain(arguments)
                .map(Into::into),
        )
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    archive::register(module)?;
    events::register(module)?;
    external::register(module)?;
    host::register(module)?;
    logs::register(module)?;
    settings::register(module)?;
    module.add_class::<Client>()?;
    module.add_class::<Subscription>()?;
    module.add("DstError", module.py().get_type::<DstError>())?;
    module.add_function(wrap_pyfunction!(describe, module)?)?;
    module.add_function(wrap_pyfunction!(inspect, module)?)?;
    module.add_function(wrap_pyfunction!(run_cli, module)?)?;
    Ok(())
}
