"""
Plugin discovery, validation, collision detection, and dispatch.

Discovery runs once (JarvisLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.
"""
from __future__ import annotations

import importlib.util
import hashlib
import inspect
import re
import sys
import threading
import time
import traceback
import types
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from memory.config_manager import (
    get_plugin_enabled,
    get_plugin_limits,
    get_plugin_settings,
)
from app import plugin_requires
from app.plugin_manifest import (
    PluginManifest,
    load_manifest_file,
    manifest_from_plugin_dict,
)

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}

#: The built-in plugins ship with the app and are discovered through the exact
#: same manifest/registry path as anything dropped into ``plugins/``.
BUILTIN_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins_builtin"

#: Manifest permission -> the real feature flag that gates it. A permission the
#: user has switched off (e.g. desktop control) makes the plugin refuse to run,
#: instead of the declaration being decorative.
_PERMISSION_FEATURE = {
    "filesystem": "file_control",
    "desktop": "desktop_control",
    "automation": "desktop_control",
    "microphone": "mic_access",
    "camera": "camera_access",
    "browser": "agent_tars",
}


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    # ── manifest v2 ──────────────────────────────────────────────────────
    plugin_id: str = ""
    manifest: Optional[PluginManifest] = None
    builtin: bool = False
    requires: list = field(default_factory=list)
    requires_ok: bool = True
    requires_detail: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.plugin_id:
            self.plugin_id = self.name


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None]):
        self._plugins = plugins          # name -> PluginRecord, VALID entries only
        self._all_records: list[PluginRecord] = []   # valid + invalid, for UI listing
        self._logger = logger
        #: Core tool names, so a plugin that arrives *after* startup (a skill
        #: forged by evolution/vault.py) is held to exactly the same
        #: no-collision rule the startup scan applies.
        self._core_tool_names: set[str] = set()

    # -- called by evolution/vault.py when a new skill is forged at runtime --
    def register(self, rec: PluginRecord) -> tuple[bool, str]:
        """Install a validated record discovered after startup.

        Hot-registration is what makes a forged skill usable without a restart:
        the next ``get_tool_declarations()`` (i.e. the next Live connect) already
        contains it, and ``run()`` dispatches it immediately. Re-registering an
        existing name replaces that plugin's code — the forged skill is
        re-validated by the caller first.
        """
        if not rec.valid or rec.run is None:
            return False, rec.error or "the plugin record is not usable"
        if rec.name in self._core_tool_names:
            return False, f"'{rec.name}' collides with a core tool — rejected"
        other = self._plugins.get(rec.name)
        if other is not None and other.file != rec.file:
            return False, (f"'{rec.name}' is already provided by plugin "
                           f"'{other.file}' — rejected")
        self._plugins[rec.name] = rec
        self._all_records = [r for r in self._all_records if r.name != rec.name]
        self._all_records.append(rec)
        self._logger(f"Plugin hot-registered: {rec.name} ({rec.file})")
        return True, f"'{rec.name}' is live"

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decls.append({
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                })
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    # -- called by main.py from _execute_tool's else branch --
    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not get_plugin_enabled(name):
            return f"The '{name}' plugin is currently disabled."
        blocked = _permission_block(rec)
        if blocked:
            return blocked
        limits = effective_limits(rec)
        over = _rate_limited(rec.plugin_id, limits)
        if over:
            return over
        settings = effective_settings(rec)
        timeout = float(limits.get("timeout_seconds") or 120)
        try:
            return _run_with_timeout(
                rec, parameters, settings, limits, timeout,
                player, session_memory, self._logger)
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}")
            traceback.print_exc()
            return f"Sir, the '{name}' plugin failed: {e}"

    # -- called by ui.py's Plugin Manager overlay and Settings ▸ PLUGINS --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            manifest = rec.manifest
            settings_schema = []
            if manifest is not None:
                settings_schema = [
                    {"key": s.key, "label": s.label, "type": s.type,
                     "default": s.default, "help": s.help,
                     "choices": [list(c) for c in s.choices],
                     "min": s.min, "max": s.max, "step": s.step,
                     "advanced": s.advanced}
                    for s in manifest.settings
                ]
            out.append({
                "name": rec.name,
                "id": rec.plugin_id,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
                "builtin": bool(rec.builtin),
                "icon": (manifest.icon if manifest else "✚"),
                "category": (manifest.category if manifest else "general"),
                "version": (manifest.version if manifest else ""),
                "permissions": list(manifest.permissions) if manifest else [],
                "requires": list(rec.requires),
                "requires_ok": bool(rec.requires_ok),
                "requires_detail": list(rec.requires_detail),
                "settings_schema": settings_schema,
                "settings": effective_settings(rec) if rec.valid else {},
                "limits": effective_limits(rec) if rec.valid else {},
                "permission_block": _permission_block(rec) if rec.valid else "",
            })
        return out

    # -- used by the Settings panel to save one plugin's values ---------
    def record(self, name: str) -> Optional[PluginRecord]:
        return self._plugins.get(name)

    # -- used by diagnostics: every discovered record, valid or rejected ----
    def records(self) -> list[PluginRecord]:
        """Every record the scan produced, valid and rejected, as a copy."""
        return list(self._all_records)


