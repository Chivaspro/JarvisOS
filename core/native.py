"""Native (Rust) acceleration backend — with a pure-numpy fallback.

The optional ``jarvis_native`` extension (built from ``native/``) carries the
PCM/DSP hot paths: mic-block level & RMS, the voice-reactive envelope, Kokoro
silence compression, format conversion, sample-rate conversion and the int16
playback ring. Everything in this module is *optional*: when the extension is
missing, fails to load, or is disabled, the identical formulas run in numpy.

Contract
--------
* Every function here keeps the exact semantics of the pure-Python code it
  replaces (see ``native/src/pcm.rs`` unit tests and
  ``tests/unit/test_native_backend.py`` parity tests).
* Loading is LAZY: importing this module never loads the DLL. The backend
  resolves on first use, so app startup is unaffected.
* Failures never raise into the caller: a broken extension degrades to the
  fallback and the real error is kept for ``backend_info()`` diagnostics.

Env:
    ``JARVIS_DISABLE_NATIVE=1`` — force the pure-Python backend (kill switch
    for debugging and for the fallback test matrix).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
#: Development build output — tools/build_native.py drops `jarvis_native.pyd`
#: here; maturin/`pip install` puts the module in site-packages instead.
NATIVE_DIR = ROOT / "native"

#: What a usable native module must expose.
_REQUIRED_SYMBOLS = (
    "__version__",
    "native_info",
    "rms_i16",
    "peak_i16",
    "rms_f32",
    "level_i16",
    "level_f32",
    "envelope_i16",
    "envelope_f32",
    "compress_silence_f32",
    "gain_f32",
    "fade_f32",
    "i16_to_f32",
    "f32_to_i16",
    "resample_all",
    "StreamingResampler",
    "PcmRing",
)

_DISABLE_VALUES = {"1", "true", "yes", "on"}

_lock = threading.Lock()
_native_module = None          # module object once loaded, else None
_backend_error: str | None = None  # why the native backend is not in use
_resolved = False              # has _resolve() run?
_use_native = False


# ── backend resolution ──────────────────────────────────────────────────────


def _resolve() -> None:
    """Decide, once, whether the native module is usable. Never raises."""
    global _native_module, _backend_error, _resolved, _use_native
    if _resolved:
        return
    with _lock:
        if _resolved:
            return
        _resolved = True
        flag = os.environ.get("JARVIS_DISABLE_NATIVE", "").strip().lower()
        if flag in _DISABLE_VALUES:
            _backend_error = "disabled by JARVIS_DISABLE_NATIVE"
            return

        candidates: list[str] = []
        native_dir = str(NATIVE_DIR)
        if NATIVE_DIR.is_dir() and native_dir not in sys.path:
            # Dev layout: the .pyd lives next to the crate sources.
            sys.path.insert(0, native_dir)

        first_error: str | None = None
        try:
            import jarvis_native  # type: ignore[import-not-found]

            missing = [s for s in _REQUIRED_SYMBOLS if not hasattr(jarvis_native, s)]
            if missing:
                first_error = (
                    "jarvis_native is incomplete (missing: " + ", ".join(missing) + ")"
                )
            else:
                _native_module = jarvis_native
                _use_native = True
                return
        except Exception as exc:  # ImportError, OSError (DLL load), ...
            first_error = f"{type(exc).__name__}: {exc}"
        candidates.append(f"import jarvis_native → {first_error}")
        _backend_error = "; ".join(candidates)


def reload_backend() -> None:
    """Forget the resolved backend and resolve again on next use (tests)."""
    global _native_module, _backend_error, _resolved, _use_native
    with _lock:
        _native_module = None
        _backend_error = None
        _resolved = False
        _use_native = False
    for _name in _PROXY_NAMES:
        globals().pop(_name, None)


#: Names resolved through the active backend on first attribute access and
#: cached in the module dict afterwards (PEP 562). Call sites use them as
#: ``native.PcmRing(...)`` / ``native.StreamingResampler(...)`` and keep
#: working across backend switches.
_PROXY_NAMES = ("StreamingResampler", "PcmRing", "native_info")


def __getattr__(name: str):
    if name in _PROXY_NAMES:
        value = getattr(_get_backend(), name)
        globals()[name] = value  # cheap fast path afterwards
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_backend():
    _resolve()
    return _native_module if _use_native else _FALLBACK


def backend_name() -> str:
    """``"rust"`` or ``"python"``."""
    _resolve()
    return "rust" if _use_native else "python"


def backend_info() -> dict:
    """Diagnostics snapshot for the wizard, Settings and logs."""
    _resolve()
    info: dict = {
        "backend": backend_name(),
        "disable_env": os.environ.get("JARVIS_DISABLE_NATIVE", ""),
        "error": _backend_error,
        "numpy": np.__version__,
        "python": sys.version.split()[0],
        "native_dir": str(NATIVE_DIR),
    }
    if _use_native and _native_module is not None:
        try:
            info["native"] = dict(_native_module.native_info())
        except Exception as exc:  # pragma: no cover - defensive
            info["native"] = {"error": f"{type(exc).__name__}: {exc}"}
    return info


# ── input normalisation ─────────────────────────────────────────────────────


def _as_i16(data) -> np.ndarray:
    """Any int16-shaped input as a flat int16 array (zero copy when possible).

    Accepts bytes / bytearray / memoryview of little-endian int16 (the wire
    format of every streamed PCM path) and numpy arrays of dtype int16.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(data, dtype=np.int16)
        return arr
    arr = np.asarray(data)
    if arr.dtype == np.int16:
        return arr.reshape(-1) if arr.ndim != 1 else arr
    raise TypeError(f"expected int16 PCM data, got dtype {arr.dtype!r}")


