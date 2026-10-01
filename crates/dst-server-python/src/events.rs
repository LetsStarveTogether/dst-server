use pyo3::prelude::*;

use crate::{from_python, py_error, to_python};

#[pyfunction]
fn event_schema(py: Python<'_>) -> PyResult<Py<PyAny>> {
    to_python(py, dst_server::events::schema().clone())
}

#[pyfunction]
fn validate_event(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let value = from_python(value, 0, &mut 0)?;
    py.detach(|| dst_server::events::validate(&value))
        .map_err(py_error)?;
    to_python(py, value)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(event_schema, module)?)?;
    module.add_function(wrap_pyfunction!(validate_event, module)?)?;
    Ok(())
}
