"""Drive real Windows controls by their own names, not by guessed coordinates.

Why this exists
---------------
``capabilities/desktop.py`` clicks and types at a *position*, and
``capabilities/uitars.py`` asks a model where that position is. Both are the
right tool when nothing better exists. When a control exposes UI Automation,
though, Windows itself can say exactly what it is called and exactly where it
is — and the action becomes deterministic: no model, no pixel, no guessing.

This module is that third controller: the Windows UI Automation tree through
``pywinauto``'s UIA backend. It exposes four actions:

* ``ui_tree``  — list a window's controls (bounded depth and count);
* ``ui_read``  — read one control's text/value;
* ``ui_click`` — invoke a named control (Invoke pattern first, real click as
  the fallback, and the result says which one ran);
* ``ui_set_text`` — set an edit field's value through the Value pattern, with a
  click-and-type fallback reported the same way.

Honesty rules
-------------
* The result always names the window and control it actually used.
* A control that is not found is reported as not found, with the closest names
  that *were* in the tree — never as "done".
* ``ui_set_text`` verifies the value it reads back and says so. If the control
  does not expose a readable value, it says it could not verify instead of
  claiming success.
* Nothing here executes a model's code or synthesises an action name.

The module is import-safe on a machine without ``pywinauto``: the probe reports
the gap and every action refuses with the same sentence.
"""

from __future__ import annotations

import time

__all__ = ["run", "status", "tree", "click", "set_text", "read"]

_MAX_NODES = 3500
_MAX_DEPTH = 8
_MAX_TREE_ROWS = 220


def _probe() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("pywinauto") is not None
    except Exception:
        return False


def status() -> dict:
    """Whether the UIA controller can run, probed (never assumed)."""
    have = _probe()
    detail = ("Windows UI Automation ready (pywinauto UIA backend)" if have
              else "pywinauto is not installed (pip install pywinauto)")
    return {"available": have, "pywinauto": have, "detail": detail}


# ── wrappers ──────────────────────────────────────────────────────────────────

def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _process_name(pid: int) -> str:
    try:
        import psutil
        return str(psutil.Process(int(pid)).name() or "").lower()
    except Exception:
        return ""


def _info(wrapper) -> dict:
    """The stable, cheap properties of one UIA wrapper."""
    element = getattr(wrapper, "element_info", None)
    title = ""
    try:
        title = str(wrapper.window_text() or "")
    except Exception:
        title = str(getattr(element, "name", "") or "")
    return {
        "name": str(getattr(element, "name", "") or title or ""),
        "title": title,
        "automation_id": str(getattr(element, "automation_id", "") or ""),
        "control_type": str(getattr(element, "control_type", "") or ""),
        "class_name": str(getattr(element, "class_name", "") or ""),
        "enabled": bool(getattr(element, "is_enabled", True)),
        "pid": int(getattr(element, "process_id", 0) or 0),
        "handle": int(getattr(element, "handle", 0) or 0),
        "rectangle": _rect(wrapper),
    }


def _rect(wrapper) -> dict:
    try:
        r = wrapper.rectangle()
        return {"left": int(r.left), "top": int(r.top),
                "right": int(r.right), "bottom": int(r.bottom)}
    except Exception:
        return {}


def _norm(value) -> str:
    return " ".join(str(value or "").casefold().split())


def _label(info: dict) -> str:
    """The most specific human name a control exposes (never empty)."""
    return (str(info.get("name") or "").strip()
            or str(info.get("automation_id") or "").strip()
            or str(info.get("control_type") or "").strip()
            or "control")


def _top_windows() -> list:
    """Every visible top-level window, as wrappers."""
    out = []
    try:
        for window in _desktop().windows():
            try:
                if not window.is_visible():
                    continue
                title = str(window.window_text() or "").strip()
                if not title:
                    continue
                out.append(window)
            except Exception:
                continue
    except Exception:
        return []
    return out