def effective_settings(rec: PluginRecord) -> dict:
    """The plugin's stored values, typed and defaulted by its manifest."""
    manifest = rec.manifest
    if manifest is None:
        return {}
    return manifest.coerce_settings(get_plugin_settings(rec.plugin_id))


def effective_limits(rec: PluginRecord) -> dict:
    """Manifest limits with the user's saved overrides applied and typed."""
    manifest = rec.manifest
    base = dict(manifest.limits) if manifest else {}
    if not base:
        return {}
    saved = get_plugin_limits(rec.plugin_id)
    out = dict(base)
    for key in ("max_calls_per_minute", "timeout_seconds", "max_results"):
        if key in saved:
            try:
                out[key] = max(1, int(saved[key]))
            except (TypeError, ValueError):
                pass
    return out


def _permission_block(rec: PluginRecord) -> str:
    """A refusal sentence when a declared permission is switched off, else ''."""
    manifest = rec.manifest
    if manifest is None or not manifest.permissions:
        return ""
    try:
        from config import settings as cfg
    except Exception:
        return ""
    for permission in manifest.permissions:
        feature = _PERMISSION_FEATURE.get(str(permission).lower())
        if not feature:
            continue
        try:
            allowed = bool(cfg.feature_enabled(feature))
        except Exception:
            allowed = True
        if not allowed:
            return (f"Sir, the '{rec.name}' plugin needs the '{permission}' "
                    f"permission, which is switched off in Settings "
                    f"({feature}). No work was done.")
    return ""


_CALL_LOG: dict[str, deque] = {}
_CALL_LOCK = threading.Lock()
# ``concurrent.futures`` is imported on first use: it is one of the heaviest
# stdlib imports and plugin discovery runs at startup, so paying for it when no
# plugin ever declares a timeout budget would be a real startup cost.
_EXECUTOR = None


def _executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        import concurrent.futures as _futures
        _EXECUTOR = _futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="jarvis-plugin")
    return _EXECUTOR


def _rate_limited(plugin_id: str, limits: dict) -> str:
    """Refuse a call that would exceed the plugin's per-minute budget."""
    if not limits:
        return ""
    per_minute = int(limits.get("max_calls_per_minute") or 0)
    if per_minute <= 0:
        return ""
    now = time.monotonic()
    with _CALL_LOCK:
        log = _CALL_LOG.setdefault(plugin_id, deque())
        while log and now - log[0] > 60.0:
            log.popleft()
        if len(log) >= per_minute:
            return (f"Sir, '{plugin_id}' has reached its limit of {per_minute} "
                    f"calls per minute. Try again shortly.")
        log.append(now)
    return ""


