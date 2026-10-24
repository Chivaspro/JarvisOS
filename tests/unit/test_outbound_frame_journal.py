"""A dropped Live session must say which outbound frame preceded it.

Measured on 2026-10-24: the Live service closed the socket with 1007
"Request contains an invalid argument" and never named the message it refused,
so diagnosing it meant guessing between mic audio, vision frames and control
turns. The journal records the last outbound frames (kind, size, age) at the
two send entry points; these tests pin the recording, the wrapper's
pass-through, and the error-path print.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def main():
    return pytest.importorskip("main")


def _host(main):
    # __new__ skips the heavy JarvisLive __init__; the journal methods only
    # touch the state they create themselves.
    return main.JarvisLive.__new__(main.JarvisLive)


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def send_realtime_input(self, **kwargs):
        self.calls.append(("realtime", kwargs))

    async def send_client_content(self, **kwargs):
        self.calls.append(("content", kwargs))


def test_audio_frames_record_their_size_and_mime(main):
    text = main.JarvisLive._describe_outbound(
        (), {"audio": {"data": b"\x00" * 3200,
                       "mime_type": "audio/pcm;rate=16000"}})
    assert "audio=3200B" in text
    assert "audio/pcm;rate=16000" in text


def test_vision_frames_record_their_size(main):
    text = main.JarvisLive._describe_outbound(
        (), {"video": {"data": b"\x00" * 640,
                       "mime_type": "image/jpeg"}})
    assert "video=640B" in text


def test_control_turns_record_their_shape(main):
    text = main.JarvisLive._describe_outbound(
        (), {"turns": {"parts": [{"text": "hi"}]}, "turn_complete": True})
    assert "turns=dict[1]" in text
    assert "turn_complete=True" in text


def test_wrapping_records_and_passes_through(main):
    host = _host(main)
    session = _FakeSession()
    host._track_outbound(session)

    payload = b"x" * 8
    asyncio.run(session.send_realtime_input(audio={"data": payload}))
    asyncio.run(session.send_client_content(turns={"parts": []},
                                            turn_complete=True))

    # The original methods still ran with the same arguments.
    assert [kind for kind, _ in session.calls] == ["realtime", "content"]
    assert session.calls[0][1]["audio"]["data"] == payload

    line = host._outbound_journal_text()
    assert "send_realtime_input" in line
    assert "audio=8B" in line
    assert "send_client_content" in line
    # The calling function is recorded at call time, so a future drop names
    # its origin without cross-referencing the timeline.
    assert "[test_wrapping_records_and_passes_through]" in line


def test_wrapping_twice_does_not_double_record(main):
    host = _host(main)
    session = _FakeSession()
    host._track_outbound(session)
    first = session.send_realtime_input
    host._track_outbound(session)
    assert session.send_realtime_input is first

    asyncio.run(session.send_realtime_input(audio={"data": b"x"}))
    assert len(host._out_journal) == 1


def test_journal_is_bounded(main):
    host = _host(main)
    session = _FakeSession()
    host._track_outbound(session)
    for i in range(main.JarvisLive._OUT_JOURNAL_SIZE + 4):
        asyncio.run(session.send_realtime_input(audio={"data": b"x" * i}))
    assert len(host._out_journal) == main.JarvisLive._OUT_JOURNAL_SIZE


def test_connect_journals_and_the_error_handler_prints_it():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "self._track_outbound(session)" in source, (
        "every connect must wrap its session for the journal")
    after_err = source[source.index("err_str = str(e)"):][:700]
    assert "_outbound_journal_text()" in after_err, (
        "a Live drop must print the frames that preceded it")
