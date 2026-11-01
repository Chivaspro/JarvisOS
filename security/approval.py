"""Central human-approval service.

One gate for every action that can change the user's machine, files, plugins or
automations.  A request is described in full (kind, title, question, affected
targets and the concrete change summary), posted to the interface, and — unless
the matching ``auto_approve_<kind>`` feature flag is on — blocks the calling
worker thread until the user presses Approve or Decline.

Design contract
---------------
* **Fail closed.**  With no interface to ask, the request is declined.  A
  declined or expired request can never be retried by the caller: it must build
  a new request.
* **Duplicate safe.**  Pending requests are keyed by a fingerprint of the
  action; a second identical request attaches to the existing decision instead
  of posting a second card.
* **Never on the UI thread.**  ``request()`` waits on a ``threading.Event``;
  callers are expected to be running off the Qt loop (tool dispatch and coding
  agents already do).
* **Terminal states are immutable.**  pending → approved / declined / expired /
  cancelled, once and only once.

The module keeps a process-wide singleton so the UI, the tool dispatcher and
the coding agents all observe the same pending set.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_TIMEOUT_SECONDS = 600.0
KINDS = ("coding", "file_change", "desktop", "plugin", "automation", "action")
_TERMINAL = ("approved", "declined", "expired", "cancelled")


def _kind(value: str) -> str:
    kind = str(value or "action").strip().lower().replace(" ", "_")
    return kind if kind in KINDS else "action"


@dataclass
class ApprovalRequest:
    """A complete, human-readable description of one pending action."""

    kind: str = "action"
    title: str = ""
    question: str = ""
    description: str = ""
    targets: list[str] = field(default_factory=list)
    changes: dict[str, str] = field(default_factory=dict)
    risk: str = "medium"
    source: str = ""
    request_id: str = ""
    created_at: float = 0.0
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = _kind(self.kind)
        self.title = str(self.title or "").strip()[:200] or "Approval required"
        self.question = str(self.question or "").strip()[:400]
        self.description = str(self.description or "").strip()[:4000]
        self.targets = [str(t) for t in (self.targets or []) if str(t).strip()][:80]
        self.changes = {
            str(k): str(v)[:1200] for k, v in (self.changes or {}).items()
        }
        self.risk = str(self.risk or "medium").strip().lower()
        self.source = str(self.source or "").strip()[:120]
        self.request_id = str(self.request_id or uuid.uuid4().hex[:10])
        if not self.created_at:
            self.created_at = time.time()
        if not isinstance(self.meta, dict):
            self.meta = {}

    def fingerprint(self) -> str:
        parts = [
            self.kind,
            self.title.lower(),
            self.question.lower(),
            self.description.lower(),
            *sorted(t.lower() for t in self.targets),
            "|".join(f"{k}={v}" for k, v in sorted(self.changes.items())),
        ]
        raw = "\x1f".join(parts)
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "title": self.title,
            "question": self.question,
            "description": self.description,
            "targets": list(self.targets),
            "changes": dict(self.changes),
            "risk": self.risk,
            "source": self.source,
            "created_at": self.created_at,
            "meta": dict(self.meta),
            "auto_approve": get_approval_service().auto_approve_enabled(self.kind),
        }


@dataclass
class ApprovalDecision:
    request_id: str
    approved: bool
    status: str
    kind: str = ""
    reason: str = ""
    auto: bool = False
    decided_at: float = field(default_factory=time.time)

    @property
    def declined(self) -> bool:
        return not self.approved

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "approved": self.approved,
            "status": self.status,
            "kind": self.kind,
            "reason": self.reason,
            "auto": self.auto,
            "decided_at": self.decided_at,
        }


class ApprovalDenied(RuntimeError):
    """Raised by strict callers when a required approval was not granted."""

    def __init__(self, decision: ApprovalDecision):
        self.decision = decision
        super().__init__(
            f"Approval {decision.status} for {decision.request_id}"
            + (f": {decision.reason}" if decision.reason else "")
        )


@dataclass
class _Pending:
    request: ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    decision: Optional[ApprovalDecision] = None
    shown: bool = False


class ApprovalService:
    """Thread-safe pending-request store + blocking decision gate."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._by_fingerprint: dict[str, str] = {}
        self._show: Optional[Callable[[dict], None]] = None
        self._log: Optional[Callable[[str], None]] = None
        self._open_panel: Optional[Callable[[], None]] = None

    # ── wiring ────────────────────────────────────────────────────────────
    def bind(self, show: Optional[Callable[[dict], None]] = None,
             log: Optional[Callable[[str], None]] = None,
             open_panel: Optional[Callable[[], None]] = None) -> None:
        """Wire the service to the interface. ``None`` clears a hook."""
        with self._lock:
            self._show = show
            self._log = log
            self._open_panel = open_panel

    def _log_line(self, message: str) -> None:
        cb = self._log
        if cb is None:
            return
        try:
            cb(message)
        except Exception:
            pass

    def auto_approve_enabled(self, kind: str) -> bool:
        try:
            from config.settings import get_feature
            return bool(get_feature(f"auto_approve_{_kind(kind)}", False))
        except Exception:
            return False

    def set_auto_approve(self, kind: str, enabled: bool) -> bool:
        """Persist the per-kind Auto-Approve flag; returns the stored value."""
        kind = _kind(kind)
        try:
            from config.settings import set_feature
            return bool(set_feature(f"auto_approve_{kind}", bool(enabled)))
        except Exception:
            return False

    # ── request lifecycle ────────────────────────────────────────────────
    def submit(self, request: ApprovalRequest) -> tuple[str, bool]:
        """Register a request and post its card once.

        Returns ``(request_id, already_pending)``.  Identical requests share one
        pending decision instead of stacking duplicate cards.
        """
        fingerprint = request.fingerprint()
        with self._lock:
            existing = self._by_fingerprint.get(fingerprint)
            if existing and existing in self._pending:
                return existing, True
            self._pending[request.request_id] = _Pending(request=request)
            self._by_fingerprint[fingerprint] = request.request_id
            pending = self._pending[request.request_id]
            show = self._show

        payload = request.as_dict()
        if show is None:
            self._resolve_locked(request.request_id, False, status="declined",
                                 reason="no interface available to approve the action")
            return request.request_id, False
        try:
            show(payload)
            pending.shown = True
        except Exception as exc:
            self._resolve_locked(request.request_id, False, status="declined",
                                 reason=f"approval card could not be shown: {exc}")
            return request.request_id, False
        self._log_line(f"SYS: Approval requested ({request.kind}) — {request.title}")
        return request.request_id, False

    def request(self, request: ApprovalRequest,
                timeout: Optional[float] = None,
                check_auto: bool = True) -> ApprovalDecision:
        """Post the request and block until a terminal decision exists."""
        if check_auto and self.auto_approve_enabled(request.kind):
            decision = ApprovalDecision(
                request_id=request.request_id, approved=True, status="approved",
                kind=request.kind, reason="auto-approve is enabled", auto=True)
            self._log_line(
                f"SYS: Auto-approved {request.kind} — {request.title}")
            return decision

        request_id, already_pending = self.submit(request)
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            # submit() resolved the request synchronously (fail-closed); the
            # decision is gone with it, so report the only safe outcome.
            return ApprovalDecision(
                request_id=request_id, approved=False, status="declined",
                kind=request.kind, reason="approval could not be requested")

        if already_pending:
            self._log_line(
                f"SYS: Reusing pending approval ({request.kind}) — {request.title}")

        wait_s = DEFAULT_TIMEOUT_SECONDS if timeout is None else max(0.0, float(timeout))
        got_answer = pending.event.wait(wait_s)

        with self._lock:
            decision = pending.decision
            if decision is None:
                decision = ApprovalDecision(
                    request_id=request_id, approved=False, status="expired",
                    kind=request.kind, reason="the approval request timed out")
                pending.decision = decision
            self._pending.pop(request_id, None)
            for fp, rid in list(self._by_fingerprint.items()):
                if rid == request_id:
                    self._by_fingerprint.pop(fp, None)

        if not got_answer:
            self._log_line(
                f"SYS: Approval expired ({request.kind}) — {request.title}")
        else:
            self._log_line(
                f"SYS: {decision.status.capitalize()} ({request.kind}) — {request.title}")
        return decision

    def _resolve_locked(self, request_id: str, accepted: bool, *,
                        status: str = "", reason: str = "",
                        auto: bool = False) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.decision is not None:
                return False
            if not status:
                status = "approved" if accepted else "declined"
            decision = ApprovalDecision(
                request_id=request_id, approved=bool(accepted), status=status,
                kind=pending.request.kind, reason=reason, auto=auto)
            pending.decision = decision
            for fp, rid in list(self._by_fingerprint.items()):
                if rid == request_id:
                    self._by_fingerprint.pop(fp, None)
        pending.event.set()
        return True

    def resolve(self, request_id: str, accepted: bool, *,
                reason: str = "", auto: bool = False) -> bool:
        """Record the user's decision. Returns False for unknown/terminal ids."""
        return self._resolve_locked(str(request_id or ""), accepted,
                                    reason=reason, auto=auto)

    def cancel(self, request_id: str, reason: str = "cancelled") -> bool:
        return self._resolve_locked(str(request_id or ""), False,
                                    status="cancelled", reason=reason)

    def cancel_all(self, reason: str = "application shutting down") -> int:
        with self._lock:
            ids = list(self._pending)
        cancelled = 0
        for request_id in ids:
            if self._resolve_locked(request_id, False, status="cancelled",
                                    reason=reason):
                cancelled += 1
        return cancelled

    # ── introspection ────────────────────────────────────────────────────
    def pending_ids(self, kind: str = "") -> list[str]:
        kind = _kind(kind) if kind else ""
        with self._lock:
            return [
                rid for rid, p in self._pending.items()
                if p.decision is None and (not kind or p.request.kind == kind)
            ]

    def pending_requests(self) -> list[dict]:
        with self._lock:
            return [
                p.request.as_dict() for p in self._pending.values()
                if p.decision is None
            ]

    def get(self, request_id: str) -> Optional[dict]:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return None
            data = pending.request.as_dict()
            data["status"] = (
                pending.decision.status if pending.decision else "pending")
            return data

    def reset_for_tests(self) -> None:
        with self._lock:
            self._pending.clear()
            self._by_fingerprint.clear()
            self._show = None
            self._log = None
            self._open_panel = None


_service = ApprovalService()


def get_approval_service() -> ApprovalService:
    return _service


def bind(show=None, log=None, open_panel=None) -> None:
    _service.bind(show=show, log=log, open_panel=open_panel)


def request(request: ApprovalRequest, timeout: Optional[float] = None,
            check_auto: bool = True) -> ApprovalDecision:
    return _service.request(request, timeout=timeout, check_auto=check_auto)


def resolve(request_id: str, accepted: bool) -> bool:
    return _service.resolve(request_id, accepted)


def cancel_all(reason: str = "application shutting down") -> int:
    return _service.cancel_all(reason)


def pending_ids(kind: str = "") -> list[str]:
    return _service.pending_ids(kind)