def _as_f32(data) -> np.ndarray:
    """Any float-shaped input as a contiguous flat float32 array."""
    arr = np.asarray(data)
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32, copy=False)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    return np.ascontiguousarray(arr)


def _is_i16_like(data) -> bool:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return True
    return isinstance(data, np.ndarray) and data.dtype == np.int16


# ── app-facing API (auto-dispatch, dtype-aware) ─────────────────────────────


def rms_auto(data) -> float:
    """RMS of int16 PCM (raw ±32768 domain) or float PCM (its own domain)."""
    backend = _get_backend()
    if _is_i16_like(data):
        return float(backend.rms_i16(_as_i16(data)))
    arr = np.asarray(data)
    if arr.dtype.kind == "f":
        return float(backend.rms_f32(_as_f32(arr)))
    raise TypeError(f"unsupported array dtype for RMS: {arr.dtype!r}")


def level_auto(data, floor: float, full: float) -> float:
    """The HUD loudness mapping (``main._pcm_level`` semantics), dtype-aware."""
    backend = _get_backend()
    if _is_i16_like(data):
        return float(backend.level_i16(_as_i16(data), float(floor), float(full)))
    arr = np.asarray(data)
    if arr.dtype.kind == "f":
        return float(backend.level_f32(_as_f32(arr), float(floor), float(full)))
    raise TypeError(f"unsupported array dtype for level: {arr.dtype!r}")


def envelope_auto(data, frame: int) -> np.ndarray:
    """Per-frame RMS envelope; the domain follows the input dtype."""
    backend = _get_backend()
    if _is_i16_like(data):
        return backend.envelope_i16(_as_i16(data), int(frame))
    arr = np.asarray(data)
    if arr.dtype.kind == "f":
        return backend.envelope_f32(_as_f32(arr), int(frame))
    raise TypeError(f"unsupported array dtype for envelope: {arr.dtype!r}")