def _window(args: dict) -> tuple[object | None, str]:
    """Resolve the window named by ``title``/``app``, or the foreground one."""
    title = _norm(args.get("title"))
    app = _norm(args.get("app"))
    if title or app:
        best = None
        best_score = 0
        for window in _top_windows():
            try:
                info = _info(window)
            except Exception:
                continue
            window_title = _norm(info.get("title"))
            process = _process_name(info.get("pid"))
            score = 0
            if title:
                if title == window_title:
                    score += 100
                elif title in window_title:
                    score += 60
                else:
                    continue
            if app:
                alias = app[:-4] if app.endswith(".exe") else app
                if alias and (alias in process or alias in window_title):
                    score += 40
                else:
                    continue
            if score > best_score:
                best, best_score = window, score
        if best is None:
            wanted = args.get("title") or args.get("app")
            available = []
            for window in _top_windows()[:12]:
                try:
                    available.append(str(window.window_text() or "")[:60])
                except Exception:
                    continue
            return None, (f"No open window matched {wanted!r}. Open windows: "
                          + ("; ".join(available) or "none with a title") + ".")
        return best, ""
    # No filter: the window the user is actually looking at.
    try:
        import win32gui
        handle = int(win32gui.GetForegroundWindow())
        if handle:
            return _desktop().window(handle=handle), ""
    except Exception:
        pass
    windows = _top_windows()
    if not windows:
        return None, "No visible window is open, so there is no control to act on."
    return windows[0], ""


def _children(wrapper) -> list:
    try:
        return list(wrapper.children())
    except Exception:
        return []


def _walk(wrapper, max_depth: int = _MAX_DEPTH, max_nodes: int = _MAX_NODES):
    """Breadth-first walk of one window's control tree (bounded, never raising)."""
    queue: list[tuple[object, int]] = [(wrapper, 0)]
    seen = 0
    while queue and seen < max_nodes:
        node, depth = queue.pop(0)
        seen += 1
        yield node, depth
        if depth >= max_depth:
            continue
        for child in _children(node):
            queue.append((child, depth + 1))


