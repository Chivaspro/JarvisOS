"""Native backend (``core.native``) — fallback correctness + Rust parity.

Two layers of tests, matching the app's own two-layer design:

* **Fallback tests** always run (CI has no Rust toolchain): they pin the exact
  formulas the native code must also implement — these are the behavioural
  contract the whole hybrid rests on.
* **Parity tests** run whenever ``jarvis_native`` has been built
  (``python tools/build_native.py``) and are skipped otherwise; they compare
  the Rust implementation against literal numpy references written here, so
  the Rust side is verified against the specification, not against itself.

The build helper (``tools/build_native.py``) itself is smoke-tested: it must
run stdlib-only and exit 0 (built) or 3 (toolchain missing) — never crash.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

from core import native  # noqa: E402


@pytest.fixture()
def forced_fallback(monkeypatch):
    """Explicitly pin the pure-Python backend for the duration of a test."""
    monkeypatch.setenv("JARVIS_DISABLE_NATIVE", "1")
    native.reload_backend()
    yield native
    native.reload_backend()


def _has_native() -> bool:
    native.reload_backend()
    return native.backend_name() == "rust"


NATIVE_AVAILABLE = _has_native()

needs_native = pytest.mark.skipif(
    not NATIVE_AVAILABLE, reason="jarvis_native not built (run tools/build_native.py)"
)


def _jn():
    """The raw extension module (only call under ``needs_native``)."""
    return pytest.importorskip("jarvis_native")


# ── reference formulas (the specification both backends must satisfy) ───────


def ref_rms_i16(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    xf = x.astype(np.float64)
    return float(np.sqrt(np.mean(xf * xf)))


def ref_level(rms: float, floor: float, full: float) -> float:
    if rms <= floor or not full > floor:
        return 0.0
    return float(min(1.0, (rms - floor) / (full - floor)))


def ref_compress_silence(arr: np.ndarray, frame_len: int, max_samp: int, thr: float) -> np.ndarray:
    out = []
    silent_acc = 0
    for i in range(0, arr.size, frame_len):
        chunk = arr[i : i + frame_len]
        if np.sqrt(np.mean(chunk**2) + 1e-12) < thr:
            silent_acc += chunk.size
            if silent_acc <= max_samp:
                out.append(chunk)
        else:
            silent_acc = 0
            out.append(chunk)
    return np.concatenate(out) if out else arr


# ── backend resolution ──────────────────────────────────────────────────────


def test_backend_info_shape():
    info = native.backend_info()
    assert info["backend"] in ("python", "rust")
    assert "error" in info and "numpy" in info and "python" in info
    if info["backend"] == "rust":
        assert info["native"]["module"] == "jarvis_native"
        assert "version" in info["native"]


def test_kill_switch_forces_fallback(monkeypatch):
    monkeypatch.setenv("JARVIS_DISABLE_NATIVE", "1")
    native.reload_backend()
    try:
        assert native.backend_name() == "python"
        assert "JARVIS_DISABLE_NATIVE" in (native.backend_info()["error"] or "")
    finally:
        native.reload_backend()


# ── fallback semantics ──────────────────────────────────────────────────────


def test_rms_auto_int16_bytes_and_array(forced_fallback):
    arr = np.array([3, -4, 5, -6], dtype=np.int16)
    expected = ref_rms_i16(arr)
    assert math.isclose(forced_fallback.rms_auto(arr), expected, rel_tol=1e-12)
    assert math.isclose(forced_fallback.rms_auto(arr.tobytes()), expected, rel_tol=1e-12)
    # 2-D sounddevice block shape
    block = np.array([[3], [-4], [5], [-6]], dtype=np.int16)
    assert math.isclose(forced_fallback.rms_auto(block), expected, rel_tol=1e-12)
    assert forced_fallback.rms_auto(np.zeros(0, dtype=np.int16)) == 0.0


def test_rms_auto_float_domain(forced_fallback):
    assert math.isclose(forced_fallback.rms_auto(np.array([0.5, -0.5], np.float32)), 0.5, rel_tol=1e-6)


def test_level_auto_mapping_matches_pcm_level(forced_fallback):
    floor, full = 60.0, 2600.0
    silence = np.full(64, 30, dtype=np.int16)
    assert forced_fallback.level_auto(silence, floor, full) == 0.0
    loud = np.full(64, 5000, dtype=np.int16)
    assert forced_fallback.level_auto(loud, floor, full) == 1.0
    # a block whose RMS sits exactly mid-window → ~0.5
    mid = np.full(64, int(60 + (2600 - 60) / 2), dtype=np.int16)
    assert math.isclose(forced_fallback.level_auto(mid, floor, full), 0.5, abs_tol=0.01)
    assert forced_fallback.level_auto(np.zeros(0, dtype=np.int16), floor, full) == 0.0


def test_auto_dispatch_rejects_unsupported_dtype(forced_fallback):
    with pytest.raises(TypeError):
        forced_fallback.rms_auto(np.array([1, 2, 3], dtype=np.int32))


def test_envelope_frames(forced_fallback):
    arr = np.arange(1, 11, dtype=np.int16)
    env = forced_fallback.envelope_auto(arr, 4)
    assert env.dtype == np.float32
    assert env.size == 3
    assert math.isclose(float(env[0]), ref_rms_i16(arr[:4]), rel_tol=1e-6)
    assert math.isclose(float(env[2]), ref_rms_i16(arr[8:10]), rel_tol=1e-6)
    # float input follows the float domain
    fenv = forced_fallback.envelope_auto(np.array([0.5] * 8, np.float32), 4)
    assert np.allclose(fenv, 0.5, atol=1e-6)


def test_compress_silence_matches_reference(forced_fallback):
    rng = np.random.default_rng(7)
    arr = np.concatenate(
        [
            (rng.standard_normal(240) * 0.2).astype(np.float32),   # loud
            np.zeros(240 * 4, dtype=np.float32),                   # 4 silent frames
            (rng.standard_normal(240) * 0.2).astype(np.float32),   # loud again
            np.zeros(240, dtype=np.float32),
        ]
    )
    out = forced_fallback.compress_silence_f32(arr, 240, 480, 0.003)
    ref = ref_compress_silence(arr, 240, 480, 0.003)
    assert np.array_equal(out, ref)
    # nothing silent → identical length
    loud = np.full(480, 0.5, dtype=np.float32)
    assert forced_fallback.compress_silence_f32(loud, 240, 480, 0.003).size == 480


def test_format_conversions(forced_fallback):
    arr = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)
    f = forced_fallback.i16_to_f32(arr)
    assert f.dtype == np.float32
    assert math.isclose(float(f[4]), 32767 / 32768, rel_tol=1e-6)
    back = forced_fallback.f32_to_i16(np.array([-2.0, -1.0, 0.0, 1.0, 2.0], dtype=np.float32))
    assert back.dtype == np.int16
    assert back.tolist() == [-32768, -32767, 0, 32767, 32767]
    # gain and fade mutate in place
    x = np.full(8, 1.0, dtype=np.float32)
    forced_fallback.gain_f32(x, 0.5)
    assert np.allclose(x, 0.5)
    forced_fallback.fade_f32(x, 4, False)
    assert x[-1] == 0.0 and x[0] == pytest.approx(0.5)


def test_pcm_ring_fallback(forced_fallback):
    r = forced_fallback.PcmRing(4, 1)
    assert r.push(np.array([1, 2, 3], dtype=np.int16)) == 3
    assert r.available_frames() == 3
    assert r.push(np.array([4, 5, 6, 7], dtype=np.int16)) == 1  # overflow → partial
    got = r.pop()
    assert got.tolist() == [1, 2, 3, 4]
    assert r.available_frames() == 0
    # underrun is counted only for explicit over-reads
    assert r.pop(8).size == 0
    assert r.stats()["underruns"] == 1
    assert r.pop().size == 0
    assert r.stats()["underruns"] == 1
    # wraparound order and clear()
    assert r.push(np.array([9, 10, 11, 12], dtype=np.int16)) == 4
    r.clear()
    assert r.available_frames() == 0
    r.push(np.array([13, 14], dtype=np.int16))
    assert r.pop(1).tolist() == [13]
    r.finish()
    assert not r.drained()
    assert r.pop().tolist() == [14]
    assert r.drained()


def test_pcm_ring_fallback_stereo_and_pop_into(forced_fallback):
    r = forced_fallback.PcmRing(4, 2)
    assert r.push(np.array([1, -1, 2, -2, 3, -3], dtype=np.int16)) == 3
    out = np.zeros(4, dtype=np.int16)
    got = r.pop_into(out)
    assert got == 2
    assert out.tolist() == [1, -1, 2, -2]


def test_streaming_resampler_fallback_lengths(forced_fallback):
    rs = forced_fallback.StreamingResampler(22050, 24000, 1, 128, "poly")
    sig = np.sin(np.linspace(0, 100, 22050) * 2 * np.pi).astype(np.float32)
    pieces = []
    for chunk in np.array_split(sig, 7):
        pieces.append(rs.process(chunk))
    out = np.concatenate(pieces)
    assert abs(out.size - 24000) < 64, out.size
    rs.reset()
    assert rs.process(np.zeros(0, np.float32)).size == 0


def test_resample_all_fallback(forced_fallback):
    sig = np.sin(np.linspace(0, 50, 2400) * 2 * np.pi).astype(np.float32)
    up = forced_fallback.resample_all(sig, 24000, 48000, 1)
    assert abs(up.size - 4800) <= 2
    same = forced_fallback.resample_all(sig, 24000, 24000, 1)
    assert np.array_equal(same, sig)
    with pytest.raises(ValueError):
        forced_fallback.resample_all(sig, 0, 48000, 1)


# ── Rust parity (specification checked against the native code) ─────────────


@needs_native
def test_native_rms_level_parity():
    jn = _jn()
    rng = np.random.default_rng(11)
    arr = rng.integers(-32768, 32767, 4097).astype(np.int16)
    assert math.isclose(jn.rms_i16(arr), ref_rms_i16(arr), rel_tol=1e-9)
    rms = ref_rms_i16(arr)
    assert math.isclose(
        jn.level_i16(arr, 60.0, 2600.0), ref_level(rms, 60.0, 2600.0), rel_tol=1e-9
    )
    f = rng.standard_normal(4096).astype(np.float32)
    assert math.isclose(jn.rms_f32(f), ref_rms_i16(f), rel_tol=1e-6)


@needs_native
def test_native_envelope_and_compress_parity():
    jn = _jn()
    rng = np.random.default_rng(3)
    arr = rng.integers(-2000, 2000, 1000).astype(np.int16)
    env = jn.envelope_i16(arr, 240)
    ref = [ref_rms_i16(arr[i : i + 240]) for i in range(0, 1000, 240)]
    assert np.allclose(env, np.array(ref, np.float32), rtol=1e-5, atol=1e-3)

    arrf = np.concatenate(
        [
            (rng.standard_normal(240) * 0.2).astype(np.float32),
            np.zeros(240 * 5, dtype=np.float32),
            (rng.standard_normal(500) * 0.2).astype(np.float32),
        ]
    )
    out = jn.compress_silence_f32(arrf, 240, 480, 0.003)
    ref_out = ref_compress_silence(arrf, 240, 480, 0.003)
    assert np.array_equal(out, ref_out)


@needs_native
def test_native_ring_parity_and_instant_clear():
    jn = _jn()
    r = jn.PcmRing(8, 1)
    assert r.push(np.arange(1, 6, dtype=np.int16)) == 5
    assert r.available_frames() == 5
    assert r.push(np.arange(6, 15, dtype=np.int16)) == 3  # partial overflow
    assert r.pop().tolist() == [1, 2, 3, 4, 5, 6, 7, 8]
    r.push(np.arange(1, 5, dtype=np.int16))
    r.clear()  # instant cancellation primitive
    assert r.available_frames() == 0
    r.finish()
    assert r.drained()


@needs_native
def test_native_resample_parity_length_and_identity():
    jn = _jn()
    sig = np.sin(np.linspace(0, 50, 2400) * 2 * np.pi).astype(np.float32)
    up = jn.resample_all(sig, 24000, 48000, 1)
    assert abs(up.size - 4800) <= 4, up.size
    ident = jn.resample_all(sig, 24000, 24000, 1)
    assert np.array_equal(ident, sig)
    # streaming API accepts arbitrary chunk sizes
    rs = jn.StreamingResampler(22050, 24000, 1, 128, "poly")
    total = 0
    for chunk in np.array_split(sig, 5):
        total += rs.process(chunk).size
    total += rs.flush().size
    # 2400 * 24000/22050 ≈ 2612; flush pads the final short chunk to one
    # full resampler chunk, so allow up to ~one chunk of trailing frames.
    assert abs(total - 2612) < 260, total


@needs_native
def test_auto_dispatch_routes_to_rust():
    native.reload_backend()
    if native.backend_name() != "rust":
        pytest.skip("kill-switch or load failure in this process")
    arr = np.array([3, -4, 5, -6], dtype=np.int16)
    assert math.isclose(native.rms_auto(arr), ref_rms_i16(arr), rel_tol=1e-9)
    r = native.PcmRing(4, 1)  # class proxies resolve through __getattr__
    assert r.push(arr) == 4


# ── the build helper itself ─────────────────────────────────────────────────


def test_build_tool_runs_stdlib_only():
    tool = ROOT / "tools" / "build_native.py"
    assert tool.is_file()
    proc = subprocess.run(
        [sys.executable, str(tool), "--check"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=180,
    )
    assert proc.returncode in (0, 3), proc.stdout + proc.stderr
    assert "Native module" in proc.stdout or "native module" in proc.stdout