def _call_run(run_fn, parameters, player, session_memory, settings=None,
              limits=None, logger=None):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for key, value in (("player", player), ("session_memory", session_memory),
                       ("settings", settings), ("limits", limits),
                       ("logger", logger)):
        if value is not None and (has_var_kw or key in sig.parameters):
            kwargs[key] = value
    if not has_var_kw and "settings" in sig.parameters and settings is None:
        kwargs["settings"] = {}
    return run_fn(parameters, **kwargs)


def _run_with_timeout(rec: PluginRecord, parameters, settings, limits,
                      timeout: float, player, session_memory, logger) -> str:
    """Run one plugin call under its manifest timeout budget."""
    def _invoke():
        return _call_run(rec.run, parameters, player, session_memory,
                         settings=settings, limits=limits, logger=logger)

    if timeout <= 0 or not limits:
        return _invoke() or "Done."
    import concurrent.futures as _futures
    future = _executor().submit(_invoke)
    try:
        return future.result(timeout=timeout) or "Done."
    except _futures.TimeoutError:
        future.cancel()
        logger(f"Plugin '{rec.name}' exceeded its {timeout:.0f}s budget.")
        return (f"Sir, the '{rec.name}' plugin did not finish within "
                f"{timeout:.0f} seconds and was abandoned. No result was "
                f"recorded from it.")


def _validate(module, filename: str, manifest: PluginManifest | None = None,
              *, builtin: bool = False) -> PluginRecord:
    """Returns a PluginRecord; .valid=False + .error set on any problem. Never raises.

    A plugin is valid when it exposes a callable ``run`` and either a ``PLUGIN``
    dict or a manifest provides a usable name and description.
    """
    plugin_meta = getattr(module, "PLUGIN", None)
    if manifest is None and isinstance(plugin_meta, dict):
        manifest = manifest_from_plugin_dict(
            plugin_meta, fallback_id=Path(filename).stem, builtin=builtin,
            filename=filename)
    if manifest is None and not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                            error="Missing PLUGIN dict constant and no plugin.json manifest.")

    if manifest is not None:
        name = manifest.name
        description = manifest.description
        parameters = manifest.parameters
        if not description.strip():
            return PluginRecord(name=name, file=filename, manifest=manifest,
                                plugin_id=manifest.id, builtin=builtin,
                                error="PLUGIN['description'] missing or empty.")
        # A legacy PLUGIN dict still gets its raw parameters validated: the
        # manifest only normalises, and silently accepting a malformed tool
        # signature would offer the model a tool it cannot call.
        raw_params = plugin_meta.get("parameters") if isinstance(plugin_meta, dict) else None
        if raw_params is not None and (
                not isinstance(raw_params, dict)
                or raw_params.get("type") != "OBJECT"):
            return PluginRecord(name=name, file=filename, manifest=manifest,
                                plugin_id=manifest.id, builtin=builtin,
                                error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")
    else:
        name = plugin_meta.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                                error="PLUGIN['name'] missing or not a valid identifier "
                                      "(letters/digits/underscore, must start with letter/underscore).")
        description = plugin_meta.get("description")
        if not isinstance(description, str) or not description.strip():
            return PluginRecord(name=name, file=filename,
                                error="PLUGIN['description'] missing or empty.")
        parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)

    if not _NAME_RE.match(str(name)):
        return PluginRecord(name=str(name), file=filename, manifest=manifest,
                            plugin_id=(manifest.id if manifest else str(name)),
                            builtin=builtin,
                            error="Plugin name is not a valid identifier "
                                  "(letters/digits/underscore).")
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename, manifest=manifest,
                            plugin_id=(manifest.id if manifest else name),
                            builtin=builtin,
                            error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename, manifest=manifest,
                            plugin_id=(manifest.id if manifest else name),
                            builtin=builtin,
                            error="Missing callable run(parameters, ...) function.")

    requires = list(manifest.requires) if manifest else []
    requires_any = list(manifest.requires_any) if manifest else []
    requires_ok, requires_detail = plugin_requires.satisfied(requires)
    any_ok, any_detail = plugin_requires.satisfied_any(requires_any)
    rec = PluginRecord(
        name=name, description=str(description).strip(), parameters=parameters,
        run=run_fn, file=filename, valid=True, error="",
        plugin_id=(manifest.id if manifest else name), manifest=manifest,
        builtin=builtin, requires=[*requires, *requires_any],
        requires_ok=(requires_ok and any_ok),
        requires_detail=[*requires_detail, *any_detail])
    if not requires_ok:
        # "Integrations only when the app exists": the tool is not offered, and
        # the reason is a fact about this machine, not a guess.
        rec.valid = False
        rec.error = ("Requires " + plugin_requires.describe(requires)
                     + " — not installed.")
    elif not any_ok:
        rec.valid = False
        rec.error = ("Requires " + any_detail[0] + " — none found on this "
                     "machine.")
    return rec


