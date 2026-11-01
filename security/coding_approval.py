"""Coding approval gate — compatibility wrapper over ``security.approval``.

Historically this module owned its own pending map.  The central approval
service now owns every kind of approval (coding, file changes, desktop actions,
plugins, automations), so this file is a thin, behaviour-preserving adapter:

* ``bind(show, log)`` keeps the old ``show(req_id, title, detail)`` signature.
* ``request_approval(title, detail)`` blocks until the user decides and returns
  a bool, exactly as before.
* ``auto_approve_enabled`` stays module-level so Settings/tests can patch it.

Downstream code (dev_agent, code_helper, file_processor) needs no changes.
"""

from __future__ import annotations

from typing import Optional

from security.approval import ApprovalRequest, get_approval_service

# Kept for callers/tests that import it.
TIMEOUT_SECONDS = 600.0

_service = get_approval_service()


def bind(show=None, log=None) -> None:
    """Wire the legacy three-argument card callback to the central service."""
    if show is None:
        _service.bind(show=None, log=log)
        return

    def _adapter(request: dict) -> None:
        show(request.get("request_id", ""), request.get("title", ""),
             request.get("description", ""))

    _service.bind(show=_adapter, log=log)


def auto_approve_enabled() -> bool:
    return _service.auto_approve_enabled("coding")


def request_approval(title: str, detail: str,
                     timeout: Optional[float] = None) -> bool:
    """Ask before coding. True only when the user approved (or auto-approve)."""
    if auto_approve_enabled():
        try:
            _service._log_line(f"SYS: Auto-approved coding — {title}")
        except Exception:
            pass
        return True
    decision = _service.request(
        ApprovalRequest(kind="coding", title=str(title), question=str(title),
                        description=str(detail)),
        timeout=TIMEOUT_SECONDS if timeout is None else timeout,
        check_auto=False,
    )
    return bool(decision.approved)


def resolve(request_id: str, accepted: bool) -> None:
    """Called by the UI when the user presses Approve/Decline."""
    _service.resolve(request_id, accepted)


def pending_ids() -> list[str]:
    return _service.pending_ids("coding")
