"""Meeting notes: local audio, local transcript, confirmation-gated start.

A recording is a promise to everyone in the room, so these tests pin the
promises rather than the audio codec:

* ``start`` goes through the HUD confirmation before a single frame is
  captured — the module itself has no path around that gate;
* the disclosure says both halves plainly: audio stays local, a requested
  summary sends transcript TEXT to the active model;
* audio files are real WAVs that survive stop; a missing faster-whisper names
  its pip command and keeps the audio;
* a summary is refused unless a real transcript exists to ground it;
* delete removes one meeting folder and needs a valid meeting id.

No microphone and no transcriber: capture is a fake that writes deterministic
WAV files, and ``faster_whisper`` is reported as absent.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from capabilities import actions, meeting_notes, registry


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Fake capture into a scratch meetings folder; no audio device is opened."""
    root = tmp_path / "meetings"

    def fake_capture(folder: Path):
        _write_wav(folder / "system.wav")
        _write_wav(folder / "mic.wav")
        return ({"backend": "fake",
                 "tracks": [
                     {"speaker": "Them", "file": "system.wav", "device": "fake",
                      "rate": 16000, "channels": 1},
                     {"speaker": "You", "file": "mic.wav", "device": "fake",
                      "rate": 16000, "channels": 1}],
                 "warnings": []},
                {"streams": [], "waves": [], "state": {}})

    monkeypatch.setattr(meeting_notes, "_meetings_root", lambda: root)
    monkeypatch.setattr(meeting_notes, "_start_capture", fake_capture)
    monkeypatch.setattr(meeting_notes, "_transcriber_available", lambda: False)
    return root


def _record(sandbox) -> tuple[str, Path]:
    result = meeting_notes.run("start", {"meeting_title": "Planning"})
    assert result["ok"], result["detail"]
    meeting_id = result["evidence"]["meeting_id"]
    return meeting_id, sandbox / meeting_id


def _make_ready(folder: Path, meeting_id: str, segments: list[dict]) -> None:
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    meta.update(status="ready", transcript_segments=len(segments),
                transcript_chars=sum(len(s["text"]) for s in segments),
                language="en")
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({
        "meeting_id": meeting_id, "title": "Planning", "model": "base",
        "language": "en", "segments": segments,
    }), encoding="utf-8")


# ── the confirmation gate comes first ────────────────────────────────────────

def test_start_is_parked_behind_the_hud_before_any_capture(sandbox, monkeypatch):
    from security import confirm

    parked: dict = {}

    def fake_request(key="", title="", detail="", run=None):
        parked.update({"key": key, "title": title, "detail": detail, "run": run})
        return "[CONFIRMATION_PENDING] waiting for the HUD"

    monkeypatch.setattr(confirm, "request", fake_request)
    out = actions._handle("meeting_notes", {"action": "start"},
                          enabled=lambda name: True)

    assert "CONFIRMATION_PENDING" in out
    assert not any(sandbox.glob("*")), "nothing may be recorded before CONFIRM"
    assert parked["key"] == "capability:meeting_notes"
    # Both halves of the disclosure are on the confirmation itself.
    assert "system audio and the microphone" in parked["detail"]
    assert "transcript text to the active AI model" in parked["detail"]
    assert "never leaves this machine" in parked["detail"]

    # The only path from here is the callable the HUD invokes on CONFIRM.
    parked["run"]()
    assert list(sandbox.glob("*")), "the confirmed run really starts recording"
    meeting_notes.run("stop", {})


def test_start_and_delete_are_declared_as_confirm_gates():
    assert registry.get("meeting_notes", "start").gate == "confirm"
    assert registry.get("meeting_notes", "delete").gate == "confirm"
    assert registry.get("meeting_notes", "stop").gate == ""


# ── record → stop → transcribe ───────────────────────────────────────────────

def test_a_recording_writes_real_audio_and_discloses_where_it_goes(sandbox):
    meeting_id, folder = _record(sandbox)
    result = meeting_notes.run("status", {})
    assert result["ok"] and "recording" in result["detail"]

    result = meeting_notes.run("stop", {})
    assert result["ok"], result["detail"]
    assert "faster-whisper is not installed" in result["detail"]
    assert "pip install faster-whisper" in result["detail"]
    assert "audio was kept" in result["detail"]

    for name in ("system.wav", "mic.wav"):
        with wave.open(str(folder / name), "rb") as handle:
            assert handle.getnframes() > 0
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "recorded"
    assert meta["duration_s"] > 0

    # The start disclosure names the AI-model step for summaries and the local
    # guarantee for the audio itself.
    started = meeting_notes.run("start", {"meeting_title": "Disclosure"})
    assert started["ok"]
    assert "transcript text to the active AI model" in started["detail"]
    meeting_notes.run("stop", {})


def test_a_second_start_while_recording_is_refused(sandbox):
    _record(sandbox)
    result = meeting_notes.run("start", {})
    assert result["ok"] is False
    assert result["reason"] == "already_recording"
    meeting_notes.run("stop", {})


def test_stop_without_a_recording_says_so(sandbox):
    result = meeting_notes.run("stop", {})
    assert result["ok"] is False
    assert result["reason"] == "not_recording"


