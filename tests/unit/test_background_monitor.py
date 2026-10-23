"""Background topic monitor — the once-a-day news check per topic.

The check treated ``_ddg_news``'s ``(results, error)`` tuple as a plain list,
so ``results[0]`` was the list itself and ``top.get(...)`` raised on every
check: the failure was swallowed, ``last_check`` was never recorded, and the
"alert me when there is news about X" feature could never fire a single alert.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import background_monitor as bm  # noqa: E402
from actions import web_search as ws  # noqa: E402


def _monitors(last_hash: str = "") -> dict:
    return {"ai_safety": {
        "topic": "AI safety",
        "added": "2026-01-01",
        "last_check": "",
        "last_hash": last_hash,
    }}


def _wire(monkeypatch, news):
    """In-memory monitor store + a scripted news backend."""
    store: dict = _monitors()
    monkeypatch.setattr(bm, "_load", lambda: store)
    monkeypatch.setattr(bm, "_save", lambda m: store.update(m))
    monkeypatch.setattr(ws, "_ddg_news", news)
    return store


def test_a_new_headline_fires_one_alert(monkeypatch):
    store = _wire(monkeypatch, lambda q, max_results=8: (
        [{"title": "Big story", "snippet": "s", "url": "http://n",
          "source": "Example News"}], ""))

    alerts = bm.check_all()

    assert len(alerts) == 1
    assert "[MONITOR_ALERT] AI safety" in alerts[0]
    assert "Headline: Big story" in alerts[0]
    assert "Source: Example News" in alerts[0]
    # The check is recorded so the topic is not re-checked today.
    assert store["ai_safety"]["last_check"]
    assert store["ai_safety"]["last_hash"] == bm._title_hash("Big story")


def test_a_backend_error_alerts_nothing_and_never_raises(monkeypatch):
    store = _wire(monkeypatch, lambda q, max_results=8: (
        [], "DuckDuckGo news search failed (RatelimitException)"))

    alerts = bm.check_all()

    assert alerts == []
    # A backend failure is still a completed check for the day — no hammering.
    assert store["ai_safety"]["last_check"]


def test_an_empty_result_set_alerts_nothing(monkeypatch):
    store = _wire(monkeypatch, lambda q, max_results=8: ([], ""))

    assert bm.check_all() == []
    assert store["ai_safety"]["last_check"]


def test_the_same_headline_does_not_alert_twice(monkeypatch):
    store = _wire(monkeypatch, lambda q, max_results=8: (
        [{"title": "Big story", "snippet": "s", "url": "http://n",
          "source": "X"}], ""))
    store["ai_safety"]["last_hash"] = bm._title_hash("Big story")

    assert bm.check_all() == []


def test_a_topic_already_checked_today_is_skipped(monkeypatch):
    def _explode(q, max_results=8):
        raise AssertionError("an already-checked topic must not be re-fetched")

    store = _wire(monkeypatch, _explode)
    store["ai_safety"]["last_check"] = datetime.now().strftime("%Y-%m-%d")

    assert bm.check_all() == []
