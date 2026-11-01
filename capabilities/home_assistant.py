"""Home Assistant — the user's own instance, over its local REST API.

This is deliberately not a generic Home Assistant client. A generic client that
can call any service is a remote-control API for the whole house, including the
front-door lock, the alarm panel and the cameras. So the module exposes a fixed,
small vocabulary and refuses everything else by name:

* **Control** is limited to ``light``, ``switch``, ``fan`` and ``input_boolean``
  (plus activating a ``scene`` and running a ``script``, the latter behind the
  HUD confirmation declared in the registry).
* **Locks, alarms, cameras and covers are never controllable.** Reading their
  state is allowed — the user owns the instance — but a control request is
  refused with the domain named rather than silently mapped to something else.
* **There is no arbitrary service call.** The URL is built here from a validated
  entity id and one of six fixed service names, so a model-supplied string can
  never become ``/api/services/lock/unlock``.

Every control is read back from Home Assistant before it is reported as done.
A request that was accepted but did not change the entity is reported as not
verified, never as success. The long-lived token is sent only in an
``Authorization`` header to the configured URL, redirects are refused (so the
token cannot be forwarded to another host), and the token never appears in a
result sentence.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

__all__ = ["run", "status"]

_TIMEOUT = 10.0
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ENTITIES = 100
_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

#: The only domains a control action may touch. Everything else is refused,
#: which is why adding a new controllable domain is a deliberate code change.
_CONTROL_DOMAINS = frozenset({"light", "switch", "fan", "input_boolean"})
#: Domains with their own action rather than a generic on/off.
_SCENE_DOMAIN = "scene"
_SCRIPT_DOMAIN = "script"
#: Explicitly named in the refusal so the user knows this is policy, not a bug.
_BLOCKED_DOMAINS = frozenset({"lock", "alarm_control_panel", "camera", "cover"})

#: Attributes that are safe to repeat to the model. Credentials, camera URLs and
#: free-form blobs are not in this list, so they can never leak through a state
#: answer even if an integration stores them in an attribute.
_SAFE_ATTRIBUTES = (
    "friendly_name", "device_class", "unit_of_measurement", "icon",
    "brightness", "color_mode", "color_temp_kelvin", "rgb_color",
    "supported_color_modes", "percentage", "speed_count", "oscillating",
    "battery_level", "humidity", "temperature", "current_temperature",
    "target_temp_step", "last_triggered", "last_activated", "source",
    "volume_level", "is_volume_muted", "media_title", "media_artist",
)


class HomeAssistantError(Exception):
    """A real Home Assistant failure with a sentence the model may repeat."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── configuration ────────────────────────────────────────────────────────────

def _setting(key: str, default: Any = "") -> Any:
    """One setting with the project's precedence: env → api_keys.json → default."""
    try:
        from config.settings import get
        return get(key, default)
    except Exception:
        try:
            from memory.config_manager import load_api_keys
            return dict(load_api_keys() or {}).get(key, default)
        except Exception:
            return default


def _truthy(value: Any, default: bool = True) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text not in ("0", "false", "no", "off")


def _config() -> dict:
    return {
        "url": str(_setting("home_assistant_url", "") or "").strip().rstrip("/"),
        "token": str(_setting("home_assistant_token", "") or "").strip(),
        "verify": _truthy(_setting("home_assistant_verify_tls", "true")),
    }


def _base(config: dict | None = None) -> str:
    data = _config() if config is None else config
    url = str(data.get("url") or "").strip().rstrip("/")
    if not url:
        raise HomeAssistantError(
            "not_configured",
            "Home Assistant is not configured yet. Add the instance URL and a "
            "long-lived access token in Settings ▸ API ▸ Home Assistant. "
            "Nothing was sent anywhere.")
    if not url.startswith(("http://", "https://")):
        raise HomeAssistantError(
            "bad_config",
            "The Home Assistant URL must start with http:// or https://. "
            "Nothing was sent anywhere.")
    parts = urlsplit(url)
    if not parts.netloc or parts.username or parts.password:
        raise HomeAssistantError(
            "bad_config",
            "The Home Assistant URL must be a plain base URL (for example "
            "http://homeassistant.local:8123), without embedded credentials.")
    return url


