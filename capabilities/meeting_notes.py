"""Meeting notes — local capture, local transcription, Markdown notes.

A meeting recording is a relationship with everyone in the room, so this module
is built around three promises:

* **The audio stays here.** Two WAV files are written under
  ``data/meetings/<id>``: the system-output track (labelled ``Them``) and the
  microphone (``You``). No audio is uploaded, streamed or sent to a model.
* **The transcript is made here.** ``faster-whisper`` runs on this CPU in int8;
  the optional package is imported only when a transcription actually starts.
  When it is absent the audio is kept and the result names the pip command.
* **A summary is grounded in a real transcript.** ``save_summary`` refuses when
  there is no transcript to summarise. The tool description tells the model —
  and the user — that the transcript text is sent to the active AI model when a
  summary is requested; the audio never is.

Recording starts only after the on-screen confirmation declared in the registry
(``gate="confirm"``). The module itself has no way to bypass that gate.
"""

from __future__ import annotations

import atexit
import json
import re
import secrets
import shutil
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["run", "status"]

_MODEL_CHOICES = ("tiny", "base", "small", "medium", "large-v3")
_DEFAULT_MODEL = "base"
_MAX_TITLE = 120
_MAX_SUMMARY = 20_000
_MAX_ACTION_ITEMS = 10_000
_MAX_TRANSCRIPT_PAGE = 500
_MEETING_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_DISCLOSURE = ("Audio is written only to this machine. A requested summary sends "
               "the transcript text to the active AI model; the audio itself "
               "never leaves this machine.")

_STATE_LOCK = threading.RLock()
_MODEL_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()
_ACTIVE: dict | None = None
_TRANSCRIBING: set[str] = set()
_MODEL_CACHE: dict[str, Any] = {}
_HOOK_INSTALLED = False


