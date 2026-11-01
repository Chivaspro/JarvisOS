"""Plugin manifest v2 — one declarative description per plugin.

A plugin used to be a single ``.py`` with a ``PLUGIN`` dict, and the Settings
panel could only show a name and a description. That is not enough to run a
real plugin safely or configure it honestly: the user needs to see what the
plugin may touch, supply its credentials, choose its model, and set an
execution budget — and the loader needs the same information to enforce it.

A manifest lives in a ``plugin.json`` next to the plugin's module (or in its
folder). It is **optional**: a plain ``PLUGIN``-dict plugin keeps working
unchanged. When both exist the manifest wins for the fields it declares.

Schema (all keys optional except ``name``)::

    {
      "id": "research",                 # stable id; defaults to the folder name
      "name": "research",               # the tool name the model calls
      "description": "…",
      "icon": "🔎",
      "version": "1.0.0",
      "category": "research",
      "builtin": true,
      "permissions": ["network"],
      "requires": ["ffmpeg"],           # external apps/tools; see plugin_requires
      "parameters": {"type": "OBJECT", "properties": {}},   # tool signature
      "settings": [ {"key": "…", "label": "…", "type": "string|text|bool|int|
                     float|choice|secret|model", "default": …,
                     "help": "…", "choices": [[value, label], …],
                     "min": …, "max": …, "step": …, "advanced": false} ],
      "limits": {"max_calls_per_minute": 30, "timeout_seconds": 120,
                 "max_results": 10}
    }

Nothing here is trusted blindly: :func:`parse_manifest` validates types and
returns a manifest whose unusable fields are dropped rather than crashing the
plugin scan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["PluginSetting", "PluginManifest", "parse_manifest",
           "manifest_from_plugin_dict", "FIELD_TYPES", "DEFAULT_LIMITS"]

FIELD_TYPES = ("string", "text", "bool", "int", "float", "choice", "secret",
               "model")

DEFAULT_LIMITS: dict[str, Any] = {
    "max_calls_per_minute": 60,
    "timeout_seconds": 120,
    "max_results": 25,
}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


@dataclass
class PluginSetting:
    """One typed, persisted field on the plugin's settings panel."""

    key: str
    label: str = ""
    type: str = "string"
    default: Any = ""
    help: str = ""
    choices: list[tuple[str, str]] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    advanced: bool = False

    @property
    def secret(self) -> bool:
        return self.type == "secret"

    def coerce(self, value: Any) -> Any:
        """Return *value* as this field's real type, clamped where bounded."""
        t = self.type
        if t == "bool":
            return _as_bool(value, bool(self.default))
        if t == "int":
            out = _as_int(value, _as_int(self.default, 0))
            if self.min is not None:
                out = max(int(self.min), out)
            if self.max is not None:
                out = min(int(self.max), out)
            return out
        if t == "float":
            out = _as_float(value, _as_float(self.default, 0.0))
            if self.min is not None:
                out = max(self.min, out)
            if self.max is not None:
                out = min(self.max, out)
            return out
        if t == "choice":
            text = _as_str(value, _as_str(self.default))
            allowed = {str(c[0]) for c in self.choices}
            return text if (not allowed or text in allowed) else _as_str(self.default)
        if t == "model":
            return _as_str(value, _as_str(self.default))
        return str(value if value is not None else self.default)


@dataclass
class PluginManifest:
    """The validated, typed description of one plugin."""

    id: str
    name: str
    description: str = ""
    icon: str = "✚"
    version: str = ""
    category: str = "general"
    builtin: bool = False
    permissions: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    #: At least one of these must be present (e.g. "any media editor").
    requires_any: list[str] = field(default_factory=list)
    parameters: dict = field(
        default_factory=lambda: {"type": "OBJECT", "properties": {}})
    settings: list[PluginSetting] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LIMITS))

    # ── helpers the UI / dispatcher use ──────────────────────────────────
    def setting(self, key: str) -> PluginSetting | None:
        for item in self.settings:
            if item.key == key:
                return item
        return None

    def default_settings(self) -> dict:
        return {item.key: item.default for item in self.settings}

    def coerce_settings(self, values: dict | None) -> dict:
        """Merge stored *values* over the defaults, each field typed."""
        stored = dict(values or {})
        out: dict = {}
        for item in self.settings:
            out[item.key] = item.coerce(
                stored.get(item.key, item.default))
        return out

    def advanced_settings(self) -> list[PluginSetting]:
        return [s for s in self.settings if s.advanced]


