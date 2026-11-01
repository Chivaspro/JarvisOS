//! Fixed-capacity int16 ring buffer for playback scheduling.
//!
//! Producers (TTS stream writers) push interleaved int16 frames; a consumer
//! drains fixed-size blocks. `clear()` makes cancellation immediate — no
//! waiting for a blocking device write to return — and the underrun counter
//! exposes starvation that is invisible today.
//!
//! Pure data structure: no threads, no allocation after construction. All
//! access is serialised by the Python GIL exactly like the `queue.Queue`s it
//! complements.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArrayDyn, PyReadwriteArrayDyn};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pyclass(module = "jarvis_native")]
pub struct PcmRing {
    buf: Vec<i16>,
    capacity: usize, // frames
    channels: usize,
    head: usize, // frame index of the oldest stored frame
    len: usize,  // stored frames
    finished: bool,
    underruns: u64,
    total_pushed: u64,
    total_popped: u64,
}

impl PcmRing {
    fn take_frames(&mut self, out: &mut [i16], want_frames: usize, count_underrun: bool) -> usize {
        let take = want_frames.min(self.len);
        let nch = self.channels;
        let cap = self.capacity;
        let mut r = self.head;
        let mut written = 0usize;
        let mut remaining = take;
        while remaining > 0 {
            let chunk = remaining.min(cap - r);
            let src = r * nch;
            let dst = written * nch;
            let n = chunk * nch;
            out[dst..dst + n].copy_from_slice(&self.buf[src..src + n]);
            r = (r + chunk) % cap;
            written += chunk;
            remaining -= chunk;
        }
        self.head = r;
        self.len -= take;
        self.total_popped += take as u64;
        if count_underrun && want_frames > take {
            self.underruns += 1;
        }
        take
    }
}

#[pymethods]
impl PcmRing {
    #[new]
    pub fn new(capacity_frames: usize, channels: usize) -> PyResult<Self> {
        if capacity_frames == 0 || channels == 0 {
            return Err(PyValueError::new_err(
                "capacity_frames and channels must be >= 1",
            ));
        }
        Ok(Self {
            buf: vec![0i16; capacity_frames * channels],
            capacity: capacity_frames,
            channels,
            head: 0,
            len: 0,
            finished: false,
            underruns: 0,
            total_pushed: 0,
            total_popped: 0,
        })
    }

    /// Push interleaved int16 frames. Writes as many whole frames as fit and
    /// returns that count (fewer than given when the ring is full — the
    /// caller decides whether to drop or retry; real-time audio drops).
    pub fn push(&mut self, data: PyReadonlyArrayDyn<'_, i16>) -> PyResult<usize> {
        let src: Vec<i16> = match data.as_slice() {
            Ok(s) => s.to_vec(),
            Err(_) => data.as_array().iter().copied().collect(),
        };
        let nch = self.channels;
        let frames = src.len() / nch;
        let space = self.capacity - self.len;
        let take = frames.min(space);
        if take == 0 {
            return Ok(0);
        }
        let cap = self.capacity;
        let mut w = (self.head + self.len) % cap;
        let mut src_off = 0usize;
        let mut remaining = take;
        while remaining > 0 {
            let chunk = remaining.min(cap - w);
            let dst = w * nch;
            let src_start = src_off * nch;
            let n = chunk * nch;
            self.buf[dst..dst + n].copy_from_slice(&src[src_start..src_start + n]);
            w = (w + chunk) % cap;
            src_off += chunk;
            remaining -= chunk;
        }
        self.len += take;
        self.total_pushed += take as u64;
        Ok(take)
    }

    /// Pop up to `frames` frames (all available when omitted). Returns an
    /// interleaved int16 array (possibly shorter than requested).
    #[pyo3(signature = (frames=None))]
    pub fn pop(&mut self, py: Python<'_>, frames: Option<usize>) -> PyResult<Py<PyArray1<i16>>> {
        let want = frames.unwrap_or(self.len);
        let mut out = vec![0i16; want * self.channels];
        let take = self.take_frames(&mut out, want, frames.is_some());
        out.truncate(take * self.channels);
        Ok(out.into_pyarray(py).unbind())
    }

    /// Pop directly into a caller-provided contiguous int16 array (no
    /// allocation at all). Returns the number of frames written.
    pub fn pop_into(&mut self, mut out: PyReadwriteArrayDyn<'_, i16>) -> PyResult<usize> {
        let s = out
            .as_slice_mut()
            .map_err(|_| PyValueError::new_err("pop_into requires a contiguous int16 array"))?;
        let nch = self.channels;
        let want_frames = s.len() / nch;
        Ok(self.take_frames(s, want_frames, true))
    }