def compress_silence_f32(arr, frame_len: int, max_samp: int, threshold: float) -> np.ndarray:
    """Trim long silence runs (``voice.tts._compress_silence`` semantics)."""
    return _get_backend().compress_silence_f32(
        _as_f32(arr), int(frame_len), int(max_samp), float(threshold)
    )


def gain_f32(arr: np.ndarray, gain: float) -> None:
    """In-place gain on a contiguous float32 array."""
    if not (isinstance(arr, np.ndarray) and arr.dtype == np.float32 and arr.flags["C_CONTIGUOUS"]):
        raise TypeError("gain_f32 requires a contiguous float32 numpy array")
    _get_backend().gain_f32(arr, float(gain))


def fade_f32(arr: np.ndarray, fade_frames: int, fade_in: bool) -> None:
    """In-place linear fade on a contiguous float32 array."""
    if not (isinstance(arr, np.ndarray) and arr.dtype == np.float32 and arr.flags["C_CONTIGUOUS"]):
        raise TypeError("fade_f32 requires a contiguous float32 numpy array")
    _get_backend().fade_f32(arr, int(fade_frames), bool(fade_in))


def i16_to_f32(data) -> np.ndarray:
    """int16 PCM → float32 at 1/32768."""
    return _get_backend().i16_to_f32(_as_i16(data))


def f32_to_i16(data) -> np.ndarray:
    """float32 PCM → int16, rounded and clipped."""
    return _get_backend().f32_to_i16(_as_f32(data))


def resample_all(data, from_rate: int, to_rate: int, channels: int = 1) -> np.ndarray:
    """One-shot whole-buffer resample of interleaved float32 PCM."""
    if channels < 1:
        raise ValueError("channels must be >= 1")
    return _get_backend().resample_all(_as_f32(data), int(from_rate), int(to_rate), int(channels))


# ── pure-Python fallback backend ────────────────────────────────────────────


class _PyStreamingResampler:
    """Incremental linear-interpolation resampler (fallback only).

    Lower quality than the native rubato resampler and no delay-matched flush,
    but sample-accurate pacing and identical call semantics.
    """

    def __init__(self, from_rate, to_rate, channels=1, chunk_size=1024, quality="poly"):
        if from_rate < 1 or to_rate < 1:
            raise ValueError("sample rates must be >= 1")
        if channels < 1:
            raise ValueError("channels must be >= 1")
        if chunk_size < 16:
            raise ValueError("chunk_size must be >= 16")
        self.from_rate = int(from_rate)
        self.to_rate = int(to_rate)
        self.channels = int(channels)
        self.chunk_size = int(chunk_size)
        self.quality = str(quality)
        self._tail: np.ndarray | None = None
        self._pos = 0.0
        self._step = float(from_rate) / float(to_rate)  # input frames per output frame

    @property
    def ratio(self) -> float:
        return self.to_rate / self.from_rate

    def buffered_frames(self) -> int:
        return 0 if self._tail is None else 1

    def latency_frames(self) -> int:
        return 1

    def process(self, data) -> np.ndarray:
        x = _as_f32(data).reshape(-1, self.channels)
        if x.shape[0] == 0:
            return np.zeros(0, dtype=np.float32)
        ext = x if self._tail is None else np.vstack([self._tail, x])
        pos = self._pos
        n = ext.shape[0]
        outs: list[np.ndarray] = []
        while pos < n - 1:
            i = int(pos)
            f = pos - i
            outs.append(ext[i] * (1.0 - f) + ext[i + 1] * f)
            pos += self._step
        self._tail = ext[-1:].copy()
        self._pos = pos - (n - 1)
        if not outs:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(outs, dtype=np.float32).reshape(-1)

    def flush(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)

    def reset(self) -> None:
        self._tail = None
        self._pos = 0.0