def _safe_module_part(value: str) -> str:
    """Turn a plugin directory name into a valid, collision-resistant module part."""
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value)).strip("_") or "plugin"
    if value[0].isdigit():
        value = f"p_{value}"
    return value[:48]


def _ensure_package(name: str, path: Path) -> None:
    """Create a synthetic package for a plugin directory.

    Plugin folders are allowed to contain spaces and do not need an
    ``__init__.py``. Creating a small package object lets a plugin keep its real
    relative imports without requiring the user's
    folder to be importable as a normal Python package.
    """
    if name in sys.modules:
        return
    package = types.ModuleType(name)
    package.__path__ = [str(path)]  # type: ignore[attr-defined]
    package.__package__ = name
    sys.modules[name] = package


def _load_plugin_module(path: Path, plugins_dir: Path):
    """Load a direct or nested plugin with its sibling imports intact."""
    rel = path.relative_to(plugins_dir)
    parent = rel.parent
    root_name = "_jarvis_discovered_plugins"
    _ensure_package(root_name, plugins_dir)

    if parent == Path("."):
        package_name = root_name
    else:
        token = "_".join(_safe_module_part(part) for part in parent.parts)
        digest = hashlib.sha1(str(parent).encode("utf-8")).hexdigest()[:8]
        package_name = f"{root_name}.{token}_{digest}"
        _ensure_package(package_name, path.parent)

    module_digest = hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:10]
    module_name = f"{package_name}.{_safe_module_part(path.stem)}_{module_digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("could not build import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Plugins make their own sibling helpers importable by appending their
    # folder to sys.path at import time. FileFinder caches each sys.path entry's
    # directory listing, and __pycache__ files written by the concurrent
    # background import thread land in that directory *after* it was cached —
    # which surfaced as an intermittent "No module named <sibling>"
    # rejection naming a different sibling on every run. Refreshing the caches
    # (and retrying once, in case a helper was mid-write) makes discovery
    # deterministic without any plugin needing to know about it.
    importlib.invalidate_caches()
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError:
        importlib.invalidate_caches()
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _manifest_for(path: Path, *, builtin: bool) -> PluginManifest | None:
    """The manifest beside this module: its own folder, or the folder above.

    For ``plugins/<name>/plugin.py`` the sidecar sits in the same folder. For a
    single-file ``plugins/<name>.py`` the folder above is also checked so a
    drop-in manifest works either way.
    """
    candidates = [path.parent / "plugin.json"]
    if path.parent.parent != path.parent:
        candidates.append(path.parent.parent / "plugin.json")
    for candidate in candidates:
        if candidate.is_file():
            manifest = load_manifest_file(candidate, fallback_id=path.stem,
                                          builtin=builtin)
            if manifest is not None:
                return manifest
    return None


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print,
                      extra_dirs=None) -> PluginRegistry:
    """
    Scans plugins_dir (plus any ``extra_dirs``, e.g. the shipped built-ins) and
    its plugin folders for *.py files. Files starting with '_' (e.g.
    __init__.py, _template.py, and shared helpers) are skipped. Import errors,
    validation errors, and name collisions are logged and the offending file is
    skipped — they NEVER raise out of this function and never abort the scan of
    remaining files.
    """
    search: list[tuple[Path, bool]] = [(Path(plugins_dir), False)]
    for extra in (extra_dirs or []):
        search.append((Path(extra), Path(extra) == BUILTIN_PLUGINS_DIR))

    valid: dict[str, PluginRecord] = {}
    all_records: list[PluginRecord] = []

    for root, builtin in search:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            continue
        files = sorted(
            (
                path for path in root.rglob("*.py")
                if "__pycache__" not in path.parts
                and not any(part.startswith(".")
                            for part in path.relative_to(root).parts)
            ),
            key=lambda p: str(p.relative_to(root)).lower(),
        )
        for path in files:
            if path.name.startswith("_"):
                continue
            relative_name = str(path.relative_to(root))
            if builtin:
                relative_name = f"plugins_builtin/{relative_name}"
            try:
                module = _load_plugin_module(path, root)
                manifest = _manifest_for(path, builtin=builtin)
                rec = _validate(module, relative_name, manifest, builtin=builtin)

                if rec.valid and rec.name in core_tool_names:
                    rec.valid = False
                    rec.error = f"Name '{rec.name}' collides with a core tool — rejected."
                elif rec.valid and rec.name in valid:
                    other = valid[rec.name].file
                    rec.valid = False
                    rec.error = f"Name '{rec.name}' already used by plugin '{other}' — rejected."

            except Exception as e:
                rec = PluginRecord(name=path.stem, file=relative_name,
                                    error=f"Failed to load: {e}")
                traceback.print_exc()

            all_records.append(rec)
            if rec.valid:
                valid[rec.name] = rec
                logger(f"Plugin loaded: {rec.name} ({path.name})")
            else:
                logger(f"Plugin rejected: {path.name} — {rec.error}")

    registry = PluginRegistry(valid, logger)
    registry._all_records = all_records
    registry._core_tool_names = set(core_tool_names or ())
    logger(f"Plugin discovery complete: {len(valid)} active, "
           f"{len(all_records) - len(valid)} rejected, {len(all_records)} total.")
    return registry