def _token(config: dict | None = None) -> str:
    data = _config() if config is None else config
    token = str(data.get("token") or "").strip()
    if not token:
        raise HomeAssistantError(
            "not_configured",
            "Home Assistant has no long-lived access token. Create one on your "
            "Home Assistant profile page and paste it in Settings ▸ API ▸ "
            "Home Assistant. Nothing was sent anywhere.")
    if any(ch.isspace() for ch in token):
        raise HomeAssistantError(
            "bad_config",
            "The stored Home Assistant token contains spaces or line breaks, so "
            "it is pasted text rather than a token. Nothing was sent anywhere.")
    return token


# ── HTTP (two fixed methods, never a generic request) ────────────────────────

def _decode(resp, what: str) -> Any:
    if len(getattr(resp, "content", b"") or b"") > _MAX_BYTES:
        raise HomeAssistantError(
            "response_too_large",
            f"Home Assistant returned more data than this tool will read for "
            f"{what}. Narrow the request and try again.")
    if not (getattr(resp, "content", b"") or b""):
        return {}
    try:
        return resp.json()
    except Exception as exc:
        raise HomeAssistantError(
            "bad_response",
            f"Home Assistant answered {what}, but the response was not JSON.") from exc


def _check(resp, what: str) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 400:
        return
    message = ""
    try:
        message = str((resp.json() or {}).get("message") or "").strip()
    except Exception:
        message = ""
    if status in (401, 403):
        raise HomeAssistantError(
            "auth_failed",
            "Home Assistant rejected the token (HTTP "
            f"{status}). Check the long-lived access token and try again.")
    if status == 404:
        raise HomeAssistantError(
            "not_found",
            f"Home Assistant returned 404 for {what}: the entity does not exist.")
    if status in (400, 422):
        raise HomeAssistantError(
            "bad_request",
            f"Home Assistant refused {what} (HTTP {status}"
            + (f": {message}" if message else "") + "). Nothing was changed.")
    raise HomeAssistantError(
        "home_assistant_error",
        f"Home Assistant returned HTTP {status} for {what}"
        + (f": {message}" if message else "") + ".")


def _request_get(path: str, what: str = "the request") -> tuple[Any, Any]:
    config = _config()
    base = _base(config)
    token = _token(config)
    if not path.startswith("/api/") or ".." in path:
        raise HomeAssistantError("bad_request", "The Home Assistant request path "
                                                 "was not a valid API path.")
    try:
        import requests
        resp = requests.get(base + path,
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=_TIMEOUT, verify=bool(config.get("verify", True)),
                            allow_redirects=False)
    except Exception as exc:
        raise HomeAssistantError(
            "network_error",
            f"Home Assistant did not answer ({type(exc).__name__}: {exc}). "
            "Nothing was changed.") from exc
    _check(resp, what)
    return _decode(resp, what), getattr(resp, "headers", {})


def _request_post(path: str, payload: dict, what: str = "the request") -> Any:
    config = _config()
    base = _base(config)
    token = _token(config)
    if not path.startswith("/api/services/") or ".." in path:
        raise HomeAssistantError("bad_request", "The Home Assistant service path "
                                                 "was not a valid API path.")
    try:
        import requests
        resp = requests.post(base + path,
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json"},
                             json=payload, timeout=_TIMEOUT,
                             verify=bool(config.get("verify", True)),
                             allow_redirects=False)
    except Exception as exc:
        raise HomeAssistantError(
            "network_error",
            f"Home Assistant did not answer ({type(exc).__name__}: {exc}). "
            "Nothing was changed.") from exc
    _check(resp, what)
    return _decode(resp, what)


# ── entity helpers ───────────────────────────────────────────────────────────

def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "home-assistant", "detail": detail,
            "reason": "", "evidence": evidence}


def _fail(detail: str, reason: str = "home_assistant_error") -> dict:
    return {"ok": False, "controller": "home-assistant", "detail": detail,
            "reason": reason, "evidence": {}}


def _entity_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _ENTITY_RE.match(text) else ""


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _friendly(state: dict) -> str:
    attrs = state.get("attributes") or {}
    return str(attrs.get("friendly_name") or state.get("entity_id") or "entity")


def _states() -> list[dict]:
    data, _ = _request_get("/api/states", "the entity list")
    if not isinstance(data, list):
        raise HomeAssistantError("bad_response",
                                 "Home Assistant returned an entity list in an "
                                 "unexpected shape.")
    return [item for item in data if isinstance(item, dict)
            and _entity_id(item.get("entity_id"))]