class _PyPcmRing:
    """Numpy mirror of ``jarvis_native.PcmRing`` (fallback only)."""

    def __init__(self, capacity_frames: int, channels: int):
        if capacity_frames < 1 or channels < 1:
            raise ValueError("capacity_frames and channels must be >= 1")
        self._cap = int(capacity_frames)
        self._ch = int(channels)
        self._buf = np.zeros((self._cap, self._ch), dtype=np.int16)
        self._head = 0
        self._len = 0
        self._finished = False
        self._underruns = 0
        self._pushed = 0
        self._popped = 0

    def push(self, data) -> int:
        flat = _as_i16(data)
        frames = flat.size // self._ch
        if frames == 0:
            return 0
        x = flat[: frames * self._ch].reshape(frames, self._ch)
        take = min(frames, self._cap - self._len)
        if take <= 0:
            return 0
        w = (self._head + self._len) % self._cap
        room_to_end = self._cap - w
        if take <= room_to_end:
            self._buf[w : w + take] = x[:take]
        else:
            k = room_to_end
            self._buf[w:] = x[:k]
            self._buf[: take - k] = x[k:take]
        self._len += take
        self._pushed += take
        return take

    def _read(self, take: int) -> np.ndarray:
        out = np.empty((take, self._ch), dtype=np.int16)
        r = self._head
        remaining = take
        written = 0
        while remaining > 0:
            chunk = min(remaining, self._cap - r)
            out[written : written + chunk] = self._buf[r : r + chunk]
            r = (r + chunk) % self._cap
            written += chunk
            remaining -= chunk
        self._head = r
        self._len -= take
        self._popped += take
        return out

    def pop(self, frames=None) -> np.ndarray:
        want = self._len if frames is None else int(frames)
        take = min(want, self._len)
        if frames is not None and want > take:
            self._underruns += 1
        return self._read(take).reshape(-1)

    def pop_into(self, out) -> int:
        if not (isinstance(out, np.ndarray) and out.dtype == np.int16 and out.flags["C_CONTIGUOUS"]):
            raise TypeError("pop_into requires a contiguous int16 numpy array")
        flat = out.reshape(-1)
        want = flat.size // self._ch
        take = min(want, self._len)
        if want > take:
            self._underruns += 1
        flat[: take * self._ch] = self._read(take).reshape(-1)
        return take

    def available_frames(self) -> int:
        return self._len

    def capacity_frames(self) -> int:
        return self._cap

    def clear(self) -> None:
        self._head = 0
        self._len = 0

    def finish(self) -> None:
        self._finished = True

    def drained(self) -> bool:
        return self._finished and self._len == 0

    def stats(self) -> dict:
        return {
            "available": self._len,
            "capacity": self._cap,
            "channels": self._ch,
            "underruns": self._underruns,
            "total_pushed": self._pushed,
            "total_popped": self._popped,
            "finished": self._finished,
        }


def _rms_from_float(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    xf = x.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(xf * xf, dtype=np.float64)))


def _level_from_rms(rms: float, floor: float, full: float) -> float:
    if rms <= floor or not (full > floor):
        return 0.0
    return float(min(1.0, (rms - floor) / (full - floor)))


def _envelope(arr: np.ndarray, frame: int, rms_fn) -> np.ndarray:
    if frame < 1 or arr.size == 0:
        return np.zeros(0, dtype=np.float32)
    n_frames = (arr.size + frame - 1) // frame
    out = np.empty(n_frames, dtype=np.float32)
    for i in range(n_frames):
        chunk = arr[i * frame : (i + 1) * frame]
        out[i] = rms_fn(chunk)
    return out


