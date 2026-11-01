#!/usr/bin/env python3
"""Repeatable micro-benchmarks: pure-Python/numpy hot paths vs the Rust backend.

Every number comes from this machine, this run — no estimates. The benchmark
sizes mirror the real call sites:

* 1024 int16 samples  = one 64 ms mic block (main.py callback, wake feed, gate)
* 2400 int16 samples  = one 100 ms streamed PCM chunk (_push_reactive)
* 24000 f32 samples   = 1 s of 24 kHz audio (reactive envelope, compaction)
* 24000 f32 samples   = 1 s 24 kHz → 48 kHz (resampling)
* 1024-frame ring     = one device block (push/pop scheduling)

Usage::

    python tools/bench_native.py            # all benchmarks
    python tools/bench_native.py --json     # machine-readable, for CI/records

Requires numpy; does NOT require the native extension (falls back with a note).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

# Windows consoles default to a legacy codepage where "→" / "µ" print as
# mojibake (or raise) — the same guard the other project tools use.
for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import native  # noqa: E402


def bench(label: str, fn, *, iters: int, warmup: int = 5) -> dict:
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    dt = (time.perf_counter() - t0) / iters
    return {"label": label, "us_per_op": dt * 1e6, "iters": iters}


def _old_pcm_level_rms(x_f32: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x_f32 * x_f32)))


def _old_push_reactive_rms(pcm: bytes) -> float:
    arr = np.frombuffer(pcm, dtype=np.int16)
    return float(np.sqrt(np.mean((arr.astype(np.float32) / 32768.0) ** 2)))


def _old_envelope_loop(arr: np.ndarray, frame: int) -> np.ndarray:
    out = []
    for i in range(0, arr.size, frame):
        chunk = arr[i : i + frame]
        out.append(float(np.sqrt(np.mean(chunk * chunk))))
    return np.asarray(out, dtype=np.float32)


def _old_compress_silence(arr: np.ndarray, frame_len: int, max_samp: int, thr: float):
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


def _old_resample_all(x: np.ndarray, frm: int, to: int) -> np.ndarray:
    n_out = int(round(x.size * to / frm))
    t_in = np.arange(x.size) / frm
    t_out = np.arange(n_out) / to
    return np.interp(t_out, t_in, x).astype(np.float32)


def main() -> int:
    as_json = "--json" in sys.argv
    rng = np.random.default_rng(42)

    rows: list[dict] = []

    # ── one-time cost: first resolution imports/loads the extension ─────
    # (must run before ANY other native call)
    t0 = time.perf_counter()
    info = native.backend_info()
    rows.append({"label": "backend load + backend_info() (one-time)",
                 "us_per_op": (time.perf_counter() - t0) * 1e6, "iters": 1})

    native_ok = info["backend"] == "rust"
    jn = None
    if native_ok:
        import jarvis_native as jn  # type: ignore

    # ── 64 ms mic block: RMS (gate/wake) ────────────────────────────────
    mic_i16 = rng.integers(-8000, 8000, 1024).astype(np.int16)
    mic_bytes = mic_i16.tobytes()
    mic_f32 = mic_i16.astype(np.float32)

    rows.append(bench("mic block 1024: old numpy (astype+mean)", lambda: _old_pcm_level_rms(mic_f32), iters=3000))
    rows.append(bench("mic block 1024: fallback backend rms", lambda: native._FALLBACK.rms_i16(mic_i16), iters=3000))
    if jn:
        rows.append(bench("mic block 1024: Rust rms_i16 (array)", lambda: jn.rms_i16(mic_i16), iters=3000))
        rows.append(bench("mic block 1024: Rust rms_auto (bytes)", lambda: native.rms_auto(mic_bytes), iters=3000))
        rows.append(bench("mic block 1024: Rust level_i16 (one call)", lambda: jn.level_i16(mic_i16, 60.0, 2600.0), iters=3000))

    # ── streamed PCM chunk: reactive RMS over bytes ─────────────────────
    pcm_chunk = rng.integers(-9000, 9000, 2400).astype(np.int16).tobytes()
    rows.append(bench("pcm chunk 2400: old numpy (bytes)", lambda: _old_push_reactive_rms(pcm_chunk), iters=3000))
    if jn:
        rows.append(bench("pcm chunk 2400: Rust rms_auto (bytes)", lambda: native.rms_auto(pcm_chunk), iters=3000))

    # ── 1 s audio: reactive envelope ────────────────────────────────────
    second_f32 = (rng.standard_normal(24_000) * 0.1).astype(np.float32)
    rows.append(bench("envelope 1 s (frame 960): old Python loop", lambda: _old_envelope_loop(second_f32, 960), iters=300))
    if jn:
        rows.append(bench("envelope 1 s (frame 960): Rust bulk", lambda: jn.envelope_f32(second_f32, 960), iters=300))

    # ── 2 s audio: silence compaction (Kokoro) ──────────────────────────
    two_s = np.concatenate([
        (rng.standard_normal(6_000) * 0.2).astype(np.float32),
        np.zeros(24_000, dtype=np.float32),
        (rng.standard_normal(18_000) * 0.2).astype(np.float32),
    ])
    rows.append(bench("compress_silence 2 s: old Python loop", lambda: _old_compress_silence(two_s, 240, 12_000, 0.003), iters=200))
    if jn:
        rows.append(bench("compress_silence 2 s: Rust", lambda: jn.compress_silence_f32(two_s, 240, 12_000, 0.003), iters=200))

    # ── 1 s: resample 24k → 48k ─────────────────────────────────────────
    rows.append(bench("resample 1 s 24k→48k: np.interp", lambda: _old_resample_all(second_f32, 24_000, 48_000), iters=30))
    if jn:
        rows.append(bench("resample 1 s 24k→48k: Rust FFT (rubato)", lambda: jn.resample_all(second_f32, 24_000, 48_000, 1), iters=30))

    # ── ring: push+pop of one 1024-frame device block ───────────────────
    block = rng.integers(-8000, 8000, 1024).astype(np.int16)

    def ring_cycle(cls):
        r = cls(2048, 1)
        def cycle():
            r.push(block)
            r.pop(1024)
        return cycle

    rows.append(bench("ring push+pop 1024: fallback", ring_cycle(native._FALLBACK.PcmRing), iters=3000))
    if jn:
        rows.append(bench("ring push+pop 1024: Rust", ring_cycle(jn.PcmRing), iters=3000))

    # ── report ──────────────────────────────────────────────────────────
    if as_json:
        print(json.dumps({"native": native_ok, "info": info, "rows": rows}, indent=2))
        return 0

    print(f"\nJ.A.R.V.I.S. native backend benchmark — backend: {info['backend']}")
    if not native_ok:
        print(f"  (native extension not active: {info.get('error')})")
    print(f"  python {info['python']} · numpy {info['numpy']} · "
          f"{info.get('native', {}).get('target', '')} "
          f"{info.get('native', {}).get('profile', '')}".rstrip())
    print()
    width = max(len(r["label"]) for r in rows) + 2
    print(f"{'benchmark'.ljust(width)} {'µs/op':>10}  {'ops/s':>9}")
    print("-" * (width + 22))
    for r in rows:
        print(f"{r['label'].ljust(width)} {r['us_per_op']:>10.1f}  {1e6 / r['us_per_op']:>9.0f}")
    print()

    # side-by-side speedups for the pairs that matter
    def find(label):
        return next((r["us_per_op"] for r in rows if r["label"] == label), None)

    pairs = [
        ("mic block 1024: old numpy (astype+mean)", "mic block 1024: Rust rms_auto (bytes)"),
        ("pcm chunk 2400: old numpy (bytes)", "pcm chunk 2400: Rust rms_auto (bytes)"),
        ("envelope 1 s (frame 960): old Python loop", "envelope 1 s (frame 960): Rust bulk"),
        ("compress_silence 2 s: old Python loop", "compress_silence 2 s: Rust"),
        ("resample 1 s 24k→48k: np.interp", "resample 1 s 24k→48k: Rust FFT (rubato)"),
        ("ring push+pop 1024: fallback", "ring push+pop 1024: Rust"),
    ]
    print("speedups (old → Rust):")
    for old, new in pairs:
        a, b = find(old), find(new)
        if a and b:
            print(f"  {new.split(':')[0].ljust(44)} {a / b:>5.1f}×  ({a:.1f} → {b:.1f} µs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
