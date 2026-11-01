# `native/` — the Rust half of J.A.R.V.I.S. OS

An **optional** PyO3 extension (`jarvis_native`) that accelerates the audio
hot paths. Python stays the main application language: this crate never owns
devices, threads or app state — it only does per-sample DSP that the
pure-Python build does with numpy.

**The app runs identically without it.** `core/native.py` resolves the backend
lazily and silently falls back to pure numpy; every function here mirrors an
existing Python helper 1:1 (parity-tested in `tests/unit/test_native_backend.py`
and the crate's own `cargo test`).

## What's in it

| Module | Contents | Replaces |
|---|---|---|
| `src/pcm.rs` | RMS / peak / HUD level (int16 + f32), per-frame envelope, silence compaction, gain, fades, i16↔f32 conversion | `main._pcm_level`, `main._MicVoiceGate._rms`, `voice.wake._rms`, `voice.tts._push_reactive`, `voice.tts._ReactiveEnvelope`, `voice.tts._compress_silence` |
| `src/resample.rs` | One-shot + streaming sample-rate conversion (rubato 5; FFT one-shot, cubic-poly async stream) | (new capability — the Python build opens a device per rate) |
| `src/ring.rs` | Fixed-capacity int16 ring buffer with instant `clear()` and underrun stats | (new capability for playback scheduling) |

No cpal: the existing sounddevice/PortAudio architecture owns every device —
a second audio system is explicitly out of scope.

## Build (development)

```text
python tools/build_native.py            # release build → native/jarvis_native.pyd
python tools/build_native.py --check    # toolchain / artifact / import status
```

or from the setup wizard: **[2] Install & update ▸ Native Rust backend**
(and `python setup_jarvis.py --native` / `--native-status`).

### Toolchain prerequisites (Windows)

| Piece | Why | Install (no admin needed) |
|---|---|---|
| rustup + cargo | the compiler | `winget install Rustlang.Rustup` |
| MSVC **or** GNU linker | linking the DLL | VS Build Tools C++ workload, **or** `rustup toolchain install stable-x86_64-pc-windows-gnu` |
| `dlltool` (binutils) | GNU path needs it for Rust raw-dylib Python linking | `winget install BrechtSanders.WinLibs.POSIX.UCRT` |

`tools/build_native.py` probes the default toolchain first (MSVC) and falls
back to the GNU one, discovering `dlltool` in WinLibs/MSYS2/Git installs.

### Wheels (release packaging)

```text
pip install maturin
maturin build --release        # in this directory → target/wheels/*.whl (abi3-py310)
```

End users of a prebuilt release need **none** of the above — the `.pyd` ships
inside the package (the PyInstaller build bundles it automatically when
present).

## Runtime contract

* `JARVIS_DISABLE_NATIVE=1` — force the pure-Python backend (kill switch).
* `core.native.backend_info()` — backend, version, target triple, profile, or
  the real load error when it fell back.
* Startup prints one banner line: which backend is active, and why not.
* Benchmarks: `python tools/bench_native.py` (measured on this machine —
  regex/level ~4–8×, envelope ~12×, silence compaction ~40×, resampling ~2×).

## Testing

```text
cargo +stable-x86_64-pc-windows-gnu test --no-default-features --target x86_64-pc-windows-gnu
python -m pytest tests/unit/test_native_backend.py
```

The pytest file pins the fallback formulas and checks the Rust code against
literal numpy references (skipped automatically when the extension isn't built).