class _FallbackBackend:
    """Module-shaped namespace with the numpy implementations."""

    name = "python"

    # -- int16 domain --------------------------------------------------

    @staticmethod
    def rms_i16(arr: np.ndarray) -> float:
        return _rms_from_float(arr)

    @staticmethod
    def peak_i16(arr: np.ndarray) -> float:
        if arr.size == 0:
            return 0.0
        return float(np.max(np.abs(arr.astype(np.float64, copy=False))))

    @staticmethod
    def level_i16(arr: np.ndarray, floor: float, full: float) -> float:
        return _level_from_rms(_rms_from_float(arr), floor, full)

    @staticmethod
    def envelope_i16(arr: np.ndarray, frame: int) -> np.ndarray:
        return _envelope(arr, frame, _rms_from_float)

    # -- float domain --------------------------------------------------

    @staticmethod
    def rms_f32(arr: np.ndarray) -> float:
        return _rms_from_float(arr)

    @staticmethod
    def level_f32(arr: np.ndarray, floor: float, full: float) -> float:
        return _level_from_rms(_rms_from_float(arr), floor, full)

    @staticmethod
    def envelope_f32(arr: np.ndarray, frame: int) -> np.ndarray:
        return _envelope(arr, frame, _rms_from_float)

    @staticmethod
    def compress_silence_f32(
        arr: np.ndarray, frame_len: int, max_samp: int, threshold: float
    ) -> np.ndarray:
        if frame_len < 1 or arr.size == 0:
            return np.array(arr, dtype=np.float32, copy=True)
        out: list[np.ndarray] = []
        silent_acc = 0
        for i in range(0, arr.size, frame_len):
            chunk = arr[i : i + frame_len]
            rms = np.sqrt(np.mean(chunk**2) + 1e-12)
            if rms < threshold:
                silent_acc += chunk.size
                if silent_acc <= max_samp:
                    out.append(chunk)
            else:
                silent_acc = 0
                out.append(chunk)
        if not out:
            return np.array(arr, dtype=np.float32, copy=True)
        return np.concatenate(out).astype(np.float32, copy=False)

    @staticmethod
    def gain_f32(arr: np.ndarray, gain: float) -> None:
        np.multiply(arr, np.float32(gain), out=arr)

    @staticmethod
    def fade_f32(arr: np.ndarray, fade_frames: int, fade_in: bool) -> None:
        n = min(int(fade_frames), arr.size)
        if n <= 0:
            return
        ramp = (np.arange(n, dtype=np.float32) / np.float32(n))
        if fade_in:
            arr[:n] *= ramp
        else:
            arr[-n:] *= ramp[::-1]

    @staticmethod
    def i16_to_f32(arr: np.ndarray) -> np.ndarray:
        return arr.astype(np.float32, copy=False) / np.float32(32768.0)

    @staticmethod
    def f32_to_i16(arr: np.ndarray) -> np.ndarray:
        return np.clip(np.round(arr * np.float32(32767.0)), -32768, 32767).astype(np.int16)

    @staticmethod
    def resample_all(
        data: np.ndarray, from_rate: int, to_rate: int, channels: int
    ) -> np.ndarray:
        if from_rate < 1 or to_rate < 1:
            raise ValueError("sample rates must be >= 1")
        frames = data.size // channels
        if frames == 0 or from_rate == to_rate:
            return np.array(data, dtype=np.float32, copy=True)
        x = data[: frames * channels].reshape(frames, channels)
        out_frames = int(round(frames * to_rate / from_rate))
        if out_frames < 1:
            return np.zeros(0, dtype=np.float32)
        t_in = np.arange(frames, dtype=np.float64) / from_rate
        t_out = np.arange(out_frames, dtype=np.float64) / to_rate
        out = np.empty((out_frames, channels), dtype=np.float32)
        for c in range(channels):
            out[:, c] = np.interp(t_out, t_in, x[:, c]).astype(np.float32)
        return out.reshape(-1)

    @staticmethod
    def native_info() -> dict:
        return {
            "module": "pure-python fallback",
            "version": "0.0.0",
            "features": ["pcm", "resample", "ring"],
        }

    # -- classes -------------------------------------------------------

    StreamingResampler = _PyStreamingResampler
    PcmRing = _PyPcmRing


_FALLBACK = _FallbackBackend()
