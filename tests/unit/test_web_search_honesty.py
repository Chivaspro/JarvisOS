"""Search must distinguish "no results" from "the backend never answered".

Before this fix an unreachable backend produced the same "No results found for:"
sentence as a genuine empty result set — a fabricated-looking success that hid a
failure — and current-information answers carried no retrieval timestamp. These
tests pin the new contract: a backend failure is named as such, a real empty set
says so, and every answer is stamped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import web_search as ws  # noqa: E402


def _raises(_q):
    raise RuntimeError("429 quota")


# ── the DDG helpers report failure instead of an empty list ──────────────────

def test_a_dead_ddg_text_backend_reports_an_error(monkeypatch):
    class _Boom:
        def __enter__(self):
            raise RuntimeError("network down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ws, "_get_ddgs", lambda: _Boom)
    results, error = ws._ddg_search("anything")
    assert results == [] and error and "DuckDuckGo" in error


def test_a_working_ddg_text_backend_reports_no_error(monkeypatch):
    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, max_results=6):
            return [{"title": "T", "body": "B", "href": "http://x"}]

    monkeypatch.setattr(ws, "_get_ddgs", lambda: _DDGS)
    results, error = ws._ddg_search("q")
    assert error == "" and results and results[0]["url"] == "http://x"


def test_an_empty_but_answered_search_has_no_error(monkeypatch):
    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, max_results=6):
            return []

    monkeypatch.setattr(ws, "_get_ddgs", lambda: _DDGS)
    results, error = ws._ddg_search("q")
    assert results == [] and error == ""


# ── the formatters must not claim "no results" for a failure ─────────────────

def test_format_ddg_reports_a_backend_failure_distinctly():
    text = ws._format_ddg("cobalt price", [],
                          "DuckDuckGo text search failed (Timeout)")
    assert "No results found" not in text
    assert "could not reach" in text and "backend failure" in text
    assert "Retrieved:" in text


def test_format_ddg_reports_a_real_empty_result_set_with_a_timestamp():
    text = ws._format_ddg("cobalt price", [])
    assert text.startswith("No results found")
    assert "retrieved" in text.lower()


def test_format_ddg_keeps_sources_and_adds_a_timestamp():
    text = ws._format_ddg("q", [{"title": "T", "snippet": "S", "url": "http://x"}])
    assert "http://x" in text and "Retrieved:" in text


def test_format_news_reports_a_backend_failure_distinctly():
    text = ws._format_news("news", [],
                           "DuckDuckGo news search failed (RatelimitException)")
    assert "No news found" not in text
    assert "could not reach" in text


def test_format_news_reports_a_real_empty_result_set():
    text = ws._format_news("news", [])
    assert text.startswith("No news found") and "retrieved" in text.lower()


# ── the modes route the failure through ─────────────────────────────────────

def test_search_names_a_backend_failure(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", _raises)
    monkeypatch.setattr(ws, "_ddg_search",
                        lambda q, max_results=6:
                        ([], "DuckDuckGo text search failed (Timeout)"))
    text = ws._search("q")
    assert "could not reach" in text and "No results found" not in text


def test_research_and_price_name_a_backend_failure(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", _raises)
    monkeypatch.setattr(ws, "_ddg_search",
                        lambda q, max_results=6:
                        ([], "DuckDuckGo text search failed (Timeout)"))
    assert "could not reach" in ws._research("q")
    assert "could not reach" in ws._price("q")


def test_compare_reports_when_nothing_was_retrieved(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", _raises)
    monkeypatch.setattr(ws, "_ddg_search",
                        lambda q, max_results=3:
                        ([], "DuckDuckGo text search failed (Timeout)"))
    text = ws._compare(["a", "b"], "speed")
    assert "could not compare" in text and "Nothing was retrieved" in text


def test_compare_still_returns_results_when_a_backend_answers(monkeypatch):
    monkeypatch.setattr(ws, "_gemini_search", _raises)
    monkeypatch.setattr(ws, "_ddg_search",
                        lambda q, max_results=3:
                        ([{"snippet": "fast", "url": "http://x"}], ""))
    text = ws._compare(["a", "b"], "speed")
    assert "Comparison" in text and "http://x" in text


def test_news_reports_a_timeout_honestly(monkeypatch):
    # _run_bounded returns None when the worker overruns its budget.
    monkeypatch.setattr(ws, "_run_bounded", lambda fn, timeout, label="task": None)
    text = ws._news("ai")
    assert "could not reach" in text and "No news found" not in text


def test_news_reports_a_real_empty_result_honestly(monkeypatch):
    monkeypatch.setattr(ws, "_ddg_news", lambda q, max_results=8: ([], ""))
    monkeypatch.setattr(ws, "_gemini_search", _raises)
    text = ws._news("ai")
    assert text.startswith("No news found") and "retrieved" in text.lower()


def test_news_returns_formatted_results_when_ddg_answers(monkeypatch):
    monkeypatch.setattr(ws, "_ddg_news", lambda q, max_results=8: (
        [{"title": "Big story", "snippet": "s", "url": "http://n", "source": "X"}], ""))
    text = ws._news("ai")
    assert text.startswith("Latest news") and "http://n" in text
