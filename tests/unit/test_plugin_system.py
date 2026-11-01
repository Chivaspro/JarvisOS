"""Plugin manifest v2, built-in discovery, settings/limits, and real plugins.

The plugin system must describe a plugin well enough to configure and bound it,
and the shipped built-ins must do real work. These tests cover the manifest
parser, discovery of ``plugins_builtin/`` through the same registry as user
plugins, the ``requires`` gate, typed settings, the per-minute call budget, and
the two network-backed built-ins (seo_analyzer against a local server, research
against a stubbed backend).
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import plugin_loader  # noqa: E402
from app.plugin_manifest import parse_manifest  # noqa: E402

BUILTIN = ROOT / "plugins_builtin"
_PAGE = """<!doctype html><html><head>
<title>Widgets for everyone — Acme</title>
<meta name="description" content="Buy widgets from Acme. Fast shipping and a
two year warranty on every order you place with us.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="/widgets">
</head><body>
<h1>Widgets</h1>
<h2>Why widgets</h2>
<h2>Pricing</h2>
<img src="a.png" alt="a widget">
<img src="b.png">
<p>%s</p>
<a href="/about">About</a>
<a href="https://example.org/ref">Partner</a>
<script type="application/ld+json">{}</script>
</body></html>""" % ("widget " * 320)


def _noop(_msg):
    pass


@pytest.fixture(autouse=True)
def _clear_call_log():
    """The per-minute budget is process-wide; isolate it between tests."""
    plugin_loader._CALL_LOG.clear()
    yield
    plugin_loader._CALL_LOG.clear()


# ── manifest ─────────────────────────────────────────────────────────────────

def test_manifest_parses_types_defaults_and_limits():
    manifest = parse_manifest({
        "id": "demo", "name": "demo", "description": "Demo.",
        "icon": "🧪", "category": "Testing",
        "permissions": ["Network", "filesystem"],
        "requires": ["FFmpeg"],
        "settings": [
            {"key": "count", "type": "int", "default": 5, "min": 1, "max": 10},
            {"key": "mode", "type": "choice", "default": "fast",
             "choices": [["fast", "Fast"], ["slow", "Slow"]]},
            {"key": "token", "type": "secret"},
            {"key": "enabled", "type": "bool", "default": True},
        ],
        "limits": {"max_calls_per_minute": 7, "timeout_seconds": 30},
    })
    assert manifest is not None
    assert manifest.category == "testing"
    assert manifest.permissions == ["network", "filesystem"]
    assert manifest.requires == ["ffmpeg"]
    assert manifest.limits["max_calls_per_minute"] == 7
    assert manifest.limits["max_results"] == 25     # default kept
    assert manifest.setting("token").secret is True
    # Coercion clamps and types.
    assert manifest.coerce_settings({"count": 99})["count"] == 10
    assert manifest.coerce_settings({})["enabled"] is True
    assert manifest.coerce_settings({"mode": "evil"})["mode"] == "fast"


def test_manifest_without_a_name_is_rejected():
    assert parse_manifest({"description": "no name"}) is None


# ── discovery ────────────────────────────────────────────────────────────────

def test_builtin_plugins_are_discovered_with_manifests(tmp_path):
    registry = plugin_loader.discover_plugins(
        tmp_path / "plugins", core_tool_names=set(), logger=_noop,
        extra_dirs=[BUILTIN])
    rows = {row["name"]: row for row in registry.list_for_ui()}
    for name in ("research", "seo_analyzer", "automation", "agent_team"):
        assert name in rows, f"built-in missing: {name}"
    for name in ("research", "seo_analyzer", "automation", "agent_team"):
        row = rows[name]
        assert row["valid"], row["error"]
        assert row["builtin"] is True
        assert row["icon"]
        assert row["permissions"], "a built-in must declare its permissions"
        assert row["settings_schema"], "a built-in must describe its settings"
        assert row["limits"]["timeout_seconds"] >= 1


def test_manifest_only_plugin_needs_no_plugin_dict(tmp_path):
    folder = tmp_path / "plugins" / "echoer"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({
        "id": "echoer", "name": "echoer",
        "description": "Echoes its input.",
        "parameters": {"type": "OBJECT",
                       "properties": {"text": {"type": "STRING"}}},
    }), encoding="utf-8")
    (folder / "echoer.py").write_text(
        "def run(parameters):\n"
        "    return 'echo:' + str((parameters or {}).get('text', ''))\n",
        encoding="utf-8")

    registry = plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                              logger=_noop)
    assert registry.has("echoer")
    assert registry.run("echoer", {"text": "hi"}) == "echo:hi"


def test_requires_a_missing_app_is_not_offered(tmp_path):
    folder = tmp_path / "plugins" / "needs_app"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({
        "id": "needs_app", "name": "needs_app",
        "description": "Needs software that is not installed.",
        "requires": ["definitely_not_installed_xyz"],
    }), encoding="utf-8")
    (folder / "needs_app.py").write_text("def run(parameters):\n    return 'x'\n",
                                         encoding="utf-8")

    registry = plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                              logger=_noop)
    assert not registry.has("needs_app")
    rows = {row["name"]: row for row in registry.list_for_ui()}
    assert rows["needs_app"]["valid"] is False
    assert "not installed" in rows["needs_app"]["error"]


# ── settings and limits ──────────────────────────────────────────────────────

def _settings_plugin(tmp_path, limits=None):
    folder = tmp_path / "plugins" / "tuner"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({
        "id": "tuner", "name": "tuner", "description": "Reads its settings.",
        "settings": [{"key": "gain", "type": "int", "default": 5,
                      "min": 0, "max": 100}],
        "limits": limits or {"max_calls_per_minute": 60},
    }), encoding="utf-8")
    (folder / "tuner.py").write_text(
        "def run(parameters, settings=None, limits=None):\n"
        "    return 'gain=' + str((settings or {}).get('gain'))\n",
        encoding="utf-8")
    return plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                          logger=_noop)


def test_run_passes_typed_settings(tmp_path, monkeypatch):
    from memory import config_manager
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "api_keys.json")
    registry = _settings_plugin(tmp_path)
    assert registry.run("tuner", {}) == "gain=5"
    config_manager.save_plugin_settings("tuner", {"gain": 42})
    assert registry.run("tuner", {}) == "gain=42"


def test_run_enforces_the_per_minute_budget(tmp_path, monkeypatch):
    from memory import config_manager
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "api_keys.json")
    registry = _settings_plugin(tmp_path, {"max_calls_per_minute": 2})
    assert registry.run("tuner", {}).startswith("gain=")
    assert registry.run("tuner", {}).startswith("gain=")
    refused = registry.run("tuner", {})
    assert "limit of 2 calls per minute" in refused


# ── the real built-ins ───────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = _PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def page_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def test_seo_analyzer_measures_a_real_page(page_url, tmp_path):
    registry = plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                              logger=_noop, extra_dirs=[BUILTIN])
    report = registry.run("seo_analyzer", {"url": page_url})
    assert "SEO report for" in report
    assert "Widgets for everyone" in report        # the real <title>
    assert "1 — ['Widgets']" in report             # the real H1
    assert "without alt" in report                 # the real img without alt
    assert "1 internal, 1 external" in report


def test_seo_analyzer_refuses_an_unreachable_page(tmp_path):
    registry = plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                              logger=_noop, extra_dirs=[BUILTIN])
    report = registry.run("seo_analyzer",
                          {"url": "http://127.0.0.1:1/definitely-closed"})
    assert "Could not fetch" in report
    assert "no SEO report was produced" in report


def test_research_returns_the_real_backend_output(tmp_path, monkeypatch):
    import actions.web_search as web_search

    def _fake_search(parameters, *_a, **_k):
        return f"REAL-RESULT for {parameters.get('query')}"

    monkeypatch.setattr(web_search, "web_search", _fake_search)
    # Force the Agent Reach path off so the fallback is exercised deterministically.
    monkeypatch.setattr("capabilities.agent_reach.available", lambda *a, **k: False)

    registry = plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                              logger=_noop, extra_dirs=[BUILTIN])
    out = registry.run("research", {"query": "fusion energy"})
    assert "REAL-RESULT for fusion energy" in out


def _builtin_registry(tmp_path):
    return plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                          logger=_noop, extra_dirs=[BUILTIN])


def test_automation_saves_runs_and_records_history(tmp_path, monkeypatch):
    from config import settings as cfg
    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path / "data")
    import capabilities.sequence as sequence
    calls = []

    def _fake_sequence(args, **_kw):
        calls.append(args)
        return {"ok": True, "output": f"ran {len(args.get('steps') or [])} step(s)"}

    monkeypatch.setattr(sequence, "run", _fake_sequence)

    registry = _builtin_registry(tmp_path)
    steps = json.dumps([{"tool": "pc_control", "action": "open_app", "app": "notepad"}])
    saved = registry.run("automation", {"action": "save", "name": "notes",
                                        "steps": steps})
    assert "Saved workflow 'notes'" in saved
    listed = registry.run("automation", {"action": "list"})
    assert "notes" in listed

    ran = registry.run("automation", {"action": "run_saved", "name": "notes"})
    assert "ran 1 step(s)" in ran
    assert calls and calls[0]["steps"][0]["tool"] == "pc_control"

    history = registry.run("automation", {"action": "history"})
    assert "notes" in history and "OK" in history


def test_automation_condition_gates_the_run(tmp_path, monkeypatch):
    from config import settings as cfg
    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path / "data")
    # Only the condition flag is off; the plugin's own permission still passes.
    monkeypatch.setattr(cfg, "feature_enabled",
                        lambda name: str(name) != "narrator")
    import capabilities.sequence as sequence
    monkeypatch.setattr(sequence, "run",
                        lambda *a, **k: pytest.fail("the workflow must not run"))
    registry = _builtin_registry(tmp_path)
    out = registry.run("automation", {
        "action": "run",
        "steps": json.dumps([{"tool": "pc_control", "action": "open_app"}]),
        "condition": "narrator"})
    assert "Skipped" in out and "switched off" in out


def test_agent_team_researcher_delegates_to_the_real_capability(tmp_path, monkeypatch):
    import capabilities.router as router
    seen = {}

    def _fake_research(args, *_a, **_k):
        seen.update(args)
        return "REAL RESEARCH ANSWER"

    monkeypatch.setattr(router, "handle_web_research", _fake_research)
    registry = _builtin_registry(tmp_path)
    out = registry.run("agent_team", {"role": "researcher", "task": "fusion"})
    assert out == "REAL RESEARCH ANSWER"
    assert seen.get("query") == "fusion"


def test_agent_team_developer_refuses_to_bypass_approval(tmp_path):
    registry = _builtin_registry(tmp_path)
    out = registry.run("agent_team", {"role": "developer", "task": "write a parser"})
    assert "approval" in out.lower()
    assert "does not write code" in out


def test_research_states_the_youtube_licence_when_media_is_involved(tmp_path, monkeypatch):
    import capabilities.agent_reach as reach

    monkeypatch.setattr(reach, "available", lambda *a, **k: True)
    monkeypatch.setattr(reach, "research", lambda **kw: {
        "ok": True, "findings": [{"title": "A clip", "url": "https://youtu.be/x",
                                  "source": "youtube", "snippet": "s"}],
        "output": "", "reason": ""})
    import actions.web_search as web_search
    monkeypatch.setattr(web_search, "web_search", lambda *a, **k: "web answer")

    registry = plugin_loader.discover_plugins(tmp_path / "plugins", set(),
                                              logger=_noop, extra_dirs=[BUILTIN])
    out = registry.run("research", {"query": "kittens", "platform": "youtube"})
    assert "https://youtu.be/x" in out
    assert "belong to their owners" in out
