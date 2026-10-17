use pyo3::prelude::*;

use crate::{from_python, py_error, to_python};

#[pyfunction]
fn event_names() -> Vec<&'static str> {
    dst_server::events::EVENT_NAMES.to_vec()
}

#[pyfunction]
fn validate_event(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let value = from_python(value, 0, &mut 0)?;
    py.detach(|| dst_server::events::validate(&value))
        .map_err(py_error)?;
    to_python(py, value)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(event_names, module)?)?;
    module.add_function(wrap_pyfunction!(validate_event, module)?)?;
    Ok(())
}
