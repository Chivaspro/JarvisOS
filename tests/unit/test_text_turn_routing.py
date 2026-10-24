"""Text turns must ride the path the live session actually accepts.

Measured 2026-10-24 (AUDIT_REPORT.md section 12): the enhanced v1alpha +
proactive session closed with 1007 "Request contains an invalid argument"
after ANY clientContent turn - the startup greeting, the greeting without
turn_complete, a trivial "Hello there!", and a client turn on a warm session
alike. Realtime text input on the same session was accepted and answered in
under a second. ``_send_text_turn()`` is the one router every text turn uses.
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


class _FakeSession:
    def __init__(self):
        self.realtime = []
        self.content = []

    async def send_realtime_input(self, **kwargs):
        self.realtime.append(kwargs)

    async def send_client_content(self, **kwargs):
        self.content.append(kwargs)


def _host(main, version: str):
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.session = _FakeSession()
    host._live_version = version
    return host


def test_v1alpha_text_goes_out_as_realtime_input(main):
    host = _host(main, "v1alpha")
    asyncio.run(host._send_text_turn("hello"))
    assert host.session.realtime == [{"text": "hello"}]
    assert host.session.content == []


def test_v1beta_text_keeps_the_deterministic_client_turn(main):
    host = _host(main, "v1beta")
    asyncio.run(host._send_text_turn("hello"))
    assert host.session.content == [
        {"turns": {"parts": [{"text": "hello"}]}, "turn_complete": True}]
    assert host.session.realtime == []


def test_empty_text_is_not_sent(main):
    host = _host(main, "v1alpha")
    asyncio.run(host._send_text_turn(""))
    assert host.session.realtime == []
    assert host.session.content == []


def test_unknown_version_defaults_to_client_turn(main):
    # A session whose version was never recorded keeps the old behaviour.
    host = _host(main, "")
    asyncio.run(host._send_text_turn("hi", turn_complete=False))
    assert host.session.content == [
        {"turns": {"parts": [{"text": "hi"}]}, "turn_complete": False}]


def test_every_text_site_uses_the_router():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for call in ("_send_text_turn(instruction)", "_send_text_turn(text)",
                 "_send_text_turn(alert)", "_send_text_turn(msg)",
                 "_send_text_turn(prompt)", "_send_text_turn(notice + suffix)"):
        assert call in source, f"{call} must route through _send_text_turn"
    assert "self._live_version = _version" in source, (
        "the connected session must record which API it runs on")
    greeting = source[source.index("Routed through _send_text_turn"):][:900]
    assert "_send_text_turn(" in greeting
    assert "send_client_content(" not in greeting, (
        "the greeting must not go back to a raw client turn")