# ── runtime registration (self-evolution) ─────────────────────────────────────
#
# Discovery runs once at startup, so a plugin file written later would sit on
# disk unseen until the next launch. The self-evolution subsystem therefore
# publishes its registry here: a skill forged mid-session is validated with the
# exact same rules and installed into the live registry, which is what makes
# "writes a brand-new tool and uses it immediately" true without a restart.

_RUNTIME_REGISTRY: "PluginRegistry | None" = None


def set_runtime_registry(registry: "PluginRegistry | None") -> None:
    """Publish the live registry for hot-registration (called from main.py)."""
    global _RUNTIME_REGISTRY
    _RUNTIME_REGISTRY = registry


def get_runtime_registry() -> "PluginRegistry | None":
    return _RUNTIME_REGISTRY


def register_runtime_plugin(path: Path, plugins_dir: Path | None = None) -> tuple[bool, str]:
    """Load, validate and hot-register one plugin ``.py`` written at runtime.

    Returns ``(ok, message)`` and never raises: a rejected file is reported with
    the same sentence the startup scan would have used.
    """
    registry = _RUNTIME_REGISTRY
    if registry is None:
        return False, "the plugin registry is not running yet"

    path = Path(path)
    root = Path(plugins_dir) if plugins_dir else path.parent.parent
    if not path.exists():
        return False, f"no such plugin file: {path}"
    try:
        relative_name = str(path.relative_to(root))
    except ValueError:
        relative_name = path.name

    try:
        module = _load_plugin_module(path, root)
        manifest = _manifest_for(path, builtin=(root == BUILTIN_PLUGINS_DIR))
        rec = _validate(module, relative_name, manifest,
                        builtin=(root == BUILTIN_PLUGINS_DIR))
    except Exception as exc:
        return False, f"failed to load: {exc}"

    if not rec.valid:
        return False, rec.error or "the plugin failed validation"
    return registry.register(rec)
