"""Realtime state truth: MUTED / IDLE / LISTENING, and no stale interim text.

Three failures this pins:

* the reactor said LISTENING while the microphone was muted (and while no Live
  session existed at all) because every call site asked only "is the mic
  muted?" before hardcoding "LISTENING" — the new ``_idle_state()`` is the one
  place that answers with the truth;
* turning Settings ▸ Realtime STT OFF stopped UPDATING the live transcript but
  left the last interim fragment in the composer forever, where it read as an
  unsent command — the OFF transition now removes exactly the echo;
* Settings wrote a feature flag and the running process kept reading the
  previous parse until something else touched the file — ``clear_cache()`` is
  called by the single writer, so the next ``get_feature`` sees the switch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as jarvis_main  # noqa: E402
from config import settings as cfg_settings  # noqa: E402

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


class _StubUI:
    def __init__(self, muted=False):
        self.muted = muted
        self.transcripts: list[tuple[str, bool]] = []

    def set_live_transcript(self, text="", final=False):
        self.transcripts.append((str(text), bool(final)))


def _live(*, muted=False, session=True):
    live = jarvis_main.JarvisLive.__new__(jarvis_main.JarvisLive)
    live.ui = _StubUI(muted=muted)
    if session:
        live.session = object()
        live._loop = object()
    else:
        live.session = None
        live._loop = None
    return live


# ── _idle_state(): MUTED / IDLE / LISTENING ──────────────────────────────────

def test_a_muted_microphone_reports_muted_not_listening():
    live = _live(muted=True, session=True)

    assert live._idle_state() == "MUTED"


def test_no_session_reports_idle_not_listening():
    live = _live(muted=False, session=False)

    assert live._idle_state() == "IDLE"


def test_a_live_unmuted_session_reports_listening():
    live = _live(muted=False, session=True)

    assert live._idle_state() == "LISTENING"


def test_muted_beats_a_live_session():
    """Mic off + session up is still MUTED: nothing is being heard."""
    live = _live(muted=True, session=True)

    assert live._idle_state() != "LISTENING"


def test_the_muted_blind_listening_sites_are_gone():
    """Every resting-state call site goes through _idle_state()."""
    assert "if not self.ui.muted:" not in MAIN_SOURCE
    assert MAIN_SOURCE.count("self.ui.set_state(self._idle_state())") >= 4


# ── _realtime_stt_on(): the OFF transition clears the echo ───────────────────

def _stt(live, monkeypatch, enabled: bool):
    monkeypatch.setattr(jarvis_main, "_read_cached_json",
                        lambda path: {"features": {"realtime_stt": enabled}})
    live._rt_stt_checked = 0.0
    return live._realtime_stt_on()


def test_turning_realtime_stt_off_clears_the_stale_interim_text(monkeypatch):
    live = _live()
    live._rt_stt_state = True          # it was ON before this call

    state = _stt(live, monkeypatch, enabled=False)

    assert state is False
    assert live.ui.transcripts == [("", True)], \
        "the last interim echo must be removed, as a final close"


def test_turning_realtime_stt_on_does_not_clear_anything(monkeypatch):
    live = _live()
    live._rt_stt_state = False

    state = _stt(live, monkeypatch, enabled=True)

    assert state is True
    assert live.ui.transcripts == []


def test_no_clearing_when_the_setting_did_not_change(monkeypatch):
    live = _live()
    live._rt_stt_state = False         # already OFF; reading it again is a no-op

    state = _stt(live, monkeypatch, enabled=False)

    assert state is False
    assert live.ui.transcripts == []


def test_the_final_close_is_used_so_a_user_draft_survives():
    """set_live_transcript('', final=True) removes OUR echo by value, never a
    draft the user typed; a non-final empty call would do nothing at all."""
    source = (ROOT / "ui.py").read_text(encoding="utf-8")
    start = source.index("if final:")
    block = source[start:start + 700]
    assert "box.text().strip() in echo_log" in block


# ── config/settings.py: the writer invalidates the reader ────────────────────

def test_clear_cache_forces_a_reread(tmp_path, monkeypatch):
    config = tmp_path / "api_keys.json"
    config.write_text(json.dumps({"features": {"realtime_stt": True}}),
                      encoding="utf-8")
    monkeypatch.setattr(cfg_settings, "API_KEYS_FILE", config)

    assert cfg_settings.get_feature("realtime_stt") is True
    # Change the file behind the cache's back, then clear it explicitly.
    config.write_text(json.dumps({"features": {"realtime_stt": False}}),
                      encoding="utf-8")
    cfg_settings.clear_cache()
    assert cfg_settings.get_feature("realtime_stt") is False


def test_set_feature_invalidates_the_process_cache(tmp_path, monkeypatch):
    config = tmp_path / "api_keys.json"
    config.write_text(json.dumps({"features": {"realtime_stt": True}}),
                      encoding="utf-8")
    monkeypatch.setattr(cfg_settings, "API_KEYS_FILE", config)

    assert cfg_settings.get_feature("realtime_stt") is True
    assert cfg_settings.set_feature("realtime_stt", False) is True
    # The very next read in THIS process must see the switch it just threw.
    assert cfg_settings.get_feature("realtime_stt") is False


def test_clear_cache_is_safe_to_call_any_time():
    cfg_settings.clear_cache()
    cfg_settings.clear_cache()
    assert isinstance(cfg_settings.get_feature("realtime_stt", True), bool)
