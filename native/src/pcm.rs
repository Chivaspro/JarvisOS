//! PCM hot-path primitives.
//!
//! Every core function mirrors a pure-Python/numpy helper in the app exactly
//! (same formula, same edge cases) so the Python layer can swap between the
//! two implementations without any behavioural change:
//!
//! | core here               | mirrors                                        |
//! |-------------------------|------------------------------------------------|
//! | `rms_i16_slice`         | `main._MicVoiceGate._rms`, `wake._rms`         |
//! | `level_from_rms`        | `main._pcm_level` (floor/full mapping)         |
//! | `envelope_i16_slice`    | `voice.tts._ReactiveEnvelope` per-frame RMS    |
//! | `compress_silence_f32_core` | `voice.tts._compress_silence`              |
//!
//! The `#[pyfunction]` wrappers are thin; the logic lives in plain slices so
//! `cargo test` exercises it without a Python interpreter.

use numpy::{
    IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArrayDyn, PyReadwriteArrayDyn,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// ───────────────────────── plain-Rust cores (unit-tested) ──────────────────

/// Root-mean-square of int16 samples in the raw ±32768 domain.
/// Mirrors `sqrt(mean(x.astype(float32) ** 2))`.
pub fn rms_i16_slice(s: &[i16]) -> f64 {
    if s.is_empty() {
        return 0.0;
    }
    let mut acc = 0.0f64;
    for &v in s {
        let x = v as f64;
        acc += x * x;
    }
    (acc / s.len() as f64).sqrt()
}

/// Absolute peak of int16 samples in the raw domain.
pub fn peak_i16_slice(s: &[i16]) -> f64 {
    let mut peak = 0.0f64;
    for &v in s {
        let a = (v as f64).abs();
        if a > peak {
            peak = a;
        }
    }
    peak
}

/// The `_pcm_level` mapping: silence at/below `floor`, full scale at `full`.
pub fn level_from_rms(rms: f64, floor: f64, full: f64) -> f64 {
    if rms <= floor || !(full > floor) {
        return 0.0;
    }
    ((rms - floor) / (full - floor)).min(1.0)
}

/// Root-mean-square of float samples (the `_push_reactive` / float-array case).
pub fn rms_f32_slice(s: &[f32]) -> f64 {
    if s.is_empty() {
        return 0.0;
    }
    let mut acc = 0.0f64;
    for &v in s {
        let x = v as f64;
        acc += x * x;
    }
    (acc / s.len() as f64).sqrt()
}

fn envelope_of<F: Fn(&[i16]) -> f64>(s: &[i16], frame: usize, f: F) -> Vec<f32> {
    if frame == 0 || s.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::with_capacity((s.len() + frame - 1) / frame);
    for chunk in s.chunks(frame) {
        out.push(f(chunk) as f32);
    }
    out
}

/// Per-frame RMS of int16 audio (raw domain). Frames shorter than `frame`
/// (only the final one) are measured over their actual length.
pub fn envelope_i16_slice(s: &[i16], frame: usize) -> Vec<f32> {
    envelope_of(s, frame, rms_i16_slice)
}

/// Per-frame RMS of float audio.
pub fn envelope_f32_slice(s: &[f32], frame: usize) -> Vec<f32> {
    if frame == 0 || s.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::with_capacity((s.len() + frame - 1) / frame);
    for chunk in s.chunks(frame) {
        out.push(rms_f32_slice(chunk) as f32);
    }
    out
}

/// Mirrors `voice.tts._compress_silence` exactly:
/// frames of `frame_len` samples whose `sqrt(mean(x²) + 1e-12) < threshold`
/// are "silent"; while silence accumulates past `max_samp` samples the
/// subsequent silent frames are dropped; any loud frame resets the counter.
pub fn compress_silence_f32_core(
    arr: &[f32],
    frame_len: usize,
    max_samp: usize,
    threshold: f32,
) -> Vec<f32> {
    if frame_len == 0 || arr.is_empty() {
        return arr.to_vec();
    }
    let mut out: Vec<f32> = Vec::with_capacity(arr.len());
    let mut silent_acc: usize = 0;
    for chunk in arr.chunks(frame_len) {
        let rms = rms_f32_slice(chunk);
        let silent = (rms * rms + 1e-12).sqrt() < threshold as f64;
        if silent {
            silent_acc += chunk.len();
            if silent_acc <= max_samp {
                out.extend_from_slice(chunk);
            }
        } else {
            silent_acc = 0;
            out.extend_from_slice(chunk);
        }
    }
    out
}

/// Linear fade-in over the first `fade_frames` samples.
pub fn fade_in_linear(s: &mut [f32], fade_frames: usize) {
    let n = fade_frames.min(s.len());
    if n == 0 {
        return;
    }
    let denom = n as f32;
    for (i, v) in s[..n].iter_mut().enumerate() {
        *v *= i as f32 / denom;
    }
}

/// Linear fade-out over the last `fade_frames` samples (click-free stop).
/// The final sample lands on exactly 0.0 — mirrors the Python fallback.
pub fn fade_out_linear(s: &mut [f32], fade_frames: usize) {
    let n = fade_frames.min(s.len());
    if n == 0 {
        return;
    }
    let denom = n as f32;
    let len = s.len();
    for (i, v) in s[len - n..].iter_mut().enumerate() {
        *v *= 1.0 - ((i + 1) as f32 / denom);
    }
}

// ───────────────────────── PyO3 wrappers ───────────────────────────────────

fn flat_i16(arr: &PyReadonlyArrayDyn<'_, i16>) -> Vec<i16> {
    // Contiguous numpy views are the norm (sounddevice blocks, frombuffer
    // views); a stray non-contiguous array costs one copy, never a crash.
    match arr.as_slice() {
        Ok(s) => s.to_vec(),
        Err(_) => arr.as_array().iter().copied().collect(),
    }
}

#[pyfunction]
pub fn rms_i16(arr: PyReadonlyArrayDyn<'_, i16>) -> f64 {
    match arr.as_slice() {
        Ok(s) => rms_i16_slice(s),
        Err(_) => rms_i16_slice(&arr.as_array().iter().copied().collect::<Vec<i16>>()),
    }
}

#[pyfunction]
pub fn peak_i16(arr: PyReadonlyArrayDyn<'_, i16>) -> f64 {
    match arr.as_slice() {
        Ok(s) => peak_i16_slice(s),
        Err(_) => peak_i16_slice(&arr.as_array().iter().copied().collect::<Vec<i16>>()),
    }
}

#[pyfunction]
pub fn level_i16(arr: PyReadonlyArrayDyn<'_, i16>, floor: f64, full: f64) -> f64 {
    let data = flat_i16(&arr);
    level_from_rms(rms_i16_slice(&data), floor, full)
}

#[pyfunction]
pub fn rms_f32(arr: PyReadonlyArrayDyn<'_, f32>) -> f64 {
    match arr.as_slice() {
        Ok(s) => rms_f32_slice(s),
        Err(_) => rms_f32_slice(&arr.as_array().iter().copied().collect::<Vec<f32>>()),
    }
}

#[pyfunction]
pub fn level_f32(arr: PyReadonlyArrayDyn<'_, f32>, floor: f64, full: f64) -> f64 {
    let rms = match arr.as_slice() {
        Ok(s) => rms_f32_slice(s),
        Err(_) => rms_f32_slice(&arr.as_array().iter().copied().collect::<Vec<f32>>()),
    };
    level_from_rms(rms, floor, full)
}

/// Per-frame RMS envelope of int16 audio (raw domain), for the voice-reactive
/// meter: the Python side only places the frames on its sleep schedule.
#[pyfunction]
pub fn envelope_i16(
    py: Python<'_>,
    arr: PyReadonlyArrayDyn<'_, i16>,
    frame: usize,
) -> PyResult<Py<PyArray1<f32>>> {
    let out: Vec<f32> = match arr.as_slice() {
        Ok(s) => py.detach(|| envelope_i16_slice(s, frame)),
        Err(_) => {
            let owned: Vec<i16> = arr.as_array().iter().copied().collect();
            py.detach(|| envelope_i16_slice(&owned, frame))
        }
    };
    Ok(out.into_pyarray(py).unbind())
}

/// Per-frame RMS envelope of float audio.
#[pyfunction]
pub fn envelope_f32(
    py: Python<'_>,
    arr: PyReadonlyArrayDyn<'_, f32>,
    frame: usize,
) -> PyResult<Py<PyArray1<f32>>> {
    let out: Vec<f32> = match arr.as_slice() {
        Ok(s) => py.detach(|| envelope_f32_slice(s, frame)),
        Err(_) => {
            let owned: Vec<f32> = arr.as_array().iter().copied().collect();
            py.detach(|| envelope_f32_slice(&owned, frame))
        }
    };
    Ok(out.into_pyarray(py).unbind())
}

/// Kokoro silence compressor — see `compress_silence_f32_core`.
#[pyfunction]
#[pyo3(signature = (arr, frame_len, max_samp, threshold))]
pub fn compress_silence_f32(
    py: Python<'_>,
    arr: PyReadonlyArray1<'_, f32>,
    frame_len: usize,
    max_samp: usize,
    threshold: f32,
) -> PyResult<Py<PyArray1<f32>>> {
    let out: Vec<f32> = match arr.as_slice() {
        Ok(s) => py.detach(|| compress_silence_f32_core(s, frame_len, max_samp, threshold)),
        Err(_) => {
            let owned: Vec<f32> = arr.as_array().iter().copied().collect();
            py.detach(move || compress_silence_f32_core(&owned, frame_len, max_samp, threshold))
        }
    };
    Ok(out.into_pyarray(py).unbind())
}

/// In-place gain. Requires a contiguous float32 array.
#[pyfunction]
pub fn gain_f32(mut arr: PyReadwriteArrayDyn<'_, f32>, gain: f64) -> PyResult<()> {
    let s = arr
        .as_slice_mut()
        .map_err(|_| PyValueError::new_err("gain_f32 requires a contiguous float32 array"))?;
    let g = gain as f32;
    for v in s.iter_mut() {
        *v *= g;
    }
    Ok(())
}

/// In-place linear fade. `fade_in=true` fades the start in, otherwise the end out.
#[pyfunction]
#[pyo3(signature = (arr, fade_frames, fade_in))]
pub fn fade_f32(
    mut arr: PyReadwriteArrayDyn<'_, f32>,
    fade_frames: usize,
    fade_in: bool,
) -> PyResult<()> {
    let s = arr
        .as_slice_mut()
        .map_err(|_| PyValueError::new_err("fade_f32 requires a contiguous float32 array"))?;
    if fade_in {
        fade_in_linear(s, fade_frames);
    } else {
        fade_out_linear(s, fade_frames);
    }
    Ok(())
}

/// int16 → float32 at 1/32768 (the app's standard conversion).
#[pyfunction]
pub fn i16_to_f32(py: Python<'_>, arr: PyReadonlyArrayDyn<'_, i16>) -> PyResult<Py<PyArray1<f32>>> {
    let src = flat_i16(&arr);
    let out: Vec<f32> = py.detach(move || src.iter().map(|&v| v as f32 / 32768.0).collect());
    Ok(out.into_pyarray(py).unbind())
}

/// float32 → int16, rounded and clipped (`x * 32767`, clamp to ±full scale).
#[pyfunction]
pub fn f32_to_i16(py: Python<'_>, arr: PyReadonlyArrayDyn<'_, f32>) -> PyResult<Py<PyArray1<i16>>> {
    let owned: Vec<f32> = match arr.as_slice() {
        Ok(s) => s.to_vec(),
        Err(_) => arr.as_array().iter().copied().collect(),
    };
    let out: Vec<i16> = py.detach(move || {
        owned
            .iter()
            .map(|&v| {
                let x = (v * 32767.0).round();
                x.clamp(-32768.0, 32767.0) as i16
            })
            .collect()
    });
    Ok(out.into_pyarray(py).unbind())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rms_i16, m)?)?;
    m.add_function(wrap_pyfunction!(peak_i16, m)?)?;
    m.add_function(wrap_pyfunction!(level_i16, m)?)?;
    m.add_function(wrap_pyfunction!(rms_f32, m)?)?;
    m.add_function(wrap_pyfunction!(level_f32, m)?)?;
    m.add_function(wrap_pyfunction!(envelope_i16, m)?)?;
    m.add_function(wrap_pyfunction!(envelope_f32, m)?)?;
    m.add_function(wrap_pyfunction!(compress_silence_f32, m)?)?;
    m.add_function(wrap_pyfunction!(gain_f32, m)?)?;
    m.add_function(wrap_pyfunction!(fade_f32, m)?)?;
    m.add_function(wrap_pyfunction!(i16_to_f32, m)?)?;
    m.add_function(wrap_pyfunction!(f32_to_i16, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rms_matches_hand_calculation() {
        // [3, -4] → sqrt((9 + 16) / 2) = sqrt(12.5)
        let r = rms_i16_slice(&[3, -4]);
        assert!((r - 12.5f64.sqrt()).abs() < 1e-12);
        assert_eq!(rms_i16_slice(&[]), 0.0);
    }

    #[test]
    fn level_mapping_edge_cases() {
        assert_eq!(level_from_rms(50.0, 60.0, 2600.0), 0.0);
        assert_eq!(level_from_rms(60.0, 60.0, 2600.0), 0.0);
        assert_eq!(level_from_rms(2600.0, 60.0, 2600.0), 1.0);
        assert_eq!(level_from_rms(5000.0, 60.0, 2600.0), 1.0);
        let mid = level_from_rms(1330.0, 60.0, 2600.0);
        assert!((mid - 0.5).abs() < 1e-9);
        // degenerate window must not divide by zero
        assert_eq!(level_from_rms(100.0, 60.0, 60.0), 0.0);
    }

    #[test]
    fn envelope_frames() {
        let data: Vec<i16> = (1..=10).collect();
        let env = envelope_i16_slice(&data, 4);
        assert_eq!(env.len(), 3); // 4 + 4 + 2
        let last = rms_i16_slice(&[9, 10]) as f32;
        assert!((env[2] - last).abs() < 1e-6);
    }

    #[test]
    fn compress_silence_drops_only_after_cap() {
        // frame_len 4; silence "x" = 0.0, speech "L" = 0.5
        // two speech samples keep silent_acc honest; max_samp = 4 → first
        // silent frame kept, second dropped, then loud resets everything.
        let mut arr: Vec<f32> = Vec::new();
        arr.extend_from_slice(&[0.5f32; 4]); // loud
        arr.extend_from_slice(&[0.0f32; 4]); // silent 0..4  (kept, <=4)
        arr.extend_from_slice(&[0.0f32; 4]); // silent 4..8  (dropped, >4)
        arr.extend_from_slice(&[0.5f32; 4]); // loud again
        let out = compress_silence_f32_core(&arr, 4, 4, 0.003);
        assert_eq!(out.len(), 12);
        // no silence at all → identical content
        let loud = [0.5f32; 16];
        assert_eq!(compress_silence_f32_core(&loud, 4, 4, 0.003).len(), 16);
    }

    #[test]
    fn fades_are_monotonic_and_bounded() {
        let mut v = [1.0f32; 8];
        fade_in_linear(&mut v, 4);
        assert_eq!(v[0], 0.0);
        assert!(v[1] < v[2] && v[2] < v[3] && v[3] < 1.0);
        assert_eq!(v[7], 1.0);

        let mut w = [1.0f32; 8];
        fade_out_linear(&mut w, 4);
        assert!(w[0] == 1.0);
        assert!(w[4] > w[5] && w[5] > w[6] && w[6] > w[7]);
        assert_eq!(w[7], 0.0);
        assert!((w[4] - 0.75).abs() < 1e-6);
    }

    #[test]
    fn rms_f32_and_peak() {
        assert!((rms_f32_slice(&[0.5, -0.5]) - 0.5).abs() < 1e-12);
        assert_eq!(peak_i16_slice(&[1, -32768, 5]), 32768.0);
    }
}
