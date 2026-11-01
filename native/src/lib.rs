//! `jarvis_native` — the native half of J.A.R.V.I.S. OS.
//!
//! Scope (deliberately narrow — only paths that are measurably hot in the
//! pure-Python build):
//!   * PCM primitives (`pcm`) used by the mic callback, the voice-reactive
//!     meter, the Kokoro silence compressor and PCM format conversion.
//!   * Streaming + one-shot sample-rate conversion (`resample`, rubato 5).
//!   * A fixed-capacity int16 ring buffer (`ring`) for playback scheduling
//!     and instant cancellation.
//!
//! Everything here mirrors an existing pure-Python implementation exactly;
//! `core/native.py` picks whichever backend is available and the app never
//! requires this module to run.

mod pcm;
mod resample;
mod ring;

use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Build identity, for `core/native.py` diagnostics (Settings / setup wizard).
#[pyfunction]
fn native_info(py: Python<'_>) -> PyResult<Py<PyDict>> {
    let d = PyDict::new(py);
    d.set_item("module", "jarvis_native")?;
    d.set_item("version", env!("CARGO_PKG_VERSION"))?;
    d.set_item(
        "target",
        option_env!("JARVIS_NATIVE_TARGET").unwrap_or("unknown"),
    )?;
    d.set_item(
        "profile",
        option_env!("JARVIS_NATIVE_PROFILE").unwrap_or("unknown"),
    )?;
    d.set_item("arch", std::env::consts::ARCH)?;
    d.set_item("os", std::env::consts::OS)?;
    d.set_item("features", vec!["pcm", "resample", "ring"])?;
    Ok(d.into())
}

#[pymodule]
fn jarvis_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(native_info, m)?)?;
    pcm::register(m)?;
    resample::register(m)?;
    ring::register(m)?;
    Ok(())
}