    pub fn available_frames(&self) -> usize {
        self.len
    }

    pub fn capacity_frames(&self) -> usize {
        self.capacity
    }

    /// Drop everything buffered — the instant-interruption primitive.
    pub fn clear(&mut self) {
        self.head = 0;
        self.len = 0;
    }

    /// Producer side has no more data; after the buffer drains, `drained()`
    /// becomes true exactly once.
    pub fn finish(&mut self) {
        self.finished = true;
    }

    pub fn drained(&self) -> bool {
        self.finished && self.len == 0
    }

    pub fn stats(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new(py);
        d.set_item("available", self.len)?;
        d.set_item("capacity", self.capacity)?;
        d.set_item("channels", self.channels)?;
        d.set_item("underruns", self.underruns)?;
        d.set_item("total_pushed", self.total_pushed)?;
        d.set_item("total_popped", self.total_popped)?;
        d.set_item("finished", self.finished)?;
        Ok(d.into())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PcmRing>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ring(cap: usize, ch: usize) -> PcmRing {
        PcmRing::new(cap, ch).unwrap()
    }

    fn push_slice(r: &mut PcmRing, data: &[i16]) -> usize {
        let nch = r.channels;
        assert_eq!(data.len() % nch, 0);
        let frames = data.len() / nch;
        let space = r.capacity - r.len;
        let take = frames.min(space);
        if take == 0 {
            return 0;
        }
        let cap = r.capacity;
        let mut w = (r.head + r.len) % cap;
        let mut off = 0usize;
        let mut remaining = take;
        while remaining > 0 {
            let chunk = remaining.min(cap - w);
            let dst = w * nch;
            let src = off * nch;
            let n = chunk * nch;
            r.buf[dst..dst + n].copy_from_slice(&data[src..src + n]);
            w = (w + chunk) % cap;
            off += chunk;
            remaining -= chunk;
        }
        r.len += take;
        take
    }

    fn pop_all(r: &mut PcmRing) -> Vec<i16> {
        let mut out = vec![0i16; r.len * r.channels];
        let take = r.take_frames(&mut out, r.len, false);
        out.truncate(take * r.channels);
        out
    }

    #[test]
    fn fifo_order_with_wraparound() {
        let mut r = ring(4, 1);
        assert_eq!(push_slice(&mut r, &[1, 2, 3]), 3);
        assert_eq!(pop_all(&mut r), vec![1, 2, 3]);
        // wraps now: head is at frame 3
        assert_eq!(push_slice(&mut r, &[4, 5, 6, 7]), 4);
        assert_eq!(pop_all(&mut r), vec![4, 5, 6, 7]);
    }

    #[test]
    fn overflow_reports_short_write_and_keeps_oldest() {
        let mut r = ring(3, 1);
        assert_eq!(push_slice(&mut r, &[1, 2, 3, 4, 5]), 3);
        assert_eq!(r.available_frames(), 3);
        assert_eq!(pop_all(&mut r), vec![1, 2, 3]);
    }

    #[test]
    fn clear_is_instant() {
        let mut r = ring(8, 1);
        push_slice(&mut r, &[1, 2, 3, 4]);
        r.clear();
        assert_eq!(r.available_frames(), 0);
        assert_eq!(pop_all(&mut r), Vec::<i16>::new());
    }

    #[test]
    fn finish_and_drained() {
        let mut r = ring(8, 1);
        push_slice(&mut r, &[1, 2]);
        r.finish();
        assert!(!r.drained());
        let _ = pop_all(&mut r);
        assert!(r.drained());
    }

    #[test]
    fn underrun_counting_on_short_read() {
        let mut r = ring(8, 1);
        push_slice(&mut r, &[1, 2]);
        let mut out = vec![0i16; 4];
        let take = r.take_frames(&mut out, 4, true);
        assert_eq!(take, 2);
        assert_eq!(r.underruns, 1);
        // pop-all style reads are not underruns
        let take2 = r.take_frames(&mut vec![0i16; 4], 4, false);
        assert_eq!(take2, 0);
        assert_eq!(r.underruns, 1);
    }

    #[test]
    fn stereo_interleaving() {
        let mut r = ring(4, 2);
        assert_eq!(push_slice(&mut r, &[1, -1, 2, -2]), 2);
        assert_eq!(r.available_frames(), 2);
        assert_eq!(pop_all(&mut r), vec![1, -1, 2, -2]);
    }
}