class MeetingError(Exception):
    """A real meeting failure with a sentence the model may repeat."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── configuration and paths ─────────────────────────────────────────────────

def _setting(key: str, default: Any = "") -> Any:
    try:
        from config.settings import get
        return get(key, default)
    except Exception:
        try:
            from memory.config_manager import load_api_keys
            return dict(load_api_keys() or {}).get(key, default)
        except Exception:
            return default


def _feature_on(name: str) -> bool:
    try:
        from config.settings import feature_enabled
        return bool(feature_enabled(name))
    except Exception:
        return False


def _model_name() -> str:
    value = str(_setting("meeting_notes_model", "") or "").strip().lower()
    if not value:
        try:
            from config.settings import get_feature
            value = str(get_feature("meeting_notes_model", _DEFAULT_MODEL)
                        or "").strip().lower()
        except Exception:
            value = ""
    return value if value in _MODEL_CHOICES else _DEFAULT_MODEL


def _meetings_root() -> Path:
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR) / "meetings"
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "meetings"


def _meeting_dir(meeting_id: Any) -> Path:
    text = str(meeting_id or "").strip()
    if not _MEETING_RE.match(text):
        raise MeetingError("bad_meeting_id",
                           "That is not a meeting id. Use an id returned by "
                           "start, list or status.")
    return _meetings_root() / text


def _new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_ids() -> list[str]:
    root = _meetings_root()
    if not root.exists():
        return []
    try:
        return sorted((entry.name for entry in root.iterdir()
                       if entry.is_dir() and _MEETING_RE.match(entry.name)),
                      reverse=True)
    except Exception:
        return []


def _latest_id() -> str:
    ids = _valid_ids()
    return ids[0] if ids else ""


def _meta_path(folder: Path) -> Path:
    return folder / "meta.json"


def _load_meta(folder: Path) -> dict:
    try:
        data = json.loads(_meta_path(folder).read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_meta(folder: Path, meta: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = _meta_path(folder)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(path)


def _update_meta(meeting_id: str, **fields: Any) -> dict:
    folder = _meeting_dir(meeting_id)
    meta = _load_meta(folder)
    meta.update(fields)
    _save_meta(folder, meta)
    return meta


def _fmt_time(seconds: Any) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        value = 0
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"


def _wav_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = int(handle.getnframes())
            rate = int(handle.getframerate()) or 1
            return {"rate": rate, "channels": int(handle.getnchannels()),
                    "frames": frames, "duration_s": round(frames / rate, 1)}
    except Exception:
        return {}


def _wav_has_audio(path: Path) -> bool:
    info = _wav_info(path)
    return bool(info.get("frames", 0) > 0)


# ── capture ─────────────────────────────────────────────────────────────────

def _pyaudiowpatch():
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
        return pyaudio
    except Exception:
        return None


def _transcriber_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def _capture_pyaudio(pyaudio, folder: Path) -> tuple[dict, dict]:
    """Open WASAPI loopback and the microphone on ONE PortAudio instance."""
    p = pyaudio.PyAudio()
    streams: list[Any] = []
    waves: list[Any] = []
    tracks: list[dict] = []
    warnings: list[str] = []
    lock = threading.Lock()
    state = {"error": ""}

    def open_track(name: str, speaker: str, info: dict, channels: int,
                   label: str) -> None:
        index = int(info.get("index"))
        rate = int(info.get("defaultSampleRate") or 48000)
        channels = max(1, min(int(channels), int(info.get("maxInputChannels") or 1)))
        path = folder / f"{name}.wav"
        handle = wave.open(str(path), "wb")
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)

        def callback(in_data, frame_count, time_info, status_flags):
            del frame_count, time_info, status_flags
            try:
                with lock:
                    handle.writeframes(in_data)
            except Exception as exc:
                state["error"] = f"{name} track write failed: {exc}"
            return (None, pyaudio.paContinue)

        stream = p.open(
            format=p.get_format_from_width(2), channels=channels, rate=rate,
            input=True, input_device_index=index, frames_per_buffer=1024,
            stream_callback=callback, start=False)
        streams.append(stream)
        waves.append(handle)
        tracks.append({"speaker": speaker, "file": path.name, "device": label,
                       "rate": rate, "channels": channels})

    try:
        try:
            loop = p.get_default_wasapi_loopback()
            open_track("system", "Them", loop,
                       2 if int(loop.get("maxInputChannels") or 2) >= 2 else 1,
                       "system output")
        except Exception as exc:
            warnings.append(f"system audio was not captured ({exc})")
        try:
            mic = p.get_default_input_device_info()
            try:
                open_track("mic", "You", mic, 1, "microphone")
            except Exception:
                open_track("mic", "You", mic,
                           min(2, int(mic.get("maxInputChannels") or 2)),
                           "microphone")
        except Exception as exc:
            warnings.append(f"the microphone was not captured ({exc})")
        if not streams:
            raise MeetingError(
                "audio_unavailable",
                "No audio device could be opened. Check that a microphone and an "
                "output device are connected, then try again.")
        for stream in streams:
            stream.start_stream()
        return ({"backend": "pyaudiowpatch", "tracks": tracks,
                 "warnings": warnings},
                {"pyaudio": p, "streams": streams, "waves": waves,
                 "lock": lock, "state": state})
    except Exception:
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
        for handle in waves:
            try:
                handle.close()
            except Exception:
                pass
        try:
            p.terminate()
        except Exception:
            pass
        raise


def _capture_sounddevice(folder: Path) -> tuple[dict, dict]:
    """Microphone-only fallback when PyAudioWPatch is not installed."""
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        raise MeetingError(
            "audio_unavailable",
            "Meeting capture needs PyAudioWPatch for system audio and the "
            "microphone (or sounddevice for a microphone-only fallback). "
            "Neither package is installed. Run 'pip install PyAudioWPatch' and "
            "try again.") from exc
    try:
        info = sd.query_devices(kind="input")
        rate = int(float(info.get("default_samplerate") or 48000))
    except Exception:
        rate = 48000
    path = folder / "mic.wav"
    handle = wave.open(str(path), "wb")
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    lock = threading.Lock()
    state = {"error": ""}

    def callback(indata, frames, time_info, status_flags):
        del frames, time_info, status_flags
        try:
            with lock:
                handle.writeframes(indata.tobytes())
        except Exception as exc:
            state["error"] = f"microphone write failed: {exc}"

    try:
        stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                callback=callback)
        stream.start()
    except Exception as exc:
        try:
            handle.close()
        except Exception:
            pass
        raise MeetingError(
            "audio_unavailable",
            f"The microphone could not be opened ({exc}). Nothing was "
            "recorded.") from exc
    return ({"backend": "sounddevice",
             "tracks": [{"speaker": "You", "file": path.name,
                         "device": "microphone", "rate": rate, "channels": 1}],
             "warnings": ["system audio was not captured: PyAudioWPatch is not "
                          "installed, so only the microphone was recorded"]},
            {"sounddevice": True, "streams": [stream], "waves": [handle],
             "lock": lock, "state": state})


def _start_capture(folder: Path) -> tuple[dict, dict]:
    pyaudio = _pyaudiowpatch()
    first_error = ""
    if pyaudio is not None:
        try:
            return _capture_pyaudio(pyaudio, folder)
        except MeetingError as exc:
            first_error = exc.detail
        except Exception as exc:
            first_error = f"PyAudioWPatch failed ({type(exc).__name__}: {exc})"
    try:
        return _capture_sounddevice(folder)
    except MeetingError as exc:
        if first_error:
            raise MeetingError(
                "audio_unavailable",
                f"{first_error} The microphone-only fallback also failed: "
                f"{exc.detail}") from exc
        raise
    except Exception as exc:
        detail = (f"Audio capture failed ({type(exc).__name__}: {exc}). "
                  "Nothing was recorded.")
        if first_error:
            detail = f"{first_error} {detail}"
        raise MeetingError("audio_unavailable", detail) from exc


def _stop_capture(runtime: dict | None) -> list[str]:
    errors: list[str] = []
    if not isinstance(runtime, dict):
        return errors
    for stream in runtime.get("streams") or []:
        for method in ("stop_stream", "stop"):
            try:
                getattr(stream, method)()
                break
            except Exception:
                continue
        try:
            stream.close()
        except Exception as exc:
            errors.append(f"a stream did not close cleanly ({exc})")
    for handle in runtime.get("waves") or []:
        try:
            handle.close()
        except Exception as exc:
            errors.append(f"a WAV file did not close cleanly ({exc})")
    pyaudio = runtime.get("pyaudio")
    if pyaudio is not None:
        try:
            pyaudio.terminate()
        except Exception as exc:
            errors.append(f"PortAudio did not terminate cleanly ({exc})")
    state = runtime.get("state") or {}
    if isinstance(state, dict) and state.get("error"):
        errors.append(str(state["error"]))
    return errors


# ── transcription ───────────────────────────────────────────────────────────

def _get_model():
    name = _model_name()
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(name)
        if cached is not None:
            return cached
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise MeetingError(
                "transcriber_missing",
                "Local transcription needs the optional faster-whisper package, "
                "which is not installed. Run 'pip install faster-whisper'. The "
                "audio has been kept.") from exc
        try:
            model = WhisperModel(name, device="cpu", compute_type="int8")
        except Exception as exc:
            raise MeetingError(
                "model_failed",
                f"The faster-whisper model '{name}' could not be loaded "
                f"({type(exc).__name__}: {exc}). The audio has been kept.") from exc
        _MODEL_CACHE[name] = model
        return model


def _write_transcript(folder: Path, meta: dict, segments: list[dict],
                      language: str, model_name: str) -> None:
    payload = {
        "meeting_id": meta.get("meeting_id", ""),
        "title": meta.get("title", ""),
        "model": model_name,
        "language": language,
        "created_at": _now(),
        "segments": segments,
    }
    target = folder / "transcript.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(target)
    lines = [f"# Transcript — {meta.get('title') or 'Meeting'}", "",
             f"Meeting id: {meta.get('meeting_id', '')}",
             f"Model: {model_name} (local, CPU int8)", ""]
    for segment in segments:
        lines.append(f"[{_fmt_time(segment.get('start'))}] "
                     f"{segment.get('speaker', '?')}: {segment.get('text', '')}")
    (folder / "transcript.md").write_text("\n".join(lines) + "\n",
                                          encoding="utf-8")


def _transcription_worker(meeting_id: str) -> None:
    folder = _meeting_dir(meeting_id)
    try:
        with _TRANSCRIBE_LOCK:
            meta = _update_meta(meeting_id, status="transcribing",
                                transcription_stage="loading_model",
                                transcription_error="")
            model_name = _model_name()
            model = _get_model()
            _update_meta(meeting_id, transcription_stage="transcribing")
            segments: list[dict] = []
            language = ""
            for track in meta.get("tracks") or []:
                path = folder / str(track.get("file") or "")
                if not path.exists() or not _wav_has_audio(path):
                    continue
                try:
                    result = model.transcribe(str(path), vad_filter=True,
                                              beam_size=1)
                    generator, info = result
                    if not language:
                        language = str(getattr(info, "language", "") or "")
                    for segment in generator:
                        text = str(getattr(segment, "text", "") or "").strip()
                        if not text:
                            continue
                        segments.append({
                            "start": round(float(getattr(segment, "start", 0.0)), 2),
                            "end": round(float(getattr(segment, "end", 0.0)), 2),
                            "speaker": str(track.get("speaker") or "?"),
                            "text": text,
                        })
                except Exception as exc:
                    raise MeetingError(
                        "transcription_failed",
                        f"The {track.get('speaker')} track could not be "
                        f"transcribed ({type(exc).__name__}: {exc}). The audio "
                        "has been kept.") from exc
            segments.sort(key=lambda item: (item["start"], item["speaker"]))
            _write_transcript(folder, meta, segments, language, model_name)
            chars = sum(len(item["text"]) for item in segments)
            _update_meta(
                meeting_id, status="ready", transcription_stage="done",
                transcription_error="", transcribed_at=_now(),
                transcript_segments=len(segments), transcript_chars=chars,
                language=language,
                transcription_note=("" if segments else
                                    "No speech was detected in the audio."))
    except MeetingError as exc:
        _update_meta(meeting_id, status="transcription_failed",
                     transcription_stage="failed",
                     transcription_error=exc.detail)
    except Exception as exc:
        _update_meta(meeting_id, status="transcription_failed",
                     transcription_stage="failed",
                     transcription_error=f"{type(exc).__name__}: {exc}")
    finally:
        with _STATE_LOCK:
            _TRANSCRIBING.discard(meeting_id)


def _start_transcription(meeting_id: str) -> bool:
    with _STATE_LOCK:
        if meeting_id in _TRANSCRIBING:
            return False
        _TRANSCRIBING.add(meeting_id)
    thread = threading.Thread(target=_transcription_worker, args=(meeting_id,),
                              daemon=True, name=f"meeting-transcribe-{meeting_id}")
    thread.start()
    return True


# ── shutdown ────────────────────────────────────────────────────────────────

def _finalize_on_shutdown() -> None:
    """Close the WAV headers if the app exits while a meeting is recording."""
    global _ACTIVE
    with _STATE_LOCK:
        active = _ACTIVE
        if not active or active.get("state") != "recording":
            return
        active["state"] = "stopping"
        runtime = active.pop("runtime", None)
        folder = active["folder"]
        meta = dict(active.get("meta") or {})
    errors = _stop_capture(runtime)
    meta.update(status="recorded", ended_at=_now(),
                duration_s=round(time.monotonic() - active.get("started_monotonic", 0), 1),
                capture_errors=errors + ["recording ended because the application closed"])
    try:
        _save_meta(folder, meta)
    except Exception:
        pass
    with _STATE_LOCK:
        _ACTIVE = None


def _install_shutdown_hook() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    _HOOK_INSTALLED = True
    try:
        from core import stability
        stability.register_shutdown(_finalize_on_shutdown, name="meeting-notes")
    except Exception:
        pass
    try:
        atexit.register(_finalize_on_shutdown)
    except Exception:
        pass


# ── result helpers and meeting selection ────────────────────────────────────

def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "meeting-notes", "detail": detail,
            "reason": "", "evidence": evidence}


def _fail(detail: str, reason: str = "meeting_error") -> dict:
    return {"ok": False, "controller": "meeting-notes", "detail": detail,
            "reason": reason, "evidence": {}}


def _pick(raw: Any = "") -> tuple[str, Path, dict]:
    text = str(raw or "").strip()
    if text:
        folder = _meeting_dir(text)
        if not folder.exists():
            raise MeetingError("not_found", f"No meeting has id {text}.")
        return text, folder, _load_meta(folder)
    with _STATE_LOCK:
        active = _ACTIVE
        if active:
            meeting_id = str(active.get("meeting_id") or "")
            folder = active.get("folder")
            if meeting_id and isinstance(folder, Path):
                return meeting_id, folder, _load_meta(folder)
    meeting_id = _latest_id()
    if not meeting_id:
        raise MeetingError("not_found", "No meeting has been recorded yet.")
    folder = _meeting_dir(meeting_id)
    return meeting_id, folder, _load_meta(folder)


def _summary_state(meta: dict) -> str:
    return "saved" if str(meta.get("summary") or "").strip() else "not saved"


def _transcript_segments(folder: Path) -> list[dict]:
    try:
        data = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
        segments = data.get("segments") if isinstance(data, dict) else None
        return [item for item in (segments or []) if isinstance(item, dict)]
    except Exception:
        return []


# ── actions ─────────────────────────────────────────────────────────────────

def _h_start(args: dict) -> dict:
    global _ACTIVE
    with _STATE_LOCK:
        if _ACTIVE is not None:
            return _fail("A meeting is already being recorded. Stop it before "
                         "starting another one.", "already_recording")
    title = str(args.get("meeting_title") or "").strip()[:_MAX_TITLE]
    if not title:
        title = "Meeting " + datetime.now().strftime("%Y-%m-%d %H:%M")
    meeting_id = _new_id()
    folder = _meeting_dir(meeting_id)
    folder.mkdir(parents=True, exist_ok=False)
    meta = {"meeting_id": meeting_id, "title": title, "status": "starting",
            "started_at": _now(), "ended_at": "", "duration_s": 0.0,
            "model": _model_name(), "tracks": [], "warnings": [],
            "capture_errors": [], "transcription_error": "", "summary": "",
            "action_items": "", "transcript_segments": 0, "transcript_chars": 0}
    _save_meta(folder, meta)
    try:
        info, runtime = _start_capture(folder)
    except MeetingError as exc:
        meta.update(status="failed", ended_at=_now(),
                    transcription_error=exc.detail)
        _save_meta(folder, meta)
        return _fail(exc.detail, exc.reason)
    except Exception as exc:
        detail = (f"Recording could not start ({type(exc).__name__}: {exc}). "
                  "Nothing was recorded.")
        meta.update(status="failed", ended_at=_now(),
                    transcription_error=detail)
        _save_meta(folder, meta)
        return _fail(detail, "audio_unavailable")
    meta.update(status="recording", backend=info.get("backend", ""),
                tracks=info.get("tracks") or [],
                warnings=info.get("warnings") or [])
    _save_meta(folder, meta)
    with _STATE_LOCK:
        _ACTIVE = {"meeting_id": meeting_id, "folder": folder,
                   "state": "recording", "runtime": runtime, "meta": meta,
                   "started_monotonic": time.monotonic()}
    _install_shutdown_hook()
    speakers = ", ".join(f"{track.get('speaker')} ({track.get('file')})"
                         for track in meta["tracks"])
    detail = (f"Recording '{title}' started (id {meeting_id}). Tracks: "
              f"{speakers or 'none'}. {_DISCLOSURE}")
    warnings = meta.get("warnings") or []
    if warnings:
        detail += " " + " ".join(str(item) for item in warnings) + "."
    return _ok(detail, meeting_id=meeting_id, title=title,
               backend=meta.get("backend", ""), tracks=meta["tracks"],
               warnings=warnings, path=str(folder), status="recording")


def _h_stop(args: dict) -> dict:
    global _ACTIVE
    del args
    with _STATE_LOCK:
        active = _ACTIVE
        if not active or active.get("state") != "recording":
            latest = _latest_id()
            if latest:
                meta = _load_meta(_meeting_dir(latest))
                state = str(meta.get("status") or "unknown")
                if state in ("transcribing", "ready", "recorded",
                             "transcription_failed"):
                    return _ok(
                        f"No meeting is being recorded. The most recent meeting "
                        f"({latest}) is {state}.", meeting_id=latest,
                        status=state)
            return _fail("No meeting is being recorded.", "not_recording")
        active["state"] = "stopping"
        runtime = active.pop("runtime", None)
        folder = active["folder"]
        meta = dict(active.get("meta") or {})
        meeting_id = str(active.get("meeting_id") or "")
        started = float(active.get("started_monotonic") or 0.0)

    errors = _stop_capture(runtime)
    elapsed = round(max(0.0, time.monotonic() - started), 1)
    tracks = []
    for track in meta.get("tracks") or []:
        item = dict(track)
        info = _wav_info(folder / str(item.get("file") or ""))
        item.update(info)
        tracks.append(item)
    has_audio = any(int(item.get("frames") or 0) > 0 for item in tracks)
    duration = max([float(item.get("duration_s") or 0) for item in tracks]
                   + [elapsed])
    meta.update(status="recorded" if has_audio else "recorded_empty",
                ended_at=_now(), duration_s=round(duration, 1),
                tracks=tracks, capture_errors=errors)
    _save_meta(folder, meta)
    with _STATE_LOCK:
        _ACTIVE = None

    detail = (f"Recording '{meta.get('title')}' stopped after "
              f"{_fmt_time(duration)} (id {meeting_id}). Audio is saved at "
              f"{folder}.")
    if not has_audio:
        detail += (" No audio frames were captured, so there is nothing to "
                   "transcribe.")
        return _ok(detail, meeting_id=meeting_id, status="recorded_empty",
                   duration_s=meta["duration_s"], tracks=tracks)
    if errors:
        detail += " Capture warnings: " + "; ".join(errors[:3]) + "."
    if not _transcriber_available():
        meta.update(status="recorded",
                    transcription_error=("faster-whisper is not installed, so "
                                         "no transcript was produced"))
        _save_meta(folder, meta)
        detail += (" faster-whisper is not installed, so the audio was kept and "
                   "no transcript was produced. Run 'pip install faster-whisper' "
                   "and ask me to transcribe it.")
        return _ok(detail, meeting_id=meeting_id, status="recorded",
                   duration_s=meta["duration_s"], tracks=tracks,
                   transcription="not_available")
    meta.update(status="transcribing", transcription_stage="queued",
                transcription_error="")
    _save_meta(folder, meta)
    started_ok = _start_transcription(meeting_id)
    detail += (" Local transcription has started in the background. Ask for the "
               "status or the transcript when it is ready." if started_ok else
               " Transcription was already running for this meeting.")
    return _ok(detail, meeting_id=meeting_id, status="transcribing",
               duration_s=meta["duration_s"], tracks=tracks,
               transcription="started" if started_ok else "already_running")


def _h_status(args: dict) -> dict:
    raw = args.get("meeting_id")
    if raw:
        meeting_id = str(raw).strip()
        folder = _meeting_dir(meeting_id)
        if not folder.exists():
            return _fail(f"No meeting has id {meeting_id}.", "not_found")
        meta = _load_meta(folder)
    else:
        with _STATE_LOCK:
            active = _ACTIVE
            if active:
                meeting_id = str(active.get("meeting_id") or "")
                folder = active["folder"]
                meta = _load_meta(folder)
            else:
                meeting_id = _latest_id()
                if not meeting_id:
                    return _ok("No meetings have been recorded yet.",
                               count=0, status="idle")
                folder = _meeting_dir(meeting_id)
                meta = _load_meta(folder)
    state = str(meta.get("status") or "unknown")
    detail = f"Meeting '{meta.get('title') or meeting_id}' ({meeting_id}) is {state}."
    with _STATE_LOCK:
        active = _ACTIVE
        if active and active.get("meeting_id") == meeting_id \
                and active.get("state") == "recording":
            elapsed = time.monotonic() - float(active.get("started_monotonic") or 0)
            detail += f" {_fmt_time(elapsed)} elapsed."
    if state == "transcribing":
        stage = str(meta.get("transcription_stage") or "working")
        detail += f" Local transcription stage: {stage}."
    if state == "ready":
        detail += (f" {int(meta.get('transcript_segments') or 0)} segment(s), "
                   f"{int(meta.get('transcript_chars') or 0)} character(s). "
                   f"Summary {_summary_state(meta)}.")
    if meta.get("transcription_note"):
        detail += " " + str(meta["transcription_note"])
    if meta.get("transcription_error"):
        detail += " " + str(meta["transcription_error"])
    if meta.get("warnings"):
        detail += " " + " ".join(str(item) for item in meta["warnings"]) + "."
    if meta.get("capture_errors"):
        detail += " Capture warnings: " + "; ".join(
            str(item) for item in meta["capture_errors"][:3]) + "."
    if not _feature_on("meeting_notes"):
        detail += (" Meeting notes is switched off in Settings, so a new "
                   "recording cannot start.")
    return _ok(detail, meeting_id=meeting_id, title=meta.get("title", ""),
               status=state, duration_s=meta.get("duration_s", 0),
               tracks=meta.get("tracks") or [],
               transcript_segments=int(meta.get("transcript_segments") or 0),
               summary_saved=bool(str(meta.get("summary") or "").strip()),
               model=meta.get("model") or _model_name(),
               feature_on=_feature_on("meeting_notes"))


def _h_list(args: dict) -> dict:
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(100, limit))
    ids = _valid_ids()[:limit]
    if not ids:
        return _ok("No meetings have been recorded yet.", count=0, meetings=[])
    lines = []
    meetings = []
    for meeting_id in ids:
        meta = _load_meta(_meeting_dir(meeting_id))
        item = {"meeting_id": meeting_id,
                "title": meta.get("title") or meeting_id,
                "status": meta.get("status") or "unknown",
                "started_at": meta.get("started_at") or "",
                "duration_s": meta.get("duration_s") or 0,
                "transcript_segments": int(meta.get("transcript_segments") or 0),
                "summary_saved": bool(str(meta.get("summary") or "").strip())}
        meetings.append(item)
        lines.append(f"{meeting_id} — {item['title']} — {item['status']} — "
                     f"{_fmt_time(item['duration_s'])} — "
                     f"{item['transcript_segments']} segment(s) — summary "
                     f"{'saved' if item['summary_saved'] else 'not saved'}")
    return _ok(f"{len(meetings)} meeting(s):\n" + "\n".join(lines),
               count=len(meetings), meetings=meetings)


def _h_transcript(args: dict) -> dict:
    meeting_id, folder, meta = _pick(args.get("meeting_id"))
    state = str(meta.get("status") or "unknown")
    segments = _transcript_segments(folder)
    if not segments:
        if state == "ready" and (folder / "transcript.json").exists():
            return _ok(f"The transcript of meeting {meeting_id} contains no "
                       "speech segments.", meeting_id=meeting_id, total=0,
                       offset=0, count=0, language=meta.get("language", ""))
        if state == "transcribing":
            return _fail(f"Meeting {meeting_id} is still being transcribed "
                         f"(stage: {meta.get('transcription_stage') or 'working'}). "
                         "Ask again when the status says ready.", "not_ready")
        if state in ("recorded", "recorded_empty"):
            return _fail(f"Meeting {meeting_id} has no transcript yet. Ask me to "
                         "transcribe it first.", "not_transcribed")
        if state == "transcription_failed":
            return _fail(f"Meeting {meeting_id} could not be transcribed: "
                         f"{meta.get('transcription_error') or 'unknown error'}",
                         "transcription_failed")
        return _fail(f"Meeting {meeting_id} has no transcript.", "not_ready")
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        count = int(args.get("count") or 200)
    except (TypeError, ValueError):
        count = 200
    count = max(1, min(_MAX_TRANSCRIPT_PAGE, count))
    page = segments[offset:offset + count]
    lines = [f"Transcript of '{meta.get('title') or meeting_id}' ({meeting_id}) — "
             f"{len(segments)} segment(s), showing {offset + 1 if page else offset}"
             f"-{offset + len(page)}:"]
    for segment in page:
        lines.append(f"[{_fmt_time(segment.get('start'))}] "
                     f"{segment.get('speaker', '?')}: {segment.get('text', '')}")
    return _ok("\n".join(lines), meeting_id=meeting_id,
               total=len(segments), offset=offset, count=len(page),
               language=meta.get("language", ""),
               model=meta.get("model") or _model_name())


def _h_save_summary(args: dict) -> dict:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise MeetingError("bad_summary", "A summary cannot be empty.")
    if len(summary) > _MAX_SUMMARY:
        summary = summary[:_MAX_SUMMARY].rstrip() + "\n… (truncated)"
    action_items = str(args.get("action_items") or "").strip()
    if len(action_items) > _MAX_ACTION_ITEMS:
        action_items = action_items[:_MAX_ACTION_ITEMS].rstrip()
    meeting_id, folder, meta = _pick(args.get("meeting_id"))
    segments = _transcript_segments(folder)
    if not segments:
        raise MeetingError(
            "no_transcript",
            "There is no transcript to ground a summary in. Read the transcript "
            "first; I will not save a summary that was not written from it.")
    items = [line.strip().lstrip("-*• ").strip()
             for line in action_items.splitlines()]
    items = [item for item in items if item][:50]
    meta.update(summary=summary, action_items="\n".join(items),
                summary_saved_at=_now())
    _save_meta(folder, meta)
    lines = [f"# {meta.get('title') or meeting_id}", "",
             f"**Date:** {meta.get('started_at') or ''}",
             f"**Meeting id:** {meeting_id}", "",
             "## Summary", "", summary, ""]
    if items:
        lines.extend(["## Action items", ""])
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    lines.extend(["## Transcript", "",
                  f"The full transcript is in `transcript.md` "
                  f"({len(segments)} segment(s)). The audio is in `system.wav` "
                  f"and `mic.wav` in the same folder.", ""])
    notes = folder / "notes.md"
    temporary = notes.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(notes)
    return _ok(f"Saved the summary for '{meta.get('title') or meeting_id}' "
               f"({meeting_id}) to {notes}. The audio stayed on this machine.",
               meeting_id=meeting_id, path=str(notes),
               action_items=len(items), summary_chars=len(summary))


def _h_transcribe(args: dict) -> dict:
    meeting_id, folder, meta = _pick(args.get("meeting_id"))
    with _STATE_LOCK:
        active = _ACTIVE
        if active and active.get("meeting_id") == meeting_id \
                and active.get("state") == "recording":
            raise MeetingError("still_recording",
                               "Stop the recording before transcribing it.")
        if meeting_id in _TRANSCRIBING:
            raise MeetingError("already_transcribing",
                               f"Meeting {meeting_id} is already being "
                               "transcribed.")
    force = bool(args.get("force"))
    if (folder / "transcript.json").exists() and not force:
        raise MeetingError(
            "already_transcribed",
            f"Meeting {meeting_id} already has a transcript. Pass force=true to "
            "redo it.")
    if not any(_wav_has_audio(folder / str(track.get("file") or ""))
               for track in meta.get("tracks") or []):
        raise MeetingError("no_audio",
                           f"Meeting {meeting_id} has no recorded audio to "
                           "transcribe.")
    if not _transcriber_available():
        raise MeetingError(
            "transcriber_missing",
            "Local transcription needs the optional faster-whisper package. Run "
            "'pip install faster-whisper'. The audio has been kept.")
    _update_meta(meeting_id, status="transcribing",
                 transcription_stage="queued", transcription_error="")
    started = _start_transcription(meeting_id)
    return _ok(f"Transcription of meeting {meeting_id} has started locally "
               f"with the '{_model_name()}' model. Ask for the status or the "
               "transcript when it is ready.", meeting_id=meeting_id,
               status="transcribing", started=started,
               model=_model_name())


def _h_delete(args: dict) -> dict:
    meeting_id = str(args.get("meeting_id") or "").strip()
    folder = _meeting_dir(meeting_id)
    with _STATE_LOCK:
        active = _ACTIVE
        if active and active.get("meeting_id") == meeting_id:
            raise MeetingError("still_recording",
                               "Stop the recording before deleting it.")
        if meeting_id in _TRANSCRIBING:
            raise MeetingError("still_transcribing",
                               "That meeting is still being transcribed. Wait for "
                               "it to finish before deleting it.")
    if not folder.exists():
        raise MeetingError("not_found", f"No meeting has id {meeting_id}.")
    meta = _load_meta(folder)
    try:
        shutil.rmtree(folder)
    except Exception as exc:
        raise MeetingError("delete_failed",
                           f"The meeting could not be deleted ({exc}).") from exc
    return _ok(f"Deleted meeting '{meta.get('title') or meeting_id}' "
               f"({meeting_id}), including its local audio, transcript and notes. "
               "Nothing else was touched.", meeting_id=meeting_id,
               title=meta.get("title", ""))


_HANDLERS = {
    "start": _h_start,
    "stop": _h_stop,
    "status": _h_status,
    "list": _h_list,
    "transcript": _h_transcript,
    "save_summary": _h_save_summary,
    "transcribe": _h_transcribe,
    "delete": _h_delete,
}


def status() -> dict:
    """Compact state for diagnostics (never opens an audio device)."""
    latest = _latest_id()
    meta = _load_meta(_meeting_dir(latest)) if latest else {}
    return {"available": _transcriber_available(),
            "recording": bool(_ACTIVE and _ACTIVE.get("state") == "recording"),
            "meeting_id": latest, "state": meta.get("status", "idle"),
            "model": _model_name(),
            "detail": (f"Meeting notes is {'recording' if _ACTIVE else 'idle'}; "
                       f"local transcription is "
                       f"{'available' if _transcriber_available() else 'not installed'}.")}


def run(action_name: str, args: dict) -> dict:
    """Run one meeting-notes action; never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip())
    if handler is None:
        return _fail(f"'{action_name}' is not a meeting-notes action. Valid "
                     f"actions: {', '.join(_HANDLERS)}.", "unknown_action")
    try:
        return handler(dict(args or {}))
    except MeetingError as exc:
        return _fail(exc.detail, exc.reason)
    except Exception as exc:
        return _fail(f"Meeting notes action failed ({type(exc).__name__}: {exc}). "
                     "Any recorded audio was kept.", "meeting_error")