def _resolve(query: Any) -> dict:
    """One entity by exact id or exact friendly name; ambiguity is refused."""
    text = str(query or "").strip()
    if not text:
        raise HomeAssistantError("bad_entity", "Name a Home Assistant entity.")
    direct = _entity_id(text)
    if direct:
        data, _ = _request_get(f"/api/states/{direct}", f"entity {direct}")
        if not isinstance(data, dict):
            raise HomeAssistantError("bad_response",
                                     f"Home Assistant returned an unexpected "
                                     f"state for {direct}.")
        return data

    wanted = text.casefold()
    matches = [state for state in _states()
               if _friendly(state).casefold() == wanted]
    if not matches:
        raise HomeAssistantError(
            "not_found",
            f"No Home Assistant entity is named '{text}'. Use entities with "
            "search to find the exact name, then use its entity id.")
    if len(matches) > 1:
        ids = ", ".join(sorted(str(s.get("entity_id")) for s in matches))
        raise HomeAssistantError(
            "ambiguous_entity",
            f"'{text}' matches more than one Home Assistant entity ({ids}). "
            "Ask which one, then use its entity id.")
    return matches[0]


def _controllable(state: dict) -> tuple[bool, str]:
    domain = _domain(_entity_id(state.get("entity_id")))
    if domain in _CONTROL_DOMAINS:
        return True, ""
    if domain in _BLOCKED_DOMAINS:
        return False, (f"{domain} entities are deliberately not controllable. "
                       "Locks, alarms, cameras and covers are read-only here.")
    return False, (f"The {domain} domain is not controllable by this tool. It can "
                   "switch lights, switches, fans and input booleans, activate "
                   "scenes and run scripts.")


def _safe_attributes(state: dict) -> dict:
    attrs = state.get("attributes") or {}
    safe = {}
    for key in _SAFE_ATTRIBUTES:
        if key not in attrs:
            continue
        value = attrs.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)) and len(value) <= 12:
            safe[key] = list(value)
    return safe


def _state_line(state: dict) -> str:
    entity_id = _entity_id(state.get("entity_id"))
    name = _friendly(state)
    value = str(state.get("state") or "unknown")
    line = f"{name} ({entity_id}) is {value}."
    attrs = _safe_attributes(state)
    bits = []
    for key in ("brightness", "percentage", "unit_of_measurement",
                "current_temperature", "media_title", "last_triggered",
                "last_activated"):
        if key in attrs and attrs[key] not in (None, ""):
            if key == "brightness":
                try:
                    bits.append(f"brightness {round(int(attrs[key]) * 100 / 255)}%")
                except Exception:
                    bits.append(f"brightness {attrs[key]}")
            else:
                bits.append(f"{key.replace('_', ' ')} {attrs[key]}")
    if bits:
        line += " " + ", ".join(bits) + "."
    return line


# ── actions ──────────────────────────────────────────────────────────────────

def _h_status(args: dict) -> dict:
    del args
    config = _config()
    base = _base(config)
    _token(config)
    data, _ = _request_get("/api/", "the API root")
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or "")
    version = location = timezone = ""
    try:
        info, _ = _request_get("/api/config", "the instance configuration")
        if isinstance(info, dict):
            version = str(info.get("version") or "")
            location = str(info.get("location_name") or "")
            timezone = str(info.get("time_zone") or "")
    except HomeAssistantError:
        info = {}
    detail = f"Home Assistant answered at {base} and accepted the token."
    if message:
        detail += f" ({message})"
    if version:
        detail += f" Version {version}"
        if location:
            detail += f", location {location}"
        detail += "."
    return _ok(detail, url=base, version=version, location=location,
               time_zone=timezone)


def _h_entities(args: dict) -> dict:
    domain = str(args.get("domain") or "").strip().lower()
    search = str(args.get("search") or "").strip().casefold()
    try:
        limit = int(args.get("limit") or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(_MAX_ENTITIES, limit))

    rows = []
    for state in _states():
        entity_id = _entity_id(state.get("entity_id"))
        item_domain = _domain(entity_id)
        if domain and item_domain != domain:
            continue
        name = _friendly(state)
        if search and search not in entity_id.casefold() \
                and search not in name.casefold():
            continue
        can_control, why = _controllable(state)
        rows.append((entity_id, name, str(state.get("state") or "unknown"),
                     can_control, why))
    rows.sort(key=lambda row: row[0])
    if not rows:
        filt = []
        if domain:
            filt.append(f"domain {domain}")
        if search:
            filt.append(f"matching '{search}'")
        return _ok("No Home Assistant entities matched"
                   + (" " + " and ".join(filt) if filt else "") + ".",
                   count=0)
    lines = []
    for entity_id, name, value, can_control, why in rows[:limit]:
        marker = "" if can_control else " [read-only]"
        lines.append(f"{name} — {entity_id} — {value}{marker}")
    detail = f"{len(rows)} entity/entities"
    if len(rows) > limit:
        detail += f" (showing the first {limit})"
    detail += ":\n" + "\n".join(lines)
    if any(not row[3] for row in rows[:limit]):
        detail += ("\nEntities marked [read-only] cannot be controlled by this "
                   "tool.")
    return _ok(detail, count=len(rows), shown=min(len(rows), limit),
               domain=domain, search=search)