def _score_control(info: dict, args: dict) -> int:
    """How well one control matches the request (0 = not a match)."""
    control = _norm(args.get("control"))
    automation_id = _norm(args.get("automation_id"))
    control_type = _norm(args.get("control_type"))
    if control_type and _norm(info.get("control_type")) != control_type:
        return 0
    if automation_id and _norm(info.get("automation_id")) != automation_id:
        return 0
    if control:
        name = _norm(info.get("name"))
        title = _norm(info.get("title"))
        if control == name or control == title:
            score = 100
        elif control in name or control in title:
            score = 60
        else:
            return 0
    else:
        score = 40 if (automation_id or control_type) else 0
    # Prefer the most specific (smallest) matching control when names repeat.
    rect = info.get("rectangle") or {}
    area = max(0, int(rect.get("right", 0)) - int(rect.get("left", 0))) * \
        max(0, int(rect.get("bottom", 0)) - int(rect.get("top", 0)))
    if area:
        score += max(0, 20 - min(20, area // 40000))
    if not info.get("enabled", True):
        score -= 10
    return score


def _find_control(window, args: dict) -> tuple[object | None, dict, str]:
    """The best control match in *window*, plus a truthful miss message."""
    if not (args.get("control") or args.get("automation_id")
            or args.get("control_type")):
        return None, {}, ("Name the control to act on: 'control' (its visible "
                          "label), 'automation_id', or 'control_type'.")
    best = None
    best_info: dict = {}
    best_score = 0
    nearby: list[str] = []
    for node, _depth in _walk(window):
        try:
            info = _info(node)
        except Exception:
            continue
        score = _score_control(info, args)
        if score > best_score:
            best, best_info, best_score = node, info, score
        if len(nearby) < 25 and info.get("name"):
            nearby.append(f"{info['name']} ({info.get('control_type') or '?'})")
    if best is None:
        wanted = (args.get("control") or args.get("automation_id")
                  or args.get("control_type"))
        return None, {}, (f"No control matching {wanted!r} is in that window. "
                          f"Controls that are there: "
                          + ("; ".join(nearby) or "none exposed") + ".")
    return best, best_info, ""


# ── handlers ──────────────────────────────────────────────────────────────────

def _h_tree(args: dict) -> dict:
    window, error = _window(args)
    if window is None:
        return {"ok": False, "controller": "uia", "reason": "window_not_found",
                "detail": error}
    try:
        window_info = _info(window)
    except Exception:
        window_info = {}
    try:
        depth_limit = max(1, min(_MAX_DEPTH, int(args.get("depth") or 3)))
    except (TypeError, ValueError):
        depth_limit = 3
    try:
        row_limit = max(10, min(_MAX_TREE_ROWS, int(args.get("limit") or 80)))
    except (TypeError, ValueError):
        row_limit = 80
    rows: list[str] = []
    total = 0
    for node, depth in _walk(window, max_depth=depth_limit):
        total += 1
        if depth == 0 or len(rows) >= row_limit:
            continue
        try:
            info = _info(node)
        except Exception:
            continue
        label = info.get("name") or info.get("automation_id") or "(unnamed)"
        bits = [f"{'  ' * (depth - 1)}{label}"]
        if info.get("control_type"):
            bits.append(f"[{info['control_type']}]")
        if info.get("automation_id"):
            bits.append(f"id={info['automation_id']}")
        if not info.get("enabled", True):
            bits.append("(disabled)")
        rows.append(" ".join(bits))
    detail = (f"UI tree of '{window_info.get('title') or window_info.get('name')}' "
              f"— {total} control(s) walked, {len(rows)} shown at depth "
              f"{depth_limit}:\n" + "\n".join(rows))
    if total > len(rows) + 1:
        detail += f"\n… {total - len(rows) - 1} more control(s) not shown."
    return {"ok": True, "controller": "uia", "detail": detail,
            "evidence": {"window": window_info.get("title"),
                         "pid": window_info.get("pid"),
                         "controls": total, "shown": len(rows),
                         "depth": depth_limit}}


def _read_value(wrapper) -> str:
    """One control's value/text, through whichever pattern it exposes."""
    try:
        value = wrapper.get_value()
        if value not in (None, ""):
            return str(value)
    except Exception:
        pass
    try:
        return str(wrapper.iface_value.CurrentValue or "")
    except Exception:
        pass
    try:
        return str(wrapper.window_text() or "")
    except Exception:
        return ""


def _h_read(args: dict) -> dict:
    window, error = _window(args)
    if window is None:
        return {"ok": False, "controller": "uia", "reason": "window_not_found",
                "detail": error}
    if not (args.get("control") or args.get("automation_id")
            or args.get("control_type")):
        # No control named: read the window's own title and the visible text of
        # its direct children, which is what "read this dialog" usually means.
        lines = []
        for node, depth in _walk(window, max_depth=4):
            if depth == 0:
                continue
            try:
                info = _info(node)
            except Exception:
                continue
            text = _read_value(node).strip()
            if text:
                lines.append(f"{info.get('name') or info.get('control_type')}: {text}")
            if len(lines) >= 60:
                break
        try:
            title = str(window.window_text() or "")
        except Exception:
            title = ""
        body = "\n".join(lines) or "no readable text"
        return {"ok": True, "controller": "uia",
                "detail": f"Window '{title}':\n{body}",
                "evidence": {"window": title, "lines": len(lines)}}
    control, info, error = _find_control(window, args)
    if control is None:
        return {"ok": False, "controller": "uia", "reason": "control_not_found",
                "detail": error}
    text = _read_value(control)
    if not text.strip():
        return {"ok": False, "controller": "uia", "reason": "no_value",
                "detail": (f"The control '{_label(info)}' exposes no readable "
                           "text or value."),
                "evidence": {"control": info.get("name"),
                             "control_type": info.get("control_type")}}
    return {"ok": True, "controller": "uia",
            "detail": f"{_label(info)} reads: {text}",
            "evidence": {"window": args.get("title") or "",
                         "control": info.get("name"),
                         "control_type": info.get("control_type"),
                         "automation_id": info.get("automation_id"),
                         "value": text[:500]}}


def _h_click(args: dict) -> dict:
    window, error = _window(args)
    if window is None:
        return {"ok": False, "controller": "uia", "reason": "window_not_found",
                "detail": error}
    control, info, error = _find_control(window, args)
    if control is None:
        return {"ok": False, "controller": "uia", "reason": "control_not_found",
                "detail": error}
    if not info.get("enabled", True):
        return {"ok": False, "controller": "uia", "reason": "control_disabled",
                "detail": (f"The control '{_label(info)}' is disabled, so a "
                           "click would do nothing. Nothing was clicked."),
                "evidence": {"control": info.get("name")}}
    how = ""
    try:
        control.iface_invoke.Invoke()
        how = "invoke"
    except Exception:
        try:
            control.click_input()
            how = "click_input"
        except Exception as exc:
            return {"ok": False, "controller": "uia", "reason": "click_failed",
                    "detail": (f"The control '{_label(info)}' was found but "
                               f"neither Invoke nor a real click worked: {exc}"),
                    "evidence": {"control": info.get("name")}}
    time.sleep(0.25)
    return {"ok": True, "controller": "uia",
            "detail": (f"{'Invoked' if how == 'invoke' else 'Clicked'} "
                       f"'{_label(info)}' "
                       f"({info.get('control_type') or 'control'}) in "
                       f"'{args.get('title') or 'the foreground window'}' "
                       f"using {how}."),
            "evidence": {"window": args.get("title") or "",
                         "control": info.get("name"),
                         "control_type": info.get("control_type"),
                         "automation_id": info.get("automation_id"),
                         "how": how, "rectangle": info.get("rectangle")}}


def _h_set_text(args: dict) -> dict:
    text = str(args.get("text") or "")
    if not text:
        return {"ok": False, "controller": "uia", "reason": "no_text",
                "detail": "ui_set_text needs the text to put in the field."}
    window, error = _window(args)
    if window is None:
        return {"ok": False, "controller": "uia", "reason": "window_not_found",
                "detail": error}
    control, info, error = _find_control(window, args)
    if control is None:
        return {"ok": False, "controller": "uia", "reason": "control_not_found",
                "detail": error}
    how = ""
    try:
        control.set_edit_text(text)
        how = "value_pattern"
    except Exception:
        try:
            control.iface_value.SetValue(text)
            how = "value_pattern"
        except Exception:
            try:
                control.click_input()
                control.type_keys("^a{BACKSPACE}", set_foreground=True)
                control.type_keys(text, with_spaces=True, set_foreground=True)
                how = "click_and_type"
            except Exception as exc:
                return {"ok": False, "controller": "uia", "reason": "type_failed",
                        "detail": (f"The field '{_label(info)}' was found but "
                                   f"could not be filled: {exc}"),
                        "evidence": {"control": info.get("name")}}
    time.sleep(0.15)
    observed = _read_value(control).strip()
    verified = bool(observed) and (observed == text.strip()
                                   or text.strip() in observed)
    detail = (f"Set '{_label(info)}' to the requested text using {how}.")
    if observed:
        detail += (" Read-back verified the value." if verified
                   else f" Read-back shows '{observed[:80]}' instead.")
    else:
        detail += (" The control exposes no readable value, so the write could "
                   "not be verified.")
    return {"ok": True, "controller": "uia", "detail": detail,
            "evidence": {"window": args.get("title") or "",
                         "control": info.get("name"),
                         "control_type": info.get("control_type"),
                         "automation_id": info.get("automation_id"),
                         "how": how, "verified": verified,
                         "read_back": observed[:200]}}


_HANDLERS = {
    "ui_tree": _h_tree,
    "ui_read": _h_read,
    "ui_click": _h_click,
    "ui_set_text": _h_set_text,
}


# ── entry points ──────────────────────────────────────────────────────────────

def tree(args: dict | None = None) -> dict:
    return _h_tree(dict(args or {}))


def read(args: dict | None = None) -> dict:
    return _h_read(dict(args or {}))


def click(args: dict | None = None) -> dict:
    return _h_click(dict(args or {}))


def set_text(args: dict | None = None) -> dict:
    return _h_set_text(dict(args or {}))


def run(action: str, args: dict) -> dict:
    """Execute one UI Automation action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action or "").strip())
    if handler is None:
        return {"ok": False, "controller": "uia",
                "detail": f"'{action}' is not a UI Automation action."}
    state = status()
    if not state.get("available"):
        return {"ok": False, "controller": "uia", "reason": "pywinauto_missing",
                "detail": state["detail"] + ". Nothing else was substituted."}
    try:
        return handler(dict(args or {})) or {
            "ok": False, "controller": "uia", "detail": "No result."}
    except Exception as exc:
        return {"ok": False, "controller": "uia", "reason": "crashed",
                "detail": f"UI Automation failed: {type(exc).__name__}: {exc}"}
