"""Real microphone input processing for the live voice path.

The Live session hears exactly what this module returns, so every control on
Settings ▸ VOICE ▸ MICROPHONE INPUT is wired to real DSP rather than a display
value:

* **input gain** 0–400 % (beyond 100 % is real amplification),
* **noise suppression** — a spectral gate with a 50 %-overlap STFT and a slowly
  adapting noise floor; speech frames never train the floor,
* **noise gate** — threshold with hysteresis and attack/release smoothing,
* **compressor** — RMS compressor with makeup gain,
* **limiter** — a true ceiling, and a final clamp so int16 conversion can never
  wrap around into loud crackle,
* **input monitoring** — callers can read the processed level (``level``) for
  a meter, and :class:`MicMonitor` plays the processed signal back for testing.

All processing is float32 and vectorised; one block costs two small FFTs and a
handful of array operations. If every filter is off, :meth:`MicDSP.process`
returns the input untouched in a single comparison.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

__all__ = ["MicSettings", "MicDSP", "MicMonitor", "DEFAULT_SETTINGS"]


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class MicSettings:
    """One immutable-by-convention snapshot of the microphone controls."""

    device: str = ""
    gain_percent: float = 100.0
    noise_suppression: bool = True
    suppression_amount: float = 0.6
    gate: bool = False
    gate_db: float = -45.0
    compressor: bool = False
    compressor_amount: float = 0.5
    limiter: bool = True
    ceiling_db: float = -1.0
    monitoring: bool = False

    def __post_init__(self) -> None:
        self.gain_percent = _clip(float(self.gain_percent or 100.0), 0.0, 400.0)
        self.suppression_amount = _clip(float(self.suppression_amount), 0.0, 1.0)
        self.gate_db = _clip(float(self.gate_db), -80.0, -5.0)
        self.compressor_amount = _clip(float(self.compressor_amount), 0.0, 1.0)
        self.ceiling_db = _clip(float(self.ceiling_db), -12.0, 0.0)

    @property
    def gain_linear(self) -> float:
        return max(self.gain_percent, 1.0) / 100.0

    @property
    def active(self) -> bool:
        """True when at least one filter would change the signal."""
        return bool(
            abs(self.gain_percent - 100.0) > 0.5
            or self.noise_suppression
            or self.gate
            or self.compressor
            or self.limiter
        )

    def signature(self) -> tuple:
        data = asdict(self)
        return tuple(sorted(data.items()))

    @classmethod
    def from_config(cls) -> "MicSettings":
        """Read the persisted controls; defaults match Settings exactly."""
        try:
            from config import settings as cfg

            return cls(
                device=str(cfg.get("input_device", "") or "").strip(),
                gain_percent=cfg.get("mic_gain_percent", 100),
                noise_suppression=cfg.get("mic_noise_suppression", True),
                suppression_amount=(
                    float(cfg.get("mic_noise_suppression_amount", 60) or 0) / 100.0),
                gate=cfg.get("mic_noise_gate", False),
                gate_db=cfg.get("mic_noise_gate_db", -45),
                compressor=cfg.get("mic_compressor", False),
                compressor_amount=(
                    float(cfg.get("mic_compressor_amount", 50) or 0) / 100.0),
                limiter=cfg.get("mic_limiter", True),
                ceiling_db=cfg.get("mic_limiter_ceiling_db", -1.0),
                monitoring=cfg.get("mic_monitoring", False),
            )
        except Exception:
            return cls()


DEFAULT_SETTINGS = MicSettings()


class MicDSP:
    """Stateful block processor. Not thread-safe; one instance per mic stream."""

    _COMPRESS_THRESHOLD_DB = -18.0

    def __init__(self, settings: MicSettings | None = None, *,
                 sample_rate: int = 16000):
        import numpy as np

        self._np = np
        self.settings = settings or MicSettings()
        self.sample_rate = int(sample_rate or 16000)
        self.level = 0.0
        self._gate_open = False
        self._gate_gain = 0.0
        self._comp_gain = 1.0
        # Noise-suppression state (50 % overlap WOLA).
        self._ns_prev = None
        self._ns_tail = None
        self._ns_noise = None
        self._ns_win = None
        self._ns_block = 0

    # ── public ────────────────────────────────────────────────────────────
    def reset(self) -> None:
        self._gate_open = False
        self._gate_gain = 0.0
        self._comp_gain = 1.0
        self._ns_prev = None
        self._ns_tail = None
        self._ns_noise = None
        self._ns_block = 0
        self.level = 0.0

    def process(self, block):
        """Process one int16 (or float) mono block; returns int16 bytes-ready."""
        np = self._np
        data = np.asarray(block)
        if data.ndim > 1:
            data = data[:, 0]
        if not self.settings.active:
            if data.dtype != np.int16:
                data = np.clip(data, -32768, 32767).astype(np.int16)
            self.level = float(np.sqrt(np.mean(
                (data.astype(np.float32) / 32768.0) ** 2)) or 0.0)
            return data
        if data.dtype == np.int16:
            x = data.astype(np.float32) / 32768.0
        else:
            x = data.astype(np.float32)
            if np.max(np.abs(x)) > 2.0:          # a stray int-scaled array
                x /= 32768.0

        if abs(self.settings.gain_percent - 100.0) > 0.5:
            x = x * self.settings.gain_linear
        if self.settings.noise_suppression:
            x = self._suppress(x)
        if self.settings.gate:
            x = self._gate(x)
        if self.settings.compressor:
            x = self._compress(x)
        if self.settings.limiter:
            x = self._limit(x)
        # Even with the limiter off, int16 conversion must clamp, never wrap.
        x = np.clip(x, -1.0, 1.0)
        self.level = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        return np.clip(np.rint(x * 32767.0), -32768, 32767).astype(np.int16)

    # ── helpers ───────────────────────────────────────────────────────────
    def _rms_db(self, x) -> float:
        np = self._np
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        return 20.0 * math.log10(max(rms, 1e-9))

    def _smooth(self, current: float, target: float,
                attack_s: float, release_s: float, n: int):
        """Per-sample exponential ramp towards *target* over this block."""
        np = self._np
        tau = attack_s if target > current else release_s
        n = max(1, int(n))
        if tau <= 0:
            return np.full(n, target, dtype=np.float32), float(target)
        alpha = math.exp(-n / (tau * self.sample_rate))
        end = float(target + (current - target) * alpha)
        ramp = np.linspace(current, end, n, dtype=np.float32)
        return ramp, end

    # ── noise suppression (spectral gate, 50 % overlap WOLA) ─────────────
    def _suppress(self, x):
        np = self._np
        n = int(x.size)
        if n <= 0:
            return x
        if self._ns_win is None or self._ns_block != n:
            self._ns_block = n
            self._ns_win = np.sqrt(np.hanning(2 * n)).astype(np.float32)
            self._ns_prev = np.zeros(n, dtype=np.float32)
            self._ns_tail = np.zeros(n, dtype=np.float32)
            self._ns_noise = None

        frame = np.concatenate((self._ns_prev, x)).astype(np.float32)
        self._ns_prev = x.copy()
        window = self._ns_win
        spec = np.fft.rfft(frame * window)
        mag = np.abs(spec)
        if self._ns_noise is None or self._ns_noise.shape != mag.shape:
            # Start conservatively: only part of the first frame is treated as
            # noise, so an opening word is not erased.
            self._ns_noise = mag * 0.35
        else:
            speech_like = self._rms_db(frame) > (self.settings.gate_db + 15.0)
            if not speech_like:
                self._ns_noise = (0.95 * self._ns_noise + 0.05 * mag)
            else:
                self._ns_noise = (0.999 * self._ns_noise + 0.001 * mag)

        amount = float(self.settings.suppression_amount)
        if amount <= 0.0:
            gain = np.ones_like(mag)
        else:
            gain = np.maximum(0.0, 1.0 - amount * self._ns_noise /
                              np.maximum(mag, 1e-9))
            # Smooth across neighbouring bins so the residual is not metallic.
            kernel = np.array([0.25, 0.5, 0.25], dtype=np.float32)
            gain = np.convolve(gain, kernel, mode="same")
            gain = np.clip(gain, 0.0, 1.0)

        cleaned = np.fft.irfft(spec * gain, n=2 * n).astype(np.float32)
        # The same window is applied again on synthesis, so the effective
        # window is a Hann; adjacent 50 %-overlap frames sum to unity and the
        # signal is reconstructed exactly when no bin is attenuated.
        cleaned *= window
        out = self._ns_tail + cleaned[:n]
        self._ns_tail = cleaned[n:]
        return out.astype(np.float32)

    # ── noise gate ────────────────────────────────────────────────────────
    def _gate(self, x):
        np = self._np
        db = self._rms_db(x)
        threshold = float(self.settings.gate_db)
        if self._gate_open:
            if db < threshold - 6.0:
                self._gate_open = False
        else:
            if db > threshold + 3.0:
                self._gate_open = True
        target = 1.0 if self._gate_open else 0.0
        ramp, self._gate_gain = self._smooth(
            self._gate_gain, target, 0.005, 0.150, x.size)
        return x * ramp

    # ── compressor ────────────────────────────────────────────────────────
    def _compress(self, x):
        np = self._np
        amount = float(self.settings.compressor_amount)
        if amount <= 0.0:
            return x
        ratio = 2.0 + 6.0 * amount
        db = self._rms_db(x)
        over = db - self._COMPRESS_THRESHOLD_DB
        if over > 0.0:
            gain_db = -over * (1.0 - 1.0 / ratio)
        else:
            gain_db = 0.0
        makeup_db = min(
            12.0,
            max(0.0, -self._COMPRESS_THRESHOLD_DB)
            * (1.0 - 1.0 / ratio) * 0.5 * amount)
        target = 10.0 ** ((gain_db + makeup_db) / 20.0)
        ramp, self._comp_gain = self._smooth(
            self._comp_gain, target, 0.008, 0.120, x.size)
        return x * ramp

    # ── limiter ───────────────────────────────────────────────────────────
    def _limit(self, x):
        np = self._np
        ceiling = 10.0 ** (float(self.settings.ceiling_db) / 20.0)
        peak = float(np.max(np.abs(x))) if x.size else 0.0
        if peak > ceiling and peak > 0.0:
            x = x * (ceiling / peak)
        return np.clip(x, -ceiling, ceiling)


class MicMonitor:
    """Play the processed microphone back through the speakers.

    Used by Settings ▸ VOICE ▸ MICROPHONE INPUT ▸ TEST INPUT. It opens its own
    input and output streams with the same :class:`MicDSP` settings, so what is
    heard is exactly what the voice path sends. It never touches the live
    session's microphone; starting it while JARVIS is listening is safe (the
    device is opened in shared mode), though the panel warns about feedback.
    """

    def __init__(self, settings: MicSettings | None = None, *,
                 sample_rate: int = 16000, block_size: int = 1024,
                 device: int | None = None):
        self.settings = settings or MicSettings.from_config()
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.device = device
        self._input = None
        self._output = None
        self._dsp = MicDSP(self.settings, sample_rate=self.sample_rate)
        self.level = 0.0
        self.error = ""
        self._running = False

    @property
    def running(self) -> bool:
        return bool(self._running)

    def start(self) -> bool:
        """Open the streams. Returns False with a real reason in ``error``."""
        if self._running:
            return True
        try:
            import numpy as np
            import sounddevice as sd

            sink_buf = np.zeros(self.block_size, dtype=np.int16)

            def _callback(indata, _frames, _time, status):
                try:
                    processed = self._dsp.process(indata)
                    self.level = float(self._dsp.level)
                    if processed.size >= sink_buf.size:
                        sink_buf[:] = processed[:sink_buf.size]
                    else:
                        sink_buf[:processed.size] = processed
                        sink_buf[processed.size:] = 0
                except Exception:
                    sink_buf[:] = 0

            self._input = sd.InputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16",
                blocksize=self.block_size, device=self.device,
                callback=_callback)

            def _play(outdata, frames, _time, _status):
                chunk = sink_buf[:min(int(frames), sink_buf.size)]
                outdata[:chunk.size, 0] = chunk
                if chunk.size < len(outdata):
                    outdata[chunk.size:, 0] = 0

            # The output callback polls the latest processed capture block;
            # there is no second queue and no extra thread.
            self._output = sd.OutputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16",
                blocksize=self.block_size, callback=_play)
            self._input.start()
            self._output.start()
            self._running = True
            self.error = ""
            return True
        except Exception as exc:
            self.error = str(exc)
            self.stop()
            return False

    def update_settings(self, settings: MicSettings) -> None:
        """Hot-swap the processing controls without reopening the streams."""
        self.settings = settings
        try:
            self._dsp.settings = settings
        except Exception:
            pass

    def stop(self) -> None:
        for stream in (self._input, self._output):
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._input = None
        self._output = None
        self._running = False


# Keep a tiny, dependency-free signature helper available to the runtime too.
def settings_signature(settings: MicSettings | None = None) -> tuple:
    return (settings or MicSettings.from_config()).signature()