def _h_state(args: dict) -> dict:
    state = _resolve(args.get("entity"))
    entity_id = _entity_id(state.get("entity_id"))
    can_control, why = _controllable(state)
    detail = _state_line(state)
    if why:
        detail += " " + why
    return _ok(detail, entity_id=entity_id,
               domain=_domain(entity_id), state=str(state.get("state") or ""),
               controllable=can_control, attributes=_safe_attributes(state))


def _readback(entity_id: str) -> dict | None:
    """Read one entity after a command; None when the read itself failed."""
    for attempt in range(2):
        try:
            data, _ = _request_get(f"/api/states/{entity_id}",
                                   f"the read-back for {entity_id}")
            return data if isinstance(data, dict) else None
        except HomeAssistantError as exc:
            if exc.reason in ("not_found", "auth_failed"):
                return None
            if attempt == 0:
                time.sleep(0.2)
    return None


def _verify(entity_id: str, before: str, expected: str, *,
            brightness_pct: int | None = None) -> tuple[bool | None, dict | None]:
    """Return (verified, state). None means the read-back itself failed."""
    after = _readback(entity_id)
    if after is None:
        return None, None
    value = str(after.get("state") or "")
    if brightness_pct is not None:
        attrs = after.get("attributes") or {}
        actual = attrs.get("brightness")
        try:
            wanted = round(int(brightness_pct) * 255 / 100)
            ok = value == "on" and actual is not None \
                and abs(int(actual) - wanted) <= 2
        except Exception:
            ok = False
        return ok, after
    if expected == "toggle":
        return value != before, after
    return value == expected, after


