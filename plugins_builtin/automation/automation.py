"""Built-in automation plugin — declared workflows, executed for real.

It deliberately does not reimplement desktop control. A workflow is a list of
steps handed to ``capabilities.sequence``, which validates every step against
the real action registry and runs it through the same permission, approval and
lock path a single action uses. This plugin adds the parts sequence does not
have: a **saved workflow store**, an optional **condition**, and a persisted
**run history**.

Honest limits, stated rather than faked:

* the trigger is **manual** — the model or the user starts a run. There is no
  background scheduler in this plugin, so it never claims to have run on a
  timer it does not have;
* a condition is a real feature-flag gate, not an expression language.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

PLUGIN = {
    "name": "automation",
    "description": "Run saved workflows of desktop actions.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "run | save | list | run_saved | history | delete"},
            "name": {"type": "STRING", "description": "Workflow name."},
            "steps": {"type": "STRING", "description": "JSON array of steps."},
            "condition": {"type": "STRING", "description": "Feature flag that must be enabled."},
        },
        "required": ["action"],
    },
}


def _data_dir() -> Path:
    try:
        from config import settings as cfg
        return Path(cfg.data_dir())
    except Exception:
        return Path(__file__).resolve().parents[3] / "data"


def _workflows_path() -> Path:
    return _data_dir() / "plugin_workflows.json"


def _history_path() -> Path:
    return _data_dir() / "plugin_workflow_history.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _parse_steps(value):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return value


def _condition_ok(condition: str) -> tuple[bool, str]:
    name = str(condition or "").strip()
    if not name:
        return True, ""
    try:
        from config import settings as cfg
        if cfg.feature_enabled(name):
            return True, ""
        return False, (f"the condition '{name}' is switched off in Settings, so "
                       f"the workflow was not run.")
    except Exception as exc:
        return False, f"the condition '{name}' could not be checked: {exc}"


def _run_steps(steps, *, max_steps: int) -> tuple[bool, str, dict]:
    try:
        from capabilities import sequence
    except Exception as exc:
        return False, f"the desktop-sequence engine is unavailable: {exc}", {}
    if not isinstance(steps, list) or not steps:
        return False, "the workflow has no steps.", {}
    if len(steps) > max_steps:
        return False, (f"the workflow has {len(steps)} steps; the limit is "
                       f"{max_steps}."), {}
    result = sequence.run({"steps": steps[:max_steps]})
    ok = bool(result.get("ok"))
    return ok, str(result.get("output") or ""), result


def _record_history(name: str, ok: bool, detail: str, steps: int,
                    limit: int) -> None:
    path = _history_path()
    data = _load(path)
    runs = data.get("runs")
    if not isinstance(runs, list):
        runs = []
    runs.insert(0, {"name": name, "ok": bool(ok), "steps": int(steps),
                    "detail": str(detail)[:600], "at": time.time()})
    data["runs"] = runs[:max(1, limit)]
    _write(path, data)


def _action_run(params: dict, settings: dict) -> str:
    steps = _parse_steps(params.get("steps"))
    condition = str(params.get("condition")
                    or settings.get("default_condition") or "")
    ok, why = _condition_ok(condition)
    if not ok:
        return f"Skipped: {why}"
    max_steps = int(settings.get("max_steps") or 15)
    ran, output, _result = _run_steps(steps, max_steps=max_steps)
    _record_history("(inline)", ran, output, len(steps or []),
                    int(settings.get("history_limit") or 50))
    return output or ("The workflow finished." if ran else "The workflow failed.")


def _action_save(params: dict) -> str:
    name = str(params.get("name") or "").strip()
    if not name:
        return "Saving a workflow needs a name."
    steps = _parse_steps(params.get("steps"))
    if not isinstance(steps, list) or not steps:
        return "Saving a workflow needs a non-empty 'steps' array."
    store = _load(_workflows_path())
    store[name] = {"steps": steps,
                   "condition": str(params.get("condition") or ""),
                   "saved_at": time.time()}
    _write(_workflows_path(), store)
    return f"Saved workflow '{name}' with {len(steps)} step(s)."


def _action_list() -> str:
    store = _load(_workflows_path())
    if not store:
        return "No saved workflows yet."
    lines = ["Saved workflows:"]
    for name, entry in sorted(store.items()):
        steps = entry.get("steps") or []
        condition = str(entry.get("condition") or "")
        lines.append(f"• {name} — {len(steps)} step(s)"
                     + (f" · condition: {condition}" if condition else ""))
    return "\n".join(lines)


def _action_run_saved(params: dict, settings: dict) -> str:
    name = str(params.get("name") or "").strip()
    store = _load(_workflows_path())
    entry = store.get(name)
    if not entry:
        return f"There is no saved workflow named '{name}'."
    condition = str(params.get("condition") or entry.get("condition")
                    or settings.get("default_condition") or "")
    ok, why = _condition_ok(condition)
    if not ok:
        return f"Skipped '{name}': {why}"
    max_steps = int(settings.get("max_steps") or 15)
    ran, output, _result = _run_steps(entry.get("steps"), max_steps=max_steps)
    _record_history(name, ran, output, len(entry.get("steps") or []),
                    int(settings.get("history_limit") or 50))
    return output or (f"Workflow '{name}' finished." if ran
                      else f"Workflow '{name}' failed.")


def _action_history(settings: dict) -> str:
    runs = (_load(_history_path()).get("runs") or [])
    if not runs:
        return "No workflow runs recorded yet."
    limit = int(settings.get("history_limit") or 50)
    lines = ["Recent workflow runs (newest first):"]
    for run in runs[:min(limit, 20)]:
        stamp = time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(run.get("at") or 0))
        status = "OK" if run.get("ok") else "FAILED"
        lines.append(f"• {stamp} · {run.get('name')} · {status} · "
                     f"{run.get('steps')} step(s)")
    return "\n".join(lines)


def _action_delete(params: dict) -> str:
    name = str(params.get("name") or "").strip()
    store = _load(_workflows_path())
    if name not in store:
        return f"There is no saved workflow named '{name}'."
    store.pop(name, None)
    _write(_workflows_path(), store)
    return f"Deleted workflow '{name}'."


def run(parameters, settings=None, limits=None, logger=None) -> str:
    params = dict(parameters or {})
    settings = dict(settings or {})
    action = str(params.get("action") or "list").strip().lower()
    try:
        if action == "run":
            return _action_run(params, settings)
        if action == "save":
            return _action_save(params)
        if action in ("list", "workflows"):
            return _action_list()
        if action in ("run_saved", "start"):
            return _action_run_saved(params, settings)
        if action == "history":
            return _action_history(settings)
        if action == "delete":
            return _action_delete(params)
    except Exception as exc:
        return f"Automation failed: {exc}"
    return ("Unknown action. Use run, save, list, run_saved, history or delete.")
