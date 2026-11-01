"""Central approval service: blocking, dedupe, auto-approve, fail-closed."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security import approval  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_service():
    approval._service.reset_for_tests()
    yield
    approval._service.reset_for_tests()


def _wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _start(req, timeout=5.0):
    result = {}

    def _worker():
        result["decision"] = approval.request(req, timeout=timeout)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread, result


def test_no_interface_fails_closed():
    decision = approval.request(approval.ApprovalRequest(
        kind="coding", title="Build X", description="change a file"))
    assert decision.approved is False
    assert decision.status == "declined"
    assert "interface" in decision.reason


def test_approve_unblocks_the_waiter():
    shown = []
    approval.bind(show=lambda req: shown.append(req))
    thread, result = _start(approval.ApprovalRequest(
        kind="coding", title="Build X", description="detail"))

    assert _wait_for(lambda: bool(shown))
    request_id = shown[0]["request_id"]
    assert approval.resolve(request_id, True) is True
    thread.join(timeout=3)
    assert result["decision"].approved is True
    assert result["decision"].status == "approved"
    assert result["decision"].kind == "coding"


def test_decline_is_terminal_and_unblocks():
    shown = []
    approval.bind(show=shown.append)
    thread, result = _start(approval.ApprovalRequest(
        kind="file_change", title="Rewrite config", targets=["config/api_keys.json"]))
    assert _wait_for(lambda: bool(shown))
    request_id = shown[0]["request_id"]
    approval.resolve(request_id, False)
    thread.join(timeout=3)
    decision = result["decision"]
    assert decision.approved is False
    assert decision.status == "declined"
    assert approval.resolve(request_id, True) is False  # already terminal


def test_duplicate_pending_request_shares_one_card():
    shown = []
    approval.bind(show=shown.append)
    req = approval.ApprovalRequest(kind="desktop", title="Close window",
                                   targets=["notepad.exe"])
    first, first_result = _start(req)
    assert _wait_for(lambda: len(shown) == 1)
    second, second_result = _start(approval.ApprovalRequest(
        kind="desktop", title="Close window", targets=["notepad.exe"]))
    # No second card: the same fingerprint attaches to the pending decision.
    time.sleep(0.15)
    assert len(shown) == 1
    approval.resolve(shown[0]["request_id"], True)
    first.join(timeout=3)
    second.join(timeout=3)
    assert first_result["decision"].approved is True
    assert second_result["decision"].approved is True


def test_timeout_expires_and_declines():
    approval.bind(show=lambda req: None)
    started = time.monotonic()
    decision = approval.request(
        approval.ApprovalRequest(kind="action", title="Slow"), timeout=0.15)
    assert decision.approved is False
    assert decision.status == "expired"
    assert time.monotonic() - started < 2.0
    assert approval.pending_ids() == []


def test_auto_approve_skips_the_card(monkeypatch):
    monkeypatch.setattr(
        approval.ApprovalService, "auto_approve_enabled",
        lambda self, kind: kind == "coding")
    shown = []
    approval.bind(show=shown.append)
    decision = approval.request(approval.ApprovalRequest(
        kind="coding", title="Auto", description="x"))
    assert decision.approved is True
    assert decision.auto is True
    assert shown == []


def test_auto_approve_is_per_kind(monkeypatch):
    monkeypatch.setattr(
        approval.ApprovalService, "auto_approve_enabled",
        lambda self, kind: kind == "coding")
    shown = []
    approval.bind(show=shown.append)
    thread, result = _start(approval.ApprovalRequest(
        kind="desktop", title="Move mouse"), timeout=5)
    assert _wait_for(lambda: bool(shown))
    assert shown[0]["kind"] == "desktop"
    approval.resolve(shown[0]["request_id"], False)
    thread.join(timeout=3)
    assert result["decision"].approved is False


def test_set_auto_approve_persists_the_kind_flag(monkeypatch):
    saved = {}

    def _set_feature(name, value):
        saved[name] = value
        return True

    monkeypatch.setattr("config.settings.set_feature", _set_feature)
    assert approval._service.set_auto_approve("coding", True) is True
    assert saved == {"auto_approve_coding": True}


def test_cancel_all_releases_every_waiter():
    shown = []
    approval.bind(show=shown.append)
    thread, result = _start(approval.ApprovalRequest(
        kind="automation", title="Run workflow"), timeout=10)
    assert _wait_for(lambda: bool(shown))
    assert approval.cancel_all("test shutdown") == 1
    thread.join(timeout=3)
    assert result["decision"].status == "cancelled"
    assert result["decision"].approved is False


def test_request_payload_carries_targets_and_changes():
    shown = []
    approval.bind(show=shown.append)
    thread, result = _start(approval.ApprovalRequest(
        kind="file_change", title="Edit", targets=["a.py"],
        changes={"a.py": "replace function"}), timeout=5)
    assert _wait_for(lambda: bool(shown))
    payload = shown[0]
    assert payload["targets"] == ["a.py"]
    assert payload["changes"] == {"a.py": "replace function"}
    assert payload["kind"] == "file_change"
    approval.resolve(payload["request_id"], True)
    thread.join(timeout=3)


def test_show_failure_declines_instead_of_hanging():
    def _boom(_req):
        raise RuntimeError("no window")

    approval.bind(show=_boom)
    decision = approval.request(
        approval.ApprovalRequest(kind="action", title="Will fail"), timeout=2)
    assert decision.approved is False
    assert decision.status == "declined"
    assert "could not be shown" in decision.reason
