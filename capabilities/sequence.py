"""Run a short, declared sequence of desktop actions as ONE request.

Why this exists
---------------
"Open Notepad, paste the clipboard, save it as notes.txt" is one spoken request
but three controller calls. Without a sequence the model has to guess whether
the second call still has the first one's window focused, and a failure halfway
through leaves the desktop in a state nobody described.

A sequence is **declared, not generated**: the model writes the steps as data
(a JSON array), this module validates every one of them against the same
registry the single actions use, and then runs them in order through the same
permission, approval and lock path. There is no expression language, no
variables, no loops and no recursion — a step is a tool name plus that tool's
own arguments.

Safety rules
------------
* Only five tools are allowed: ``pc_control``, ``window_control``,
  ``system_control``, ``clipboard_sentry`` and ``file_control``. Power actions,
  browser automation, accounts and anything that can end the session are not
  sequence steps.
* An action the registry marks ``gate="confirm"`` is refused, because the
  on-screen confirmation cannot be answered from inside a sequence. Run that
  action on its own.
* A step that fails stops the sequence. The result reports every step that ran,
  the one that failed, and the steps that were never attempted — it never
  presents a partial run as complete.
* The outer call holds no desktop lock while the steps run, so each step takes
  and releases its own lock exactly as a standalone call would.
"""

from __future__ import annotations

import json

__all__ = ["run", "ALLOWED_TOOLS", "MAX_STEPS"]

#: Tools a sequence may use. Deliberately narrower than the registry: the
#: excluded tools either end the session, act on another person's account, or
#: own a domain whose lock must never be nested inside a desktop sequence.
ALLOWED_TOOLS = ("pc_control", "window_control", "system_control",
                 "clipboard_sentry", "file_control")

MAX_STEPS = 15


def _parse_steps(value) -> tuple[list | None, str]:
    """Turn the ``steps`` argument into a list, or explain why it is not one."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "run_sequence needs a non-empty 'steps' array."
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, (f"'steps' is not valid JSON ({exc.msg} at position "
                          f"{exc.pos}). Pass a JSON array of step objects.")
    if not isinstance(value, list):
        return None, ("'steps' must be a JSON array of objects, each with a "
                      "'tool' and an 'action'.")
    if not value:
        return None, "'steps' is empty, so there is nothing to run."
    if len(value) > MAX_STEPS:
        return None, (f"A sequence may contain at most {MAX_STEPS} steps; this "
                      f"one has {len(value)}. Split it into two requests.")
    return value, ""


def _step_args(step: dict) -> dict:
    """The action arguments of one step (everything except the tool name)."""
    return {key: value for key, value in step.items() if key != "tool"}


def run(args: dict | None = None, *, enabled=None,
        on_event=None) -> dict:
    """Execute a declared sequence. Returns a result dict, never raises."""
    from capabilities import actions, registry

    args = dict(args or {})
    steps, error = _parse_steps(args.get("steps"))
    if steps is None:
        return {"ok": False, "controller": "sequence", "reason": "bad_steps",
                "detail": error, "output": error}

    total = len(steps)
    outcomes: list[dict] = []
    for index, raw_step in enumerate(steps):
        number = index + 1
        if not isinstance(raw_step, dict):
            return _stopped(outcomes, number, total,
                            f"Step {number} is not an object.", steps)
        tool = str(raw_step.get("tool") or "").strip()
        step_args = _step_args(raw_step)
        action_name = str(step_args.get("action") or "").strip()
        if not tool or not action_name:
            return _stopped(outcomes, number, total,
                            f"Step {number} needs both 'tool' and 'action'.", steps)
        if tool not in ALLOWED_TOOLS:
            return _stopped(
                outcomes, number, total,
                (f"Step {number} asks for '{tool}', which is not allowed in a "
                 f"sequence. Allowed: {', '.join(ALLOWED_TOOLS)}."), steps)
        if action_name == "run_sequence":
            return _stopped(outcomes, number, total,
                            f"Step {number} tries to nest a sequence inside a "
                            "sequence; that is not allowed.", steps)
        action = registry.get(tool, action_name)
        if action is None:
            return _stopped(
                outcomes, number, total,
                (f"Step {number}: '{action_name}' is not an action of {tool}. "
                 f"Valid actions: {', '.join(registry.names(tool))}."), steps)
        if getattr(action, "gate", "") == "confirm":
            return _stopped(
                outcomes, number, total,
                (f"Step {number} is '{tool}.{action_name}', which needs the "
                 "on-screen confirmation a sequence cannot answer. Run it on "
                 "its own so the confirmation can be shown."), steps)

        def _narrate(detail: str, _number: int = number) -> None:
            if on_event:
                try:
                    on_event(f"Step {_number}/{total}: {detail}")
                except Exception:
                    pass

        resolved, message, result = actions._run_action(
            tool, step_args, enabled=enabled, on_event=_narrate)
        if resolved is None or result is None:
            return _stopped(outcomes, number, total,
                            f"Step {number} could not run: {message}", steps)
        output = str(getattr(result, "output", "") or "")
        data = dict(getattr(result, "data", {}) or {})
        outcomes.append({
            "step": number,
            "tool": tool,
            "action": action_name,
            "ok": bool(result.ok),
            "controller": str(data.get("controller") or
                              getattr(result, "backend", "") or action.controller),
            "output": output[:600],
            "reason": str(getattr(result, "reason", "") or ""),
        })
        if not result.ok:
            return _stopped(
                outcomes, number, total,
                (f"Step {number} ({tool}.{action_name}) failed: "
                 + (output or getattr(result, "reason", "") or "no reason given")),
                steps)

    lines = [f"Ran all {total} step(s) in order:"]
    for item in outcomes:
        lines.append(f"  {item['step']}. {item['tool']}.{item['action']} — "
                     f"{'done' if item['ok'] else 'FAILED'} "
                     f"via {item['controller']}")
        if item["output"]:
            lines.append("     " + item["output"].replace("\n", "\n     ")[:400])
    return {"ok": True, "controller": "sequence", "detail": "\n".join(lines),
            "output": "\n".join(lines),
            "evidence": {"steps": outcomes, "ran": len(outcomes),
                         "total": total}}


def _stopped(outcomes: list[dict], number: int, total: int, message: str,
             steps: list) -> dict:
    """A failed step: report what ran, what failed, and what never started."""
    skipped = [
        f"{index + 1}. {step.get('tool', '?')}.{step.get('action', '?')}"
        for index, step in enumerate(steps)
        if index + 1 > number and isinstance(step, dict)
    ]
    lines = []
    if outcomes:
        lines.append(f"Ran {len(outcomes)} of {total} step(s) before stopping:")
        for item in outcomes:
            lines.append(f"  {item['step']}. {item['tool']}.{item['action']} — "
                         f"{'done' if item['ok'] else 'FAILED'} "
                         f"via {item['controller']}")
            if item["output"]:
                lines.append("     " + item["output"].replace("\n", "\n     ")[:300])
    lines.append(message)
    if skipped:
        lines.append("Not attempted: " + "; ".join(skipped) + ".")
    return {"ok": False, "controller": "sequence", "reason": "step_failed",
            "detail": "\n".join(lines), "output": "\n".join(lines),
            "evidence": {"steps": outcomes, "ran": len(outcomes),
                         "failed_step": number, "total": total,
                         "skipped": skipped}}