def _parse_settings(raw: Any) -> list[PluginSetting]:
    out: list[PluginSetting] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = _as_str(entry.get("key"))
        if not key or key in seen:
            continue
        seen.add(key)
        ftype = _as_str(entry.get("type"), "string").lower()
        if ftype not in FIELD_TYPES:
            ftype = "string"
        choices: list[tuple[str, str]] = []
        for choice in (entry.get("choices") or []):
            if isinstance(choice, (list, tuple)) and len(choice) >= 2:
                choices.append((str(choice[0]), str(choice[1])))
            elif choice is not None:
                choices.append((str(choice), str(choice)))

        def _bound(name: str):
            value = entry.get(name)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        default = entry.get("default", "")
        if ftype == "bool":
            default = _as_bool(default, False)
        elif ftype == "int":
            default = _as_int(default, 0)
        elif ftype == "float":
            default = _as_float(default, 0.0)
        else:
            default = _as_str(default)
        out.append(PluginSetting(
            key=key,
            label=_as_str(entry.get("label"), key.replace("_", " ").title()),
            type=ftype,
            default=default,
            help=_as_str(entry.get("help")),
            choices=choices,
            min=_bound("min"),
            max=_bound("max"),
            step=_bound("step"),
            advanced=_as_bool(entry.get("advanced"), False),
        ))
    return out


def _parse_limits(raw: Any) -> dict[str, Any]:
    limits = dict(DEFAULT_LIMITS)
    if isinstance(raw, dict):
        limits["max_calls_per_minute"] = max(
            1, _as_int(raw.get("max_calls_per_minute"),
                       DEFAULT_LIMITS["max_calls_per_minute"]))
        limits["timeout_seconds"] = max(
            1, _as_int(raw.get("timeout_seconds"),
                       DEFAULT_LIMITS["timeout_seconds"]))
        limits["max_results"] = max(
            1, _as_int(raw.get("max_results"), DEFAULT_LIMITS["max_results"]))
    return limits


def parse_manifest(raw: dict, *, fallback_id: str = "",
                   builtin: bool = False) -> PluginManifest | None:
    """Validate a raw manifest dict. Returns ``None`` without a usable name."""
    if not isinstance(raw, dict):
        return None
    name = _as_str(raw.get("name")) or _as_str(fallback_id)
    if not name:
        return None
    pid = _as_str(raw.get("id"), _as_str(fallback_id, name)) or name
    parameters = raw.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        parameters = {"type": "OBJECT", "properties": {}}
    perms = raw.get("permissions")
    permissions = ([str(p).strip().lower() for p in perms
                    if str(p).strip()] if isinstance(perms, list) else [])
    requires = raw.get("requires")
    if isinstance(requires, list):
        requirements = [str(r).strip().lower() for r in requires if str(r).strip()]
    else:
        requirements = []
    requires_any = raw.get("requires_any")
    if isinstance(requires_any, list):
        any_requirements = [str(r).strip().lower() for r in requires_any if str(r).strip()]
    else:
        any_requirements = []
    return PluginManifest(
        id=pid,
        name=name,
        description=_as_str(raw.get("description")),
        icon=_as_str(raw.get("icon"), "✚"),
        version=_as_str(raw.get("version")),
        category=_as_str(raw.get("category"), "general").lower(),
        builtin=bool(builtin or _as_bool(raw.get("builtin"), False)),
        permissions=permissions,
        requires=requirements,
        requires_any=any_requirements,
        parameters=parameters,
        settings=_parse_settings(raw.get("settings")),
        limits=_parse_limits(raw.get("limits")),
    )


def load_manifest_file(path: Path, *, fallback_id: str = "",
                       builtin: bool = False) -> PluginManifest | None:
    """Read and validate a ``plugin.json``. Never raises."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return parse_manifest(raw, fallback_id=fallback_id, builtin=builtin)


def manifest_from_plugin_dict(plugin_meta: dict, *, fallback_id: str = "",
                              builtin: bool = False,
                              filename: str = "") -> PluginManifest | None:
    """Adapt a legacy ``PLUGIN`` dict into a manifest.

    ``PLUGIN`` may now also carry the v2 keys (``icon``, ``permissions``,
    ``settings``, ``limits``, …), so a single-file plugin can opt into the richer
    panel without a separate ``plugin.json``.
    """
    if not isinstance(plugin_meta, dict):
        return None
    meta = dict(plugin_meta)
    # A v1 plugin names itself after the file stem when 'name' is absent; the
    # loader already reports that case, so keep the same fallback here.
    if "id" not in meta and filename:
        meta.setdefault("id", Path(filename).stem)
    return parse_manifest(meta, fallback_id=fallback_id or
                          (Path(filename).stem if filename else ""),
                          builtin=builtin)
