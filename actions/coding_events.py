"""Real coding progress → the chat's live coding card.

The dev agent and the code helper call :func:`emit_coding` at each REAL step
(planning, writing a file, running, fixing). This bridge is deliberately
fail-safe in both directions:

* a player without the channel (legacy UI, tests) is a no-op;
* a player whose UI raises can never break a build.

:func:`unified_diff_text` produces the bounded unified diff for the card's
"what changed" view; it is plain stdlib, so the actions keep no UI dependency.
"""
from __future__ import annotations

import difflib

__all__ = ["emit_coding", "unified_diff_text"]


def emit_coding(player, kind: str, **payload) -> None:
    """Report one real coding step to the UI. Never raises."""
    try:
        fn = getattr(player, "coding_activity", None)
        if callable(fn):
            fn(str(kind), dict(payload))
    except Exception:
        pass


def unified_diff_text(old: str, new: str, *, path: str = "",
                      max_lines: int = 160) -> str:
    """A compact unified diff (bounded), or '' when nothing changed."""
    try:
        old_lines = str(old or "").splitlines()
        new_lines = str(new or "").splitlines()
        if old_lines == new_lines:
            return ""
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=str(path or "before"),
            tofile=str(path or "after"),
            lineterm="", n=2,
        ))
        if len(diff) > max_lines:
            diff = diff[:max_lines] + [
                f"… ({len(diff) - max_lines} more diff lines)"]
        return "\n".join(diff)
    except Exception:
        return ""
