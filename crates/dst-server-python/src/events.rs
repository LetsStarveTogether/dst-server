use dst_server::events::coverage::CoverageBody;
use pyo3::{exceptions::PyValueError, prelude::*};

use crate::{from_python, py_error, to_python};

#[pyfunction]
fn event_capture(py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
    let requirements = dst_server::events::types::capture_requirements(name)
        .ok_or_else(|| PyValueError::new_err("unknown gameplay event"))?;
    to_python(py, serde_json::json!(requirements))
}

#[pyfunction]
fn event_names() -> Vec<&'static str> {
    dst_server::events::EVENT_NAMES.to_vec()
}

#[pyfunction]
fn validate_event(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let value = from_python(value)?;
    py.detach(|| dst_server::events::validate(&value))
        .map_err(py_error)?;
    to_python(py, value)
}

#[pyfunction]
fn validate_coverage(value: &Bound<'_, PyAny>) -> PyResult<()> {
    if CoverageBody::valid(&from_python(value)?) {
        Ok(())
    } else {
        Err(PyValueError::new_err("invalid telemetry coverage body"))
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(event_capture, module)?)?;
    module.add_function(wrap_pyfunction!(event_names, module)?)?;
    module.add_function(wrap_pyfunction!(validate_event, module)?)?;
    module.add_function(wrap_pyfunction!(validate_coverage, module)?)?;
    Ok(())
}