def test_a_capture_failure_is_reported_and_leaves_no_active_state(sandbox,
                                                                 monkeypatch):
    def failing(folder):
        raise meeting_notes.MeetingError("audio_unavailable",
                                         "No audio device could be opened.")

    monkeypatch.setattr(meeting_notes, "_start_capture", failing)
    result = meeting_notes.run("start", {"meeting_title": "Doomed"})
    assert result["ok"] is False
    assert result["reason"] == "audio_unavailable"

    state = meeting_notes.status()
    assert state["recording"] is False
    # The failed attempt left an honest record rather than a phantom meeting.
    folders = list(sandbox.glob("*"))
    assert len(folders) == 1
    meta = json.loads((folders[0] / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert "No audio device could be opened." in meta["transcription_error"]


# ── transcript and summary ───────────────────────────────────────────────────

def test_a_summary_without_a_transcript_is_refused(sandbox):
    meeting_id, folder = _record(sandbox)
    meeting_notes.run("stop", {})

    result = meeting_notes.run("save_summary",
                               {"meeting_id": meeting_id,
                                "summary": "Something made up."})
    assert result["ok"] is False
    assert result["reason"] == "no_transcript"
    assert not (folder / "notes.md").exists()


def test_a_transcript_is_read_with_speaker_labels(sandbox):
    meeting_id, folder = _record(sandbox)
    meeting_notes.run("stop", {})
    _make_ready(folder, meeting_id, [
        {"start": 0.0, "end": 1.0, "speaker": "You", "text": "Hello everyone."},
        {"start": 1.0, "end": 2.0, "speaker": "Them", "text": "Morning."},
    ])

    result = meeting_notes.run("transcript", {"meeting_id": meeting_id})
    assert result["ok"], result["detail"]
    assert "[00:00:00] You: Hello everyone." in result["detail"]
    assert "[00:00:01] Them: Morning." in result["detail"]
    assert result["evidence"]["total"] == 2


def test_a_summary_grounded_in_a_transcript_writes_the_note(sandbox):
    meeting_id, folder = _record(sandbox)
    meeting_notes.run("stop", {})
    _make_ready(folder, meeting_id, [
        {"start": 0.0, "end": 1.0, "speaker": "You", "text": "Ship on Friday."},
    ])

    result = meeting_notes.run("save_summary", {
        "meeting_id": meeting_id,
        "summary": "The team agreed to ship on Friday.",
        "action_items": "- Finish the checklist\n- Tell support"})
    assert result["ok"], result["detail"]
    notes = (folder / "notes.md").read_text(encoding="utf-8")
    assert "The team agreed to ship on Friday." in notes
    assert "- Finish the checklist" in notes
    assert "- Tell support" in notes
    assert "transcript.md" in notes


def test_transcribe_without_faster_whisper_names_the_install(sandbox):
    meeting_id, folder = _record(sandbox)
    meeting_notes.run("stop", {})
    # stop already left the audio; ask for a retry explicitly.
    result = meeting_notes.run("transcribe", {"meeting_id": meeting_id})
    assert result["ok"] is False
    assert result["reason"] == "transcriber_missing"
    assert "pip install faster-whisper" in result["detail"]
    assert (folder / "mic.wav").exists()


def test_list_and_status_report_the_meeting(sandbox):
    meeting_id, folder = _record(sandbox)
    meeting_notes.run("stop", {})

    result = meeting_notes.run("list", {})
    assert result["ok"]
    assert meeting_id in result["detail"]
    assert result["evidence"]["meetings"][0]["title"] == "Planning"

    result = meeting_notes.run("status", {"meeting_id": meeting_id})
    assert result["ok"]
    assert result["evidence"]["status"] in ("recorded", "recorded_empty")


def test_status_works_with_the_switch_off_and_says_so(sandbox, monkeypatch):
    _record(sandbox)
    monkeypatch.setattr(meeting_notes, "_feature_on", lambda name: False)
    result = meeting_notes.run("status", {})
    assert result["ok"], "status must keep answering for a live recording"
    assert "switched off" in result["detail"]
    assert result["evidence"]["feature_on"] is False
    meeting_notes.run("stop", {})


# ── deletion ─────────────────────────────────────────────────────────────────

def test_delete_removes_one_meeting_folder_only(sandbox):
    meeting_id, folder = _record(sandbox)
    meeting_notes.run("stop", {})
    kept = sandbox.parent / "keep-me.txt"
    kept.write_text("unrelated", encoding="utf-8")

    result = meeting_notes.run("delete", {"meeting_id": meeting_id})
    assert result["ok"], result["detail"]
    assert not folder.exists()
    assert kept.exists()

    again = meeting_notes.run("delete", {"meeting_id": meeting_id})
    assert again["ok"] is False and again["reason"] == "not_found"


def test_delete_refuses_a_path_like_id(sandbox):
    result = meeting_notes.run("delete", {"meeting_id": "../everything"})
    assert result["ok"] is False
    assert result["reason"] == "bad_meeting_id"


def test_delete_refuses_while_still_recording(sandbox):
    meeting_id, folder = _record(sandbox)
    result = meeting_notes.run("delete", {"meeting_id": meeting_id})
    assert result["ok"] is False
    assert result["reason"] == "still_recording"
    assert folder.exists()
    meeting_notes.run("stop", {})


def test_an_unknown_action_lists_the_real_ones(sandbox):
    result = meeting_notes.run("record_everything", {})
    assert result["ok"] is False
    assert result["reason"] == "unknown_action"
    assert "save_summary" in result["detail"]
