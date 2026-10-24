"""One owner for "is JARVIS actually ready?".

The launch screen, the splash and the HUD all used to decide readiness for
themselves: the splash said ONLINE as soon as ``main.py`` was spawned, the
overlay said LISTENING as soon as a key was saved, and the HUD stayed on
INITIALISING until Gemini connected. These tests pin the single state machine
that replaces all three answers.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import startup_state as st  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    st.reset_for_tests()
    yield
    st.reset_for_tests()


def test_starts_in_booting():
    assert st.current() == (st.BOOTING, "")


def test_transition_notifies_subscribers_and_keeps_detail():
    seen: list[tuple[str, str]] = []
    unsubscribe = st.subscribe(lambda state, detail: seen.append((state, detail)))

    st.set_state(st.CONNECTING, "opening the Live session")

    assert st.current() == (st.CONNECTING, "opening the Live session")
    assert seen == [(st.CONNECTING, "opening the Live session")]

    unsubscribe()
    st.set_state(st.READY)
    assert seen == [(st.CONNECTING, "opening the Live session")]


def test_unknown_state_never_replaces_the_real_one():
    st.set_state(st.CONNECTING)
    st.set_state("SOMETHING ELSE", "ignored")
    assert st.current()[0] == st.CONNECTING


def test_a_late_boot_signal_cannot_clear_ready():
    """The original bug: a stale INITIALISING overwrote a real READY."""
    st.set_state(st.READY, "Gemini session is live")

    for late in (st.BOOTING, st.CLEANUP, st.STARTING_UI):
        st.set_state(late)

    assert st.current() == (st.READY, "Gemini session is live")
    assert st.snapshot()["ever_ready"] is True


def test_reconnect_after_ready_is_reported_as_reconnect():
    st.set_state(st.READY)
    st.set_state(st.CONNECTING, "connection dropped - retrying")
    snap = st.snapshot()
    assert snap["state"] == st.CONNECTING
    assert snap["ever_ready"] is True


def test_configure_writes_timestamped_state_and_event_lines(tmp_path):
    log = tmp_path / "logs" / "startup.log"
    st.configure(log)
    st.set_state(st.CONNECTING, "opening the Live session")
    st.event("PLUGIN_DISCOVERY_DONE", "12 plugins")

    text = log.read_text(encoding="utf-8")
    assert "STATE" in text and "CONNECTING" in text
    assert "opening the Live session" in text
    assert "EVENT" in text and "PLUGIN_DISCOVERY_DONE" in text
    # A timestamp on every line, so a slow stage can be found after the fact.
    assert text.splitlines()[0][:4].isdigit()


def test_logging_never_breaks_a_transition(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("not a directory", encoding="utf-8")

    st.configure(blocker / "startup.log")   # mkdir will fail
    st.set_state(st.ERROR, "boom")

    assert st.current() == (st.ERROR, "boom")


def test_watchdog_marks_a_stalled_connect_as_degraded():
    st.set_state(st.CONNECTING, "waiting for Gemini")
    st.arm_watchdog(0.05, detail="No connection yet - check the key and network.")

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and st.current()[0] != st.DEGRADED:
        time.sleep(0.02)

    assert st.current() == (
        st.DEGRADED, "No connection yet - check the key and network.")


def test_watchdog_does_not_overwrite_a_real_ready():
    st.set_state(st.CONNECTING)
    st.arm_watchdog(0.05)
    st.set_state(st.READY)

    time.sleep(0.15)

    assert st.current()[0] == st.READY


def test_watchdog_can_be_disabled_for_tests(monkeypatch):
    monkeypatch.setenv("JARVIS_STARTUP_WATCHDOG", "0")
    st.set_state(st.CONNECTING)
    st.arm_watchdog(0.05)
    time.sleep(0.12)
    assert st.current()[0] == st.CONNECTING