def _control(action: str, args: dict) -> dict:
    state = _resolve(args.get("entity"))
    entity_id = _entity_id(state.get("entity_id"))
    domain = _domain(entity_id)

    before = str(state.get("state") or "")
    payload: dict[str, Any] = {"entity_id": entity_id}
    brightness_pct: int | None = None

    # The on/off family answers to the control allowlist. Scenes and scripts
    # have their own domain checks below — putting the allowlist first would
    # refuse them, since "scene" and "script" are deliberately not switchable
    # domains.
    if action in ("turn_on", "turn_off", "toggle", "set_brightness"):
        can_control, why = _controllable(state)
        if not can_control:
            raise HomeAssistantError("blocked_domain", why)

    if action in ("turn_on", "turn_off", "toggle"):
        service = action
        expected = {"turn_on": "on", "turn_off": "off", "toggle": "toggle"}[action]
    elif action == "set_brightness":
        if domain != "light":
            raise HomeAssistantError(
                "blocked_domain",
                f"{entity_id} is a {domain}, not a light, so it has no "
                "brightness. Nothing was changed.")
        try:
            brightness_pct = int(args.get("brightness_pct"))
        except (TypeError, ValueError):
            raise HomeAssistantError("bad_brightness",
                                     "Brightness must be a whole percentage "
                                     "between 1 and 100.")
        brightness_pct = max(1, min(100, brightness_pct))
        payload["brightness_pct"] = brightness_pct
        service = "turn_on"
        expected = "on"
    elif action == "activate_scene":
        if domain != _SCENE_DOMAIN:
            raise HomeAssistantError(
                "blocked_domain",
                f"{entity_id} is not a scene. Only scene entities can be "
                "activated with this action.")
        service = "turn_on"
        expected = ""
    elif action == "run_script":
        if domain != _SCRIPT_DOMAIN:
            raise HomeAssistantError(
                "blocked_domain",
                f"{entity_id} is not a script. Only script entities can be run "
                "with this action.")
        service = "turn_on"
        expected = ""
    else:
        raise HomeAssistantError("unknown_action",
                                 f"'{action}' is not a Home Assistant control "
                                 f"action. Nothing was changed.")

    # The service name and domain are built from validated values, never from
    # arbitrary text: there is no path from a tool argument to another service.
    _request_post(f"/api/services/{domain}/{service}", payload,
                  f"{service} on {entity_id}")

    if action in ("activate_scene", "run_script"):
        after = _readback(entity_id)
        detail = (f"Home Assistant accepted the request to "
                  f"{'activate' if action == 'activate_scene' else 'run'} "
                  f"{_friendly(state)} ({entity_id}).")
        if after is None:
            detail += (" The read-back failed, so the outcome is unverified.")
            return {"ok": True, "controller": "home-assistant",
                    "detail": detail, "reason": "",
                    "evidence": {"entity_id": entity_id, "domain": domain,
                                 "service": service, "verified": False}}
        detail += f" It now reports {after.get('state')}."
        return _ok(detail, entity_id=entity_id, domain=domain, service=service,
                   state_before=before,
                   state_after=str(after.get("state") or ""), verified=True)

    verified, after = _verify(entity_id, before, expected,
                              brightness_pct=brightness_pct)
    name = _friendly(state)
    if after is None:
        detail = (f"Home Assistant accepted the request for {name} ({entity_id}), "
                  "but the read-back failed. I cannot confirm the device changed.")
        return {"ok": True, "controller": "home-assistant", "detail": detail,
                "reason": "", "evidence": {"entity_id": entity_id,
                                            "domain": domain,
                                            "service": service,
                                            "state_before": before,
                                            "verified": False}}
    after_value = str(after.get("state") or "")
    if not verified:
        if brightness_pct is not None:
            detail = (f"Home Assistant accepted the request for {name} "
                      f"({entity_id}), but it reports {after_value} at "
                      f"brightness {((after.get('attributes') or {}).get('brightness'))}, "
                      f"not the requested {brightness_pct}%. The change is not "
                      "confirmed.")
        else:
            detail = (f"Home Assistant accepted the request for {name} "
                      f"({entity_id}), but it still reports {after_value} instead "
                      f"of {expected}. The change is not confirmed.")
        return {"ok": False, "controller": "home-assistant", "detail": detail,
                "reason": "state_mismatch",
                "evidence": {"entity_id": entity_id, "domain": domain,
                             "service": service, "state_before": before,
                             "state_after": after_value, "verified": False}}
    detail = f"{name} ({entity_id}) is now {after_value}."
    if brightness_pct is not None:
        detail += f" Brightness is {brightness_pct}%."
    return _ok(detail, entity_id=entity_id, domain=domain, service=service,
               state_before=before, state_after=after_value, verified=True,
               brightness_pct=brightness_pct)


def _h_turn_on(args: dict) -> dict:
    return _control("turn_on", args)


def _h_turn_off(args: dict) -> dict:
    return _control("turn_off", args)


def _h_toggle(args: dict) -> dict:
    return _control("toggle", args)


def _h_brightness(args: dict) -> dict:
    return _control("set_brightness", args)


def _h_scene(args: dict) -> dict:
    return _control("activate_scene", args)


def _h_script(args: dict) -> dict:
    return _control("run_script", args)


_HANDLERS = {
    "status": _h_status,
    "entities": _h_entities,
    "state": _h_state,
    "turn_on": _h_turn_on,
    "turn_off": _h_turn_off,
    "toggle": _h_toggle,
    "set_brightness": _h_brightness,
    "activate_scene": _h_scene,
    "run_script": _h_script,
}


def status() -> dict:
    """Configuration shape without spending a request (used by diagnostics)."""
    config = _config()
    configured = bool(config.get("url") and config.get("token"))
    return {"configured": configured, "url": config.get("url", ""),
            "detail": ("Home Assistant is configured." if configured else
                       "Home Assistant has no URL/token configured.")}


def run(action_name: str, args: dict) -> dict:
    """Run one Home Assistant action; never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip())
    if handler is None:
        return _fail(f"'{action_name}' is not a Home Assistant action. Valid "
                     f"actions: {', '.join(_HANDLERS)}.", "unknown_action")
    try:
        return handler(dict(args or {}))
    except HomeAssistantError as exc:
        return _fail(exc.detail, exc.reason)
    except Exception as exc:
        return _fail(f"Home Assistant action failed ({type(exc).__name__}: "
                     f"{exc}). Nothing was changed.", "home_assistant_error")
