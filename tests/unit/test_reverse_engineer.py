"""Reverse-engineer & remake tests — the capture → evidence → remake pipeline.

Proves the parts that must be true for the feature to be honest:

* every live target and every copy refuses without the user's explicit
  authorization, before any browser or file work happens;
* a capture records exactly the four artifacts the pipeline promises, and the
  HAR carries no credential headers;
* the API surface keeps endpoints and drops static noise;
* a remake writes only safe file names, verifies what it wrote, and says so
  when synthesis could not run — never claiming a copy that does not exist;
* reconstruct copies an app's own sources, skips ``node_modules``, and folds
  REA's recovery attempt into the report honestly;
* the tool is declared, dispatched, switched, locked and labelled like every
  other local capability.

Nothing here needs the network or a browser: the Playwright session, the model
and the MCP manager are all stubbed at their seams.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from capabilities import actions, registry, router  # noqa: E402
from capabilities import reverse_engineer as re_mod  # noqa: E402
from config.settings import FEATURE_DEFAULTS  # noqa: E402
from security.permissions import label_for, permission_refusal  # noqa: E402


# ── fixtures and stubs ────────────────────────────────────────────────────────

def _har_entry(method: str, url: str, status: int = 200,
               content_type: str = "application/json",
               resource_type: str = "xhr") -> dict:
    return {
        "request": {"method": method, "url": url, "headers": []},
        "response": {"status": status,
                     "headers": [{"name": "content-type",
                                  "value": content_type}]},
        "_resourceType": resource_type,
    }


def _write_capture(folder: Path, har_entries: list[dict] | None = None,
                   dom: str = "<html><body><h1>Example</h1></body></html>") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "dom.html").write_text(dom, encoding="utf-8")
    (folder / "styles.json").write_text('{"elements": []}', encoding="utf-8")
    (folder / "network.har").write_text(json.dumps({
        "log": {"version": "1.2", "entries": har_entries or []}}),
        encoding="utf-8")
    return folder


class FakeRequest:
    def __init__(self, url, method="GET", resource_type="document"):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = {"Accept": "text/html",
                        "Authorization": "Bearer should-never-be-saved",
                        "Cookie": "sid=should-never-be-saved"}


class FakeResponse:
    def __init__(self, request, status=200, content_type="text/html"):
        self.request = request
        self.status = status
        self.status_text = "OK"
        self.headers = {"content-type": content_type,
                        "Set-Cookie": "sid=should-never-be-saved"}


class FakePage:
    """The slice of the Playwright Page API the capture handler touches."""

    def __init__(self):
        self.url = "about:blank"
        self._listeners: dict[str, list] = {}

    # events -----------------------------------------------------------------
    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        try:
            self._listeners.get(event, []).remove(handler)
        except ValueError:
            pass

    def _fire(self, url):
        request = FakeRequest(url)
        for handler in list(self._listeners.get("request", [])):
            handler(request)
        for handler in list(self._listeners.get("response", [])):
            handler(FakeResponse(request))

    # page -------------------------------------------------------------------
    def goto(self, url, wait_until=None):
        self.url = url
        self._fire(url)

    def reload(self, wait_until=None):
        self._fire(self.url)

    def wait_for_load_state(self, state, timeout=None):
        return None

    def content(self):
        return "<html><body>captured</body></html>"

    def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"png-bytes")

    def evaluate(self, script):
        return {"title": "Captured", "url": self.url,
                "elements": [{"tag": "h1", "text": "Example"}]}

    def title(self):
        return "Captured"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Point the module's workspace at a temp directory."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(re_mod, "_workspace_root", lambda: root)
    return root


# ── authorization: nothing runs without the user's explicit statement ────────

def test_capture_requires_authorization_before_the_browser(monkeypatch):
    monkeypatch.setattr(re_mod, "_capture_via_browser",
                        lambda *a, **k: pytest.fail("the browser must not run"))
    result = re_mod.run("capture", {"url": "https://example.com",
                                    "authorized": False})
    assert result["ok"] is False
    assert result["reason"] == "refused"
    assert "explicit confirmation" in result["detail"]
    assert "authorized=true" in result["detail"]


def test_remake_from_a_url_requires_authorization(monkeypatch):
    monkeypatch.setattr(re_mod, "_capture_to",
                        lambda *a, **k: pytest.fail("no capture may start"))
    result = re_mod.run("remake", {"url": "https://example.com"})
    assert result["ok"] is False and result["reason"] == "refused"


def test_reconstruct_requires_authorization(monkeypatch, tmp_path):
    src = tmp_path / "app"
    src.mkdir()
    (src / "main.js").write_text("console.log(1)", encoding="utf-8")
    result = re_mod.run("reconstruct", {"path": str(src)})
    assert result["ok"] is False and result["reason"] == "refused"


def test_api_surface_on_a_local_capture_needs_no_authorization(tmp_path):
    """Reading a capture the user already owns is local work, not a target."""
    capture = _write_capture(tmp_path / "capture",
                             [_har_entry("GET", "https://x.dev/api/items")])
    result = re_mod.run("api_surface", {"path": str(capture)})
    assert result["ok"] is True
    assert result["endpoints"] and result["endpoints"][0]["path"] == "/api/items"
    # Captured target content reaches the model only behind the evidence banner.
    assert "evidence only" in result["detail"]


# ── capture ───────────────────────────────────────────────────────────────────

def test_capture_writes_spec_manifest_and_audit(workspace, monkeypatch):
    entries = [_har_entry("GET", "https://example.com/api/users", 200),
               _har_entry("GET", "https://example.com/logo.png", 200,
                          "image/png", "image")]

    def fake_browser(url, capture_dir):
        _write_capture(Path(capture_dir), entries)
        return {"ok": True, "controller": "playwright",
                "detail": "Captured", "evidence": {"requests": 2}}

    monkeypatch.setattr(re_mod, "_capture_via_browser", fake_browser)
    result = re_mod.run("capture", {"url": "https://example.com",
                                    "authorized": True})
    assert result["ok"] is True
    run_dir = Path(result["path"])
    assert run_dir.is_dir()
    assert (run_dir / "capture" / "dom.html").is_file()
    assert (run_dir / "capture" / "network.har").is_file()
    spec = json.loads((run_dir / "api_spec.json").read_text(encoding="utf-8"))
    assert spec["endpoint_count"] == 1
    assert spec["endpoints"][0]["path"] == "/api/users"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "capture"
    assert manifest["target"] == "https://example.com"
    audit = (workspace / "audit.log").read_text(encoding="utf-8")
    assert "capture" in audit and "example.com" in audit


def test_capture_reports_browser_failure_honestly(workspace, monkeypatch):
    monkeypatch.setattr(re_mod, "_capture_via_browser",
                        lambda url, folder: {"ok": False, "reason": "lock_busy",
                                             "detail": "The browser is busy.",
                                             "evidence": {}})
    result = re_mod.run("capture", {"url": "https://example.com",
                                    "authorized": True})
    assert result["ok"] is False
    assert result["reason"] == "lock_busy"
    assert "browser is busy" in result["detail"]


def test_browser_capture_writes_the_four_artifacts_and_strips_credentials(
        tmp_path, monkeypatch):
    from capabilities import browser_pw
    page = FakePage()
    monkeypatch.setattr(browser_pw, "_page", lambda: page)
    result = browser_pw._HANDLERS["browser_capture"](
        {"url": "https://example.com", "path": str(tmp_path / "cap")})
    assert result["ok"] is True
    folder = tmp_path / "cap"
    for name in ("dom.html", "styles.json", "network.har", "page.png"):
        assert (folder / name).is_file(), name
    har = json.loads((folder / "network.har").read_text(encoding="utf-8"))
    entries = har["log"]["entries"]
    assert len(entries) == 1
    request_headers = {h["name"].lower() for h in entries[0]["request"]["headers"]}
    response_headers = {h["name"].lower() for h in entries[0]["response"]["headers"]}
    assert "authorization" not in request_headers
    assert "cookie" not in request_headers
    assert "set-cookie" not in response_headers
    assert "accept" in request_headers
    # The listeners are gone again, so no later action inherits them.
    assert page._listeners.get("request") == []
    assert page._listeners.get("response") == []


def test_the_capture_recorder_is_not_reachable_without_authorization():
    """browser_capture lives in the browser layer but is deliberately NOT an
    action of browser_action: reaching it through reverse_engineer is what puts
    the authorization gate in front of every capture."""
    from capabilities import browser_pw
    assert "browser_capture" in browser_pw._HANDLERS
    assert "browser_capture" not in registry.names(registry.TOOL_BROWSER)


# ── API surface extraction (pure) ─────────────────────────────────────────────

def test_api_surface_keeps_endpoints_and_drops_static_noise():
    har = {"log": {"entries": [
        _har_entry("GET", "https://x.dev/api/orders?page=2", 200),
        _har_entry("POST", "https://x.dev/graphql", 201),
        _har_entry("GET", "https://x.dev/assets/app.css", 200, "text/css",
                   "stylesheet"),
        _har_entry("GET", "https://x.dev/img/hero.jpg", 200, "image/jpeg",
                   "image"),
        _har_entry("GET", "https://x.dev/manifest.json", 200,
                   "application/json", "other"),
    ]}}
    spec = re_mod.extract_api_surface(har)
    paths = {(row["method"], row["path"]) for row in spec["endpoints"]}
    assert ("GET", "/api/orders") in paths
    assert ("POST", "/graphql") in paths
    assert ("GET", "/manifest.json") in paths
    assert all("app.css" not in path for _m, path in paths)
    assert all("hero.jpg" not in path for _m, path in paths)
    orders = next(row for row in spec["endpoints"]
                  if row["path"] == "/api/orders")
    assert orders["query_keys"] == ["page"]
    assert orders["statuses"] == [200]


def test_api_surface_survives_an_unreadable_har():
    assert re_mod.extract_api_surface({})["endpoint_count"] == 0
    assert re_mod.extract_api_surface(None)["endpoint_count"] == 0


# ── remake ────────────────────────────────────────────────────────────────────

def _fake_model(text: str):
    calls: list[str] = []

    def ask(prompt, system, config):
        calls.append(prompt)
        return text, "fake-provider"

    return ask, calls


def test_remake_writes_a_verified_project(tmp_path, monkeypatch):
    from evolution import forge
    capture = _write_capture(tmp_path / "capture",
                             [_har_entry("GET", "https://x.dev/api/items")])
    reply = (
        "=== FILE: index.html ===\n<html><body><h1>Clean remake</h1></body></html>\n"
        "=== FILE: styles.css ===\nbody { margin: 0; }\n"
        "=== FILE: app.js ===\nconsole.log('clean');\n"
        "=== FILE: server.py ===\nimport json\n\n\ndef serve():\n    return json.dumps([])\n"
        "=== FILE: README.md ===\nA clean-room remake.\n"
    )
    ask, calls = _fake_model(reply)
    monkeypatch.setattr(forge, "_ask_model", ask)
    out = tmp_path / "out"
    result = re_mod.run("remake", {"path": str(capture),
                                   "destination": str(out)})
    assert result["ok"] is True, result
    assert result["checks"]["index_html"] is True
    assert result["checks"]["python_ok"] is True
    for name in ("index.html", "styles.css", "app.js", "server.py", "README.md"):
        assert (out / name).is_file(), name
    manifest = json.loads((out / "remake_manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "remake"
    assert manifest["provider"] == "fake-provider"
    assert calls and "https://x.dev/api/items" in calls[0]


def test_remake_rejects_unsafe_file_names_and_keeps_the_rest(tmp_path, monkeypatch):
    from evolution import forge
    capture = _write_capture(tmp_path / "capture")
    reply = (
        "=== FILE: ../../evil.sh ===\necho pwned\n"
        "=== FILE: payload.exe ===\nMZ\n"
        "=== FILE: index.html ===\n<html><body>ok</body></html>\n"
        "=== FILE: ../up/one.html ===\n<div>second page</div>\n"
        "=== FILE: safe.js ===\nconsole.log('ok');\n"
    )
    ask, _calls = _fake_model(reply)
    monkeypatch.setattr(forge, "_ask_model", ask)
    out = tmp_path / "out"
    result = re_mod.run("remake", {"path": str(capture),
                                   "destination": str(out)})
    assert result["ok"] is True
    assert (out / "one.html").is_file()          # basename kept, directory gone
    assert (out / "safe.js").is_file()
    assert not (out / "evil.sh").exists()
    assert not (out / "payload.exe").exists()
    assert set(result["files"]) == {"index.html", "one.html", "safe.js"}


def test_remake_without_synthesis_says_so_and_writes_nothing(tmp_path, monkeypatch):
    from evolution import forge
    capture = _write_capture(tmp_path / "capture")
    monkeypatch.setattr(
        forge, "_ask_model",
        lambda prompt, system, config: (_ for _ in ()).throw(
            RuntimeError("No AI provider key is available for skill synthesis.")))
    out = tmp_path / "out"
    result = re_mod.run("remake", {"path": str(capture),
                                   "destination": str(out)})
    assert result["ok"] is False
    assert result["reason"] == "unavailable"
    assert "No AI provider key" in result["detail"]
    assert not (out / "index.html").exists()


def test_remake_reports_an_unverified_project_instead_of_claiming_success(
        tmp_path, monkeypatch):
    from evolution import forge
    capture = _write_capture(tmp_path / "capture")
    # No index.html and a broken Python file: the checks must fail honestly.
    reply = ("=== FILE: page.html ===\n<div>hi</div>\n"
             "=== FILE: server.py ===\ndef broken(:\n")
    ask, _calls = _fake_model(reply)
    monkeypatch.setattr(forge, "_ask_model", ask)
    out = tmp_path / "out"
    result = re_mod.run("remake", {"path": str(capture),
                                   "destination": str(out)})
    assert result["ok"] is False
    assert result["reason"] == "unverified"
    assert result["checks"]["index_html"] is False
    assert result["checks"]["python_ok"] is False
    assert "did NOT fully pass verification" in result["detail"]


def test_remake_needs_a_url_or_a_capture_folder():
    result = re_mod.run("remake", {})
    assert result["ok"] is False
    assert "needs a 'url'" in result["detail"]


def test_parse_file_blocks_handles_fences_and_the_json_fallback():
    fenced = ("=== FILE: index.html ===\n```html\n<html>hi</html>\n```\n"
              "=== FILE: app.js ===\n```js\nconsole.log(1)\n```\n")
    files = re_mod.parse_file_blocks(fenced)
    assert files["index.html"] == "<html>hi</html>"
    assert files["app.js"] == "console.log(1)"
    fallback = re_mod.parse_file_blocks(
        '{"files": {"index.html": "<html>ok</html>"}}')
    assert fallback == {"index.html": "<html>ok</html>"}


@pytest.mark.parametrize("given,expected", [
    ("index.html", "index.html"),
    ("a/b/index.html", "index.html"),
    ("..\\..\\evil.sh", ""),
    ("payload.exe", ""),
    (".hidden.js", ""),
    ("weird name!.py", "weird_name_.py"),
])
def test_safe_file_name(given, expected):
    assert re_mod._safe_file_name(given) == expected


# ── reconstruct ───────────────────────────────────────────────────────────────

def test_reconstruct_copies_sources_and_skips_node_modules(workspace, tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(re_mod, "_rea_recovery",
                        lambda src, out: {"attempted": False, "ok": False,
                                          "note": "REA is not connected."})
    src = tmp_path / "my_app"
    (src / "src").mkdir(parents=True)
    (src / "package.json").write_text("{}", encoding="utf-8")
    (src / "src" / "main.js").write_text("console.log(1)", encoding="utf-8")
    (src / "README.md").write_text("hi", encoding="utf-8")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "big.js").write_text("x" * 10, encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG")
    result = re_mod.run("reconstruct", {"path": str(src), "authorized": True})
    assert result["ok"] is True, result
    dest = Path(result["path"])
    assert (dest / "package.json").is_file()
    assert (dest / "src" / "main.js").is_file()
    assert not (dest / "node_modules").exists()
    assert not (dest / "logo.png").exists()      # binary assets are not sources
    assert "REA is not connected" in result["detail"]


def test_reconstruct_refuses_a_single_file(tmp_path):
    single = tmp_path / "bundle.js"
    single.write_text("(()=>{})()", encoding="utf-8")
    result = re_mod.run("reconstruct", {"path": str(single), "authorized": True})
    assert result["ok"] is False
    assert "extracted app folder" in result["detail"]


# ── REA recovery integration (stubbed at the manager seam) ───────────────────

class FakeToolCall:
    def __init__(self, ok, text="", error=""):
        self.ok = ok
        self.text = text
        self.error = error


class FakeManager:
    def __init__(self, tools, result=None):
        self._tools = tools
        self._result = result
        self.calls: list[tuple] = []

    def tools(self):
        return self._tools

    def call_tool(self, server, tool, arguments, timeout=None):
        self.calls.append((server, tool, arguments))
        return self._result


def _rea_tool_entry(schema: dict) -> dict:
    return {"server": "rea", "tool": "recover_javascript_sources",
            "schema": schema}


def test_rea_recovery_saves_the_output_when_the_schema_matches(tmp_path,
                                                               monkeypatch):
    fake = FakeManager([_rea_tool_entry(
        {"properties": {"path": {"type": "STRING"}}, "required": ["path"]})],
        result=FakeToolCall(True, "# recovered sources"))
    monkeypatch.setattr("mcp_host.manager.get_manager", lambda: fake)
    out = tmp_path / "reconstruct"
    out.mkdir()
    note = re_mod._rea_recovery(tmp_path / "app", out)
    assert note["attempted"] is True and note["ok"] is True
    assert (out / "rea_recovery.md").read_text(encoding="utf-8") == \
        "# recovered sources"
    assert fake.calls and fake.calls[0][1] == "recover_javascript_sources"


def test_rea_recovery_reports_when_rea_is_not_connected(tmp_path, monkeypatch):
    fake = FakeManager([])
    monkeypatch.setattr("mcp_host.manager.get_manager", lambda: fake)
    note = re_mod._rea_recovery(tmp_path / "app", tmp_path)
    assert note["attempted"] is False
    assert "not connected" in note["note"]


def test_rea_recovery_never_guesses_a_parameter_name(tmp_path, monkeypatch):
    # The tool exists but its schema exposes no path-like input: the attempt
    # must not run rather than call it with a guessed name.
    fake = FakeManager([_rea_tool_entry(
        {"properties": {"detail": {"type": "STRING"}}, "required": ["path"]})],
        result=FakeToolCall(True, "should never be asked for"))
    monkeypatch.setattr("mcp_host.manager.get_manager", lambda: fake)
    note = re_mod._rea_recovery(tmp_path / "app", tmp_path)
    assert note["attempted"] is False
    assert "usable path argument" in note["note"]
    assert fake.calls == []


def test_rea_arguments_uses_the_discovered_schema_only():
    assert re_mod._rea_arguments({"properties": {"path": {}}}, Path("x")) \
        == {"path": str(Path("x"))}
    assert re_mod._rea_arguments({"properties": {"input": {}}}, Path("x")) \
        == {"input": str(Path("x"))}
    # A required property that cannot be filled means "do not call".
    assert re_mod._rea_arguments(
        {"properties": {"path": {}}, "required": ["options"]}, Path("x")) is None
    assert re_mod._rea_arguments({"properties": {}}, Path("x")) is None


# ── registry / router / permissions wiring ───────────────────────────────────

def test_the_module_implements_exactly_the_declared_actions():
    assert set(registry.names(registry.TOOL_REVERSE)) == set(re_mod._HANDLERS)


def test_the_tool_is_declared_locked_labelled_and_dispatched():
    tool = registry.TOOL_REVERSE
    assert tool in registry.TOOLS
    assert tool in actions._RUNNERS
    assert tool in router.ENGINE_OF
    assert tool in router.LOCK_OF
    assert tool in router.LABEL_OF
    assert tool in router.LOCAL_LOCK_WAIT
    assert registry.TOOL_REVERSE in FEATURE_DEFAULTS
    label = label_for("reverse_engineer")
    assert label and label != "reverse_engineer" and "_" not in label


def test_parameters_expose_authorization_and_feature_focus():
    props = registry.parameters(registry.TOOL_REVERSE)["properties"]
    assert props["authorized"]["type"] == "BOOLEAN"
    assert props["feature"]["type"] == "STRING"
    assert props["action"]["enum"] == ["capture", "api_surface", "remake",
                                       "reconstruct"]
    assert registry.get(registry.TOOL_REVERSE, "capture").required == \
        ("url", "authorized")


def test_a_switched_off_permission_refuses_before_the_runner(monkeypatch):
    def fail(*_a, **_k):
        pytest.fail("the runner must not be reached")

    monkeypatch.setattr(re_mod, "_capture_via_browser", fail)
    out = actions._handle("reverse_engineer",
                          {"action": "capture", "url": "https://example.com",
                           "authorized": True},
                          enabled=lambda name: False)
    assert "status=NOT_DONE" in out
    assert "switched off in Settings" in out


def test_the_full_path_refuses_without_authorization(monkeypatch):
    monkeypatch.setattr(re_mod, "_capture_via_browser",
                        lambda *a, **k: pytest.fail("no browser"))
    out = actions._handle("reverse_engineer",
                          {"action": "capture", "url": "https://example.com",
                           "authorized": False},
                          enabled=lambda name: True)
    assert "status=NOT_DONE" in out
    assert "explicit confirmation" in out


def test_validation_catches_a_capture_without_its_target():
    out = actions._handle("reverse_engineer", {"action": "capture"},
                          enabled=lambda name: True)
    assert "needs" in out and "url" in out


def test_run_refuses_an_unknown_action():
    result = re_mod.run("explode", {})
    assert result["ok"] is False and result["reason"] == "unknown_action"
    assert "capture" in result["detail"]


def test_progress_events_report_real_phases(workspace, monkeypatch):
    phases: list[str] = []

    def fake_browser(url, capture_dir):
        _write_capture(Path(capture_dir), [])
        return {"ok": True, "controller": "playwright", "detail": "ok",
                "evidence": {"requests": 0}}

    monkeypatch.setattr(re_mod, "_capture_via_browser", fake_browser)
    result = re_mod.run("capture", {"url": "https://example.com",
                                    "authorized": True},
                        on_event=phases.append)
    assert result["ok"] is True
    assert any("capturing the page" in phase.lower() for phase in phases)
    assert any("api surface" in phase.lower() for phase in phases)


def test_the_runner_forwards_progress_and_the_handle_path(workspace, monkeypatch):
    seen: list[str] = []

    def fake_browser(url, capture_dir):
        _write_capture(Path(capture_dir), [])
        return {"ok": True, "controller": "playwright", "detail": "ok",
                "evidence": {"requests": 0}}

    monkeypatch.setattr(re_mod, "_capture_via_browser", fake_browser)
    out = actions._handle("reverse_engineer",
                          {"action": "capture", "url": "https://example.com",
                           "authorized": True},
                          enabled=lambda name: True, on_event=seen.append)
    assert "status=DONE" in out
    assert any("capturing the page" in item.lower() for item in seen)


def test_call_module_only_passes_on_event_to_modules_that_declare_it():
    class Plain:
        def run(self, action, args):
            return {"ok": True, "plain": action, "args": args}

    class WithEvent:
        def run(self, action, args, on_event=None):
            if on_event:
                on_event("phase")
            return {"ok": True, "event": True}

    seen: list[str] = []
    assert actions._call_module(Plain(), "a", {"x": 1}, seen.append)["plain"] == "a"
    assert actions._call_module(WithEvent(), "a", {}, seen.append)["event"] is True
    assert seen == ["phase"]


def test_the_brain_routes_reverse_engineering_requests_to_the_new_tool():
    from brain import skills
    selected = skills.route("reverse engineer this website and remake it")
    keys = [skill.key for skill in selected]
    assert "reverse_engineering" in keys
    skill = skills.get_skill("reverse_engineering")
    assert "reverse_engineer" in skill.tools
    # The new switch gating the tool gates the skill the same way.
    off = skills.route("reverse engineer this website",
                       features={"reverse_engineer": False})
    assert "reverse_engineering" not in [s.key for s in off]


# ── main.py surfaces (AST only; the app itself is never imported) ────────────

def _main_source() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


def test_main_declares_dispatches_and_guards_the_tool():
    source = _main_source()
    tree = ast.parse(source)
    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "name"
                        and isinstance(value, ast.Constant)):
                    declared.add(str(value.value))
    assert "reverse_engineer" in declared
    assert '"reverse_engineer": _local_actions.handle_reverse_engineer' in source
    assert "if tool_name == 'reverse_engineer':" in source

    guarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_GREETING_GUARDED_TOOLS"
                for t in node.targets):
            for item in ast.walk(node.value):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    guarded.add(item.value)
    assert "reverse_engineer" in guarded
