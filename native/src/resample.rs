//! Sample-rate conversion, built on rubato 5.
//!
//! Two entry points:
//!   * `resample_all` — one-shot, whole-clip conversion (FFT resampler,
//!     delay-trimmed). For sentence-level buffers.
//!   * `StreamingResampler` — stateful stream converter (cubic-polynomial
//!     async resampler; optional higher-quality sinc mode). Fed arbitrary
//!     chunk sizes, holds the remainder internally, `flush()` drains the tail.
//!
//! The Python app currently opens device streams at the provider's native
//! rate (PortAudio resamples), so these primitives plug in where a fixed
//! output rate is wanted without re-opening the device — they never touch
//! stream ownership themselves.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rubato::audioadapter_buffers::direct::InterleavedSlice;
use rubato::{
    Async, Fft, FixedAsync, FixedSync, Indexing, PolynomialDegree, Resampler,
    SincInterpolationParameters, WindowFunction,
};
use std::collections::VecDeque;
use std::sync::Mutex;

fn to_pyerr(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// One-shot, whole-clip resampling of interleaved float32 data.
///
/// `data` length must be a whole multiple of `channels` (a trailing partial
/// frame is ignored). Returns interleaved float32 at `to_rate`, with the
/// resampler's startup delay trimmed so the output lines up with the input.
#[pyfunction]
pub fn resample_all(
    py: Python<'_>,
    data: PyReadonlyArray1<'_, f32>,
    from_rate: usize,
    to_rate: usize,
    channels: usize,
) -> PyResult<Py<PyArray1<f32>>> {
    if from_rate == 0 || to_rate == 0 {
        return Err(PyValueError::new_err("sample rates must be >= 1"));
    }
    if channels == 0 {
        return Err(PyValueError::new_err("channels must be >= 1"));
    }
    let slice = data.as_slice()?;
    let frames = slice.len() / channels;
    if frames == 0 || from_rate == to_rate {
        return Ok(slice.to_vec().into_pyarray(py).unbind());
    }
    let out: Vec<f32> = py
        .detach(|| -> Result<Vec<f32>, String> {
            let mut resampler = Fft::<f32>::new(from_rate, to_rate, 1024, channels, FixedSync::Both)
                .map_err(|e| e.to_string())?;
            let input = InterleavedSlice::new(slice, channels, frames).map_err(|e| e.to_string())?;
            let needed = resampler.process_all_needed_output_len(frames);
            let mut outdata = vec![0.0f32; needed * channels];
            let mut output =
                InterleavedSlice::new_mut(&mut outdata, channels, needed).map_err(|e| e.to_string())?;
            let (_consumed, produced) = resampler
                .process_all_into_buffer(&input, &mut output, frames, None)
                .map_err(|e| e.to_string())?;
            outdata.truncate(produced * channels);
            Ok(outdata)
        })
        .map_err(to_pyerr)?;
    Ok(out.into_pyarray(py).unbind())
}

/// Stateful streaming resampler for interleaved float32 PCM.
///
/// Arbitrary input chunk sizes are accepted; output is emitted as soon as it
/// is available. This is not the real-time device callback path (that stays
/// with PortAudio) — it only converts buffers on worker threads.
#[pyclass(module = "jarvis_native")]
pub struct StreamingResampler {
    // Mutex: rubato's Async resampler is Send but not Sync; PyO3 pyclasses
    // must be Sync. All access is GIL-serialised anyway, so the lock is
    // uncontended in practice and only exists to satisfy the type system.
    inner: Mutex<Async<f32>>,
    nch: usize,
    chunk_frames: usize,
    inbuf: VecDeque<f32>,
}

impl StreamingResampler {
    /// Process exactly one full chunk (fixed-input mode) and remove
    /// `drain_samples` interleaved samples from the input buffer.
    fn process_one(&mut self, drain_samples: usize, partial_frames: Option<usize>) -> PyResult<Vec<f32>> {
        let nch = self.nch;
        self.inbuf.make_contiguous();
        let (front, _) = self.inbuf.as_slices();
        let available_frames = front.len() / nch;
        let input = InterleavedSlice::new(front, nch, available_frames).map_err(to_pyerr)?;
        let mut inner = self.inner.lock().unwrap();
        let cap_frames = inner.output_frames_max();
        let mut outdata = vec![0.0f32; cap_frames * nch];
        let mut output = InterleavedSlice::new_mut(&mut outdata, nch, cap_frames).map_err(to_pyerr)?;
        let indexing = partial_frames.map(|n| Indexing::new().partial_len(n));
        let (_read, written) = inner
            .process_into_buffer(&input, &mut output, indexing.as_ref())
            .map_err(to_pyerr)?;
        drop(inner);
        let drain = drain_samples.min(self.inbuf.len());
        self.inbuf.drain(..drain);
        outdata.truncate(written * nch);
        Ok(outdata)
    }
}

#[pymethods]
impl StreamingResampler {
    #[new]
    #[pyo3(signature = (from_rate, to_rate, channels, chunk_size=1024, quality="poly"))]
    pub fn new(
        from_rate: usize,
        to_rate: usize,
        channels: usize,
        chunk_size: usize,
        quality: &str,
    ) -> PyResult<Self> {
        if from_rate == 0 || to_rate == 0 {
            return Err(PyValueError::new_err("sample rates must be >= 1"));
        }
        if channels == 0 {
            return Err(PyValueError::new_err("channels must be >= 1"));
        }
        if chunk_size < 16 {
            return Err(PyValueError::new_err("chunk_size must be >= 16"));
        }
        let ratio = to_rate as f64 / from_rate as f64;
        let max_rel = 1.1;
        let inner = match quality {
            "sinc" => Async::<f32>::new_sinc(
                ratio,
                max_rel,
                &SincInterpolationParameters::new(256, WindowFunction::BlackmanHarris2),
                chunk_size,
                channels,
                FixedAsync::Input,
            ),
            // Cubic polynomial: rubato's recommended trade-off for voice /
            // video-call style streams (no anti-alias sinc cost).
            _ => Async::<f32>::new_poly(
                ratio,
                max_rel,
                PolynomialDegree::Cubic,
                chunk_size,
                channels,
                FixedAsync::Input,
            ),
        }
        .map_err(to_pyerr)?;
        Ok(Self {
            inner: Mutex::new(inner),
            nch: channels,
            chunk_frames: chunk_size,
            inbuf: VecDeque::new(),
        })
    }

    /// Feed one arbitrary-sized interleaved chunk; returns every output frame
    /// that became available. An empty array is normal while input is short.
    pub fn process(
        &mut self,
        py: Python<'_>,
        data: PyReadonlyArray1<'_, f32>,
    ) -> PyResult<Py<PyArray1<f32>>> {
        let slice = data.as_slice()?;
        self.inbuf.extend(slice.iter().copied());
        let samples_per_chunk = self.chunk_frames * self.nch;
        let mut out: Vec<f32> = Vec::new();
        while self.inbuf.len() >= samples_per_chunk {
            out.extend(self.process_one(samples_per_chunk, None)?);
        }
        Ok(out.into_pyarray(py).unbind())
    }

    /// Drain the tail: the remaining (short) input is processed as a partial
    /// final chunk. The output may include up to roughly one chunk of
    /// trailing (near-silent) frames from the padding + filter window —
    /// harmless for playback, visible in exact-length assertions.
    pub fn flush(&mut self, py: Python<'_>) -> PyResult<Py<PyArray1<f32>>> {
        let nch = self.nch;
        let total = self.inbuf.len();
        let mut out: Vec<f32> = Vec::new();
        let frames = total / nch;
        if frames > 0 {
            out.extend(self.process_one(total, Some(frames))?);
        } else {
            self.inbuf.clear();
        }
        Ok(out.into_pyarray(py).unbind())
    }

    /// Clear all buffered input/output and internal resampler state.
    pub fn reset(&mut self) {
        self.inner.lock().unwrap().reset();
        self.inbuf.clear();
    }

    /// Frames currently held internally (below one chunk).
    pub fn buffered_frames(&self) -> usize {
        self.inbuf.len() / self.nch
    }

    /// Reported filter delay in output frames.
    pub fn latency_frames(&self) -> usize {
        self.inner.lock().unwrap().output_delay()
    }

    /// Output/input ratio actually in use.
    pub fn ratio(&self) -> f64 {
        self.inner.lock().unwrap().resample_ratio()
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(resample_all, m)?)?;
    m.add_class::<StreamingResampler>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sine_stays_sine_at_length() {
        // 440 Hz at 24 kHz, 0.5 s, resampled to 48 kHz → ~2× the frames.
        let n = 12_000usize;
        let signal: Vec<f32> = (0..n)
            .map(|i| (2.0 * std::f64::consts::PI * 440.0 * i as f64 / 24_000.0).sin() as f32)
            .collect();
        python_check_resample(&signal);
    }

    // The plain core used by tests does not need Python; this helper only
    // exercises the rubato configuration that `resample_all` uses.
    fn python_check_resample(signal: &[f32]) {
        let mut resampler =
            Fft::<f32>::new(24_000, 48_000, 1024, 1, FixedSync::Both).expect("construct");
        let input = InterleavedSlice::new(signal, 1, signal.len()).expect("adapter");
        let needed = resampler.process_all_needed_output_len(signal.len());
        let mut out = vec![0.0f32; needed];
        let mut output = InterleavedSlice::new_mut(&mut out, 1, needed).expect("adapter mut");
        let (consumed, produced) = resampler
            .process_all_into_buffer(&input, &mut output, signal.len(), None)
            .expect("process");
        assert_eq!(consumed, signal.len());
        assert!((produced as i64 - 24_000).abs() <= 2, "produced {produced}");
        // peak amplitude of the middle of the output must be ~1.0
        let mid = &out[8_000..16_000];
        let peak = mid.iter().fold(0.0f32, |m, &v| m.max(v.abs()));
        assert!(peak > 0.95, "peak {peak}");
    }

    #[test]
    fn streaming_resampler_piecewise_matches_length() {
        let mut r = StreamingResampler::new(22_050, 24_000, 1, 128, "poly").expect("new");
        let n = 22_050usize; // 1 s
        let signal: Vec<f32> = (0..n).map(|i| ((i % 97) as f32 / 97.0) - 0.5).collect();
        let mut produced = 0usize;
        for chunk in signal.chunks(511) {
            let v = vec_f32(chunk);
            let out = r.process_internal(&v);
            produced += out;
        }
        produced += r.flush_internal();
        // 1 s at 24 kHz ≈ 24 000 frames. `flush()` deliberately pads the
        // final short chunk to a full resampler chunk (the missing frames
        // read as silence), so the tail can exceed the ideal by up to ~one
        // chunk — all of it trailing, near-silent frames.
        assert!(
            (produced as i64 - 24_000).abs() < 240,
            "produced {produced} frames"
        );
    }

    // Small internal mirrors of the pymethods so tests avoid the Python API.
    impl StreamingResampler {
        fn process_internal(&mut self, data: &[f32]) -> usize {
            self.inbuf.extend(data.iter().copied());
            let samples_per_chunk = self.chunk_frames * self.nch;
            let mut total = 0usize;
            while self.inbuf.len() >= samples_per_chunk {
                total += self.process_one(samples_per_chunk, None).expect("process").len();
            }
            total
        }

        fn flush_internal(&mut self) -> usize {
            let nch = self.nch;
            let total = self.inbuf.len();
            let frames = total / nch;
            if frames == 0 {
                self.inbuf.clear();
                return 0;
            }
            self.process_one(total, Some(frames)).expect("flush").len()
        }
    }

    fn vec_f32(s: &[f32]) -> Vec<f32> {
        s.to_vec()
    }
}
