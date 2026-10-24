"""Observability unit tests (tasks 2.1-2.4).

Proves: structured events carry every required field, secret-shaped values
never reach the log file (redaction at the logging boundary), per-subsystem
counters record latency/outcome, and the Langfuse adapter is a complete no-op
when unconfigured.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from observability.logger import log_event  # noqa: E402
from observability.metrics import record, counters_snapshot, reset  # noqa: E402
from observability.tracing import trace_span  # noqa: E402

REQUIRED_FIELDS = (
    "ts", "level", "component", "event", "task_id", "tool",
    "state", "duration_ms", "result", "error",
)

# A realistic, clearly-fake Gemini-shaped key. The redaction boundary can only
# be proven with a value that actually matches the scrubber; the previous
# ``os.getenv("SECRET_SHAPED", "")`` default made ``assert SECRET_SHAPED not in
# text`` vacuously true-but-failing (the empty string is in every string), so
# the security assertion never ran. An operator may still inject a real key.
SECRET_SHAPED = os.getenv("SECRET_SHAPED") or (
    "AIzaSy" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0"
)


def _last_log_record(tmp_path, monkeypatch):
    """Emit one event with the log file redirected into tmp_path; return it."""
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("INFO", "test", "unit-check", task_id="T-1", tool="probe",
              state="EXECUTING", duration_ms=12.3, result={"api_key": SECRET_SHAPED})
    logmod._fh.close()  # flush for Windows file readers
    logmod._fh = None
    lines = logmod._fh_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "structured event was not written"
    return json.loads(lines[-1])


def test_structured_event_has_all_required_fields(tmp_path, monkeypatch):
    rec = _last_log_record(tmp_path, monkeypatch)
    for field in REQUIRED_FIELDS:
        assert field in rec, f"missing required field: {field}"
    assert rec["task_id"] == "T-1" and rec["tool"] == "probe"
    assert rec["state"] == "EXECUTING" and rec["duration_ms"] == 12.3


def test_secrets_never_reach_the_log_file(tmp_path, monkeypatch):
    rec = _last_log_record(tmp_path, monkeypatch)
    assert rec["result"]["api_key"] == "[REDACTED]"
    assert SECRET_SHAPED not in logfile_text()


def test_secret_shaped_string_in_event_is_scrubbed(tmp_path, monkeypatch):
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("WARNING", "test", "leak attempt", error=f"failed with {SECRET_SHAPED}")
    logmod._fh.close()
    logmod._fh = None
    assert SECRET_SHAPED not in logfile_text()


def logfile_text() -> str:
    from observability import logger as logmod
    if logmod._fh_path and logmod._fh_path.exists():
        return logmod._fh_path.read_text(encoding="utf-8")
    return ""


@pytest.mark.parametrize("shape", [
    "AQ.Ab8dEfGhIjKlMnOpQrStUvWxYz0123456789",      # current Gemini
    "sk-or-v1-abcdefghijklmnopqrstuvwx",            # OpenRouter
    "sk-ant-api03-abcdefghijklmnopqrstuvwx",        # Anthropic
    "sk-fish-abcdefghijklmnopqrstuvwx",             # Fish Audio
    "gsk_abcdefghijklmnopqrstuvwx",                 # Groq
    "sk-proj-abcdefghijklmnopqrstuvwx",             # OpenAI project keys
])
def test_modern_key_shapes_are_scrubbed_from_free_text(shape, tmp_path, monkeypatch):
    """The original pattern missed every one of these: an exception string
    quoting them reached the JSONL log verbatim."""
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("WARNING", "test", f"Called upstream with {shape} in the header")
    logmod._fh.close()
    logmod._fh = None
    text = logfile_text()
    assert shape not in text, "the key shape reached the log verbatim"
    assert "[REDACTED]" in text


def test_a_key_echoed_into_a_tool_argument_is_scrubbed(tmp_path, monkeypatch):
    """main.py logs FULL tool arguments, and redact_for_log works on dict KEY
    names only — so a key pasted into chat and echoed by the model into an
    argument was persisted verbatim. String leaves are now scrubbed too."""
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("INFO", "agent", "tool_started",
              result={"activity": "CODING",
                      "arguments": {"note": f"please use {SECRET_SHAPED} for this"}})
    logmod._fh.close()
    logmod._fh = None
    text = logfile_text()
    assert SECRET_SHAPED not in text, "a key inside a tool argument was logged"
    assert "[REDACTED]" in text


def test_ordinary_words_are_not_over_redacted(tmp_path, monkeypatch):
    """The scrubber must not eat innocent text (e.g. parameter names)."""
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("INFO", "test", "tuning max_tokens and risk_score thresholds",
              result={"max_tokens": 1024})
    logmod._fh.close()
    logmod._fh = None
    text = logfile_text()
    assert "max_tokens" in text and "risk_score" in text


def test_counters_record_latency_and_outcomes():
    reset()
    record("computer", duration_ms=100.0, ok=True)
    record("computer", duration_ms=300.0, ok=False)
    record("model", duration_ms=50.0, ok=True)
    snap = counters_snapshot()
    assert snap["computer"]["calls"] == 2
    assert snap["computer"]["errors"] == 1
    assert snap["computer"]["avg_ms"] == 200.0
    assert snap["computer"]["max_ms"] == 300.0
    assert snap["model"]["calls"] == 1 and snap["model"]["errors"] == 0


def test_langfuse_unconfigured_is_noop(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    t0 = __import__("time").perf_counter()
    with trace_span("unit-test") as span:
        span.update(metadata={"api_key": SECRET_SHAPED})  # stub must accept calls
    elapsed_ms = (__import__("time").perf_counter() - t0) * 1000
    assert elapsed_ms < 500, "unconfigured tracing must not import or do network I/O"


def test_timed_context_logs_and_counts(tmp_path, monkeypatch):
    from observability import logger as logmod
    from observability import metrics as met
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    met.reset()
    with logmod.timed("computer", tool="unit-op"):
        pass
    with pytest.raises(ValueError):
        with logmod.timed("computer", tool="unit-fail"):
            raise ValueError("boom")
    snap = met.counters_snapshot()
    assert snap["computer"]["calls"] == 2 and snap["computer"]["errors"] == 1


def test_agent_tool_started_is_logged_after_the_activity_resolves():
    """The tool_started event once read _doing_category/_doing_detail BEFORE
    _activity_for_tool() assigned them: every call raised UnboundLocalError
    into a bare except, so every agent/tool_started record was silently
    missing from the JSONL log."""
    main_src = (Path(__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    fn_start = main_src.index("def _execute_tool(self, fc)")
    fn_end = main_src.index("async def _send_realtime", fn_start)
    body = main_src[fn_start:fn_end]
    resolve_at = body.index("_activity_for_tool(name, _activity_args)")
    log_at = body.index('"tool_started"')
    assert log_at > resolve_at, (
        "tool_started must be logged AFTER the activity resolution, or the "
        "activity fields are unbound when the event is built")
