"""The setup wizard must run on a bare interpreter and configure keys safely.

Two guarantees:

* Structural — ``setup_jarvis.py`` imports only the standard library plus the
  project's own stdlib-only packages (``core``, ``app``). It has to run *before*
  pip, so a third-party import at any level would defeat the point.
* Behaviour — non-interactive key setup seeds from the environment and writes
  ``config/api_keys.json`` without disturbing anything else.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_jarvis  # noqa: E402

WIZARD_SOURCE = ROOT / "setup_jarvis.py"

# Project packages the wizard may import: both are themselves stdlib-only, so a
# bare interpreter can import them before any pip install has happened.
PROJECT_PACKAGES = {"core", "app"}


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_setup_wizard_imports_only_stdlib_and_project_packages():
    allowed = set(sys.stdlib_module_names) | {"__future__"} | PROJECT_PACKAGES
    third_party = sorted(_imported_top_level_modules(WIZARD_SOURCE) - allowed)
    assert third_party == [], (
        "setup_jarvis.py must stay runnable before pip; found third-party "
        f"imports: {third_party}"
    )


def test_mask_hides_secrets():
    assert "secret" not in setup_jarvis._mask("supersecretvalue")
    assert setup_jarvis._mask("") == setup_jarvis.dim("(not set)")


def test_write_and_read_config_round_trip(tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    monkeypatch.setattr(setup_jarvis, "CONFIG_FILE", cfg)
    assert setup_jarvis._write_config({"assistant_name": "JARVIS", "features": {}})
    assert setup_jarvis._read_config()["assistant_name"] == "JARVIS"


def test_non_interactive_keys_seed_from_environment(tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    env = tmp_path / ".env"
    monkeypatch.setattr(setup_jarvis, "CONFIG_FILE", cfg)
    monkeypatch.setattr(setup_jarvis, "ENV_FILE", env)
    monkeypatch.setattr(setup_jarvis, "ENV_EXAMPLE", tmp_path / ".env.example")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-1234567890")

    setup_jarvis.configure_keys("core", non_interactive=True)

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["gemini_api_key"] == "test-gemini-key-1234567890"
    # The key is mirrored into .env so shell launches pick it up too.
    assert "GEMINI_API_KEY=test-gemini-key-1234567890" in env.read_text(encoding="utf-8")


def test_check_flag_exits_zero(capsys):
    assert setup_jarvis.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "ENVIRONMENT" in out


# ── live connection testing (no network in the suite) ────────────────────────

def test_probe_key_reports_optional_and_skipped():
    assert setup_jarvis.probe_key("gemini_api_key", "") == ("optional", "no value stored")
    # A stored credential with no cheap read-only endpoint is reported honestly
    # as "skipped" rather than guessed at.
    state, _detail = setup_jarvis.probe_key("instagram_password", "abc")
    assert state == "skipped"


def test_probe_key_uses_a_real_request_and_reads_the_status(monkeypatch):
    calls: dict = {}

    def fake(url, headers=None, timeout=12.0):
        calls["url"] = url
        calls["headers"] = headers or {}
        return 200, b'{"data": [1, 2, 3]}', ""

    monkeypatch.setattr(setup_jarvis, "_http_status", fake)
    state, detail = setup_jarvis.probe_key("openai_api_key", "sk-test")
    assert state == "ready" and "3 model(s)" in detail
    assert calls["headers"]["Authorization"] == "Bearer sk-test"


def test_probe_key_rejects_a_bad_status(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_http_status", lambda *a, **k: (401, b"", ""))
    state, detail = setup_jarvis.probe_key("groq_api_key", "gsk-bad")
    assert state == "failed" and "401" in detail


def test_probe_key_reports_an_unreachable_provider(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_http_status", lambda *a, **k: (0, b"", "URLError"))
    state, detail = setup_jarvis.probe_key("anthropic_api_key", "sk-ant")
    assert state == "failed" and "could not be reached" in detail


def test_gemini_probe_sends_the_key_as_a_query_parameter(monkeypatch):
    seen: dict = {}

    def fake(url, headers=None, timeout=12.0):
        seen["url"] = url
        return 200, b"", ""

    monkeypatch.setattr(setup_jarvis, "_http_status", fake)
    state, _detail = setup_jarvis.probe_key("gemini_api_key", "abc123")
    assert state == "ready"
    assert "key=abc123" in seen["url"]


def test_group_spec_covers_exactly_the_known_fields():
    all_fields = {k for k, _l, _s, _u in setup_jarvis._group_spec("all")}
    expected = ({k for k, *_ in setup_jarvis.CORE_KEYS}
                | {k for k, *_ in setup_jarvis.VOICE_KEYS}
                | {k for k, *_ in setup_jarvis.EXTRA_KEYS})
    assert all_fields == expected
    assert {k for k, *_ in setup_jarvis._group_spec("core")} == \
        {k for k, *_ in setup_jarvis.CORE_KEYS}


def test_vlen_ignores_ansi_colour():
    assert setup_jarvis._vlen("\x1b[36mred\x1b[0m") == 3
    assert setup_jarvis._vlen("plain") == 5


def test_clear_screen_is_a_noop_when_not_a_tty(capsys):
    setup_jarvis.clear_screen()
    assert capsys.readouterr().out == ""


def test_confirm_takes_the_default_when_not_interactive(monkeypatch):
    monkeypatch.setattr(setup_jarvis.sys, "stdin", None)
    assert setup_jarvis._confirm("install it?", default=True) is True
    assert setup_jarvis._confirm("install it?", default=False) is False


def test_the_parser_accepts_the_new_flags():
    args = setup_jarvis.build_parser().parse_args(["--test", "--install-missing"])
    assert args.test is True
    assert args.install_missing is True


def test_diagnose_offers_to_install_when_something_is_missing(monkeypatch):
    called: list = []
    monkeypatch.setattr(setup_jarvis, "check_python", lambda: True)
    monkeypatch.setattr(setup_jarvis, "check_pip", lambda: True)
    monkeypatch.setattr(setup_jarvis, "check_node", lambda: (True, "node v20.11.0"))
    monkeypatch.setattr(setup_jarvis, "_count_configured_keys", lambda: 2)
    monkeypatch.setattr(setup_jarvis, "_feature_rows", lambda: [
        {"feature": "documents", "module": "docx", "package": "python-docx",
         "purpose": "Word", "ready": False, "detail": "missing python-docx",
         "fix": "pip install python-docx"}])
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(setup_jarvis, "install_all_missing", lambda: called.append(True))

    summary = setup_jarvis.diagnose()

    assert summary == {"ready": 0, "missing": 1}
    assert called == [True]


def test_diagnose_does_not_install_when_everything_is_ready(monkeypatch):
    called: list = []
    monkeypatch.setattr(setup_jarvis, "check_python", lambda: True)
    monkeypatch.setattr(setup_jarvis, "check_pip", lambda: True)
    monkeypatch.setattr(setup_jarvis, "check_node", lambda: (True, "node v20.11.0"))
    monkeypatch.setattr(setup_jarvis, "_count_configured_keys", lambda: 3)
    monkeypatch.setattr(setup_jarvis, "_feature_rows", lambda: [
        {"feature": "documents", "module": "docx", "package": "python-docx",
         "purpose": "Word", "ready": True, "detail": "", "fix": ""}])
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(setup_jarvis, "install_all_missing", lambda: called.append(True))

    summary = setup_jarvis.diagnose()

    assert summary == {"ready": 1, "missing": 0}
    assert called == []
# ── HUD styling (the wizard speaks the app's ui_theme "hud" language) ────────

def test_hud_palette_matches_the_app_theme():
    assert setup_jarvis.HUD_ACCENT == "#00d4ff"   # arc-reactor cyan
    assert setup_jarvis.HUD_GOLD == "#f4b24a"     # repulsor-gold
    assert setup_jarvis.HUD_GREEN == "#5df0b6"
    assert setup_jarvis.HUD_RED == "#ff6f8b"


def test_paint_is_plain_when_colour_is_off(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_COLOR", False)
    assert setup_jarvis._paint("#00d4ff", "hi") == "hi"


def test_paint_emits_truecolour_when_enabled(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_COLOR", True)
    out = setup_jarvis._paint("#00d4ff", "hi")
    assert "38;2;0;212;255" in out and out.endswith("\033[0m")


def test_hex_rgb_parses_short_long_and_garbage():
    assert setup_jarvis._hex_rgb("#0df") == (0, 221, 255)
    assert setup_jarvis._hex_rgb("#00d4ff") == (0, 212, 255)
    assert setup_jarvis._hex_rgb("nonsense") == (0, 0, 0)


def test_vlen_ignores_truecolour_codes(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_COLOR", True)
    painted = setup_jarvis._paint("#00d4ff", "abc")
    assert setup_jarvis._vlen(painted) == 3


def test_the_main_menu_shows_every_main_row():
    text = setup_jarvis.render_menu()
    for key, _title, _desc, _target in setup_jarvis.MAIN_MENU:
        assert f"[{key}]" in text, key


def test_every_submenu_renders_its_own_rows():
    for menu_id, rows in setup_jarvis.SUBMENUS.items():
        title = setup_jarvis.SUBMENU_TITLES[menu_id]
        text = setup_jarvis.render_menu(rows, 0, title)
        for key, _title, _desc, _action in rows:
            assert f"[{key}]" in text, (menu_id, key)


def test_authorize_is_reachable_from_the_keys_submenu():
    rows = setup_jarvis.SUBMENUS["keys-menu"]
    text = setup_jarvis.render_menu(rows, 0, setup_jarvis.SUBMENU_TITLES["keys-menu"])
    assert "[o]" in text and "Authorize" in text
    assert setup_jarvis._lookup_choice("o", "keys-menu")[0] == "action"


def test_menu_lookup_resolves_every_key_at_its_own_level():
    # Main-menu keys resolve from the main view.
    for key, _t, _d, target in setup_jarvis.MAIN_MENU:
        found = setup_jarvis._lookup_choice(key, "")
        assert found is not None, key
        if isinstance(target, str):
            assert found == ("submenu", target)
        elif target is None:
            assert found == ("exit", None)
        else:
            assert found[0] == "action" and found[1] is target
    # Submenu keys resolve inside their submenu.
    for menu_id, rows in setup_jarvis.SUBMENUS.items():
        for key, _t, _d, action in rows:
            found = setup_jarvis._lookup_choice(key, menu_id)
            assert found == ("action", action), (menu_id, key)


def test_a_repeated_key_follows_the_open_view():
    # [5] is Speed doctor on the main menu and API keys inside Keys & providers.
    assert setup_jarvis._lookup_choice("5", "")[1] is setup_jarvis.speed_doctor
    assert setup_jarvis._lookup_choice("5", "keys-menu")[1] is \
        setup_jarvis._configure_keys_entry
    # Unknown keys resolve to nothing (the menu warns instead of crashing).
    assert setup_jarvis._lookup_choice("\x07", "") is None


def test_menu_line_keeps_the_description_last():
    line = setup_jarvis._menu_line("1", "Title", "the help text")
    assert line.index("the help text") > line.index("[1]")
# ── Node runtime repair ladder ───────────────────────────────────────────────

def test_node_modules_ready_requires_a_nonempty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    assert setup_jarvis._node_modules_ready() is False
    (tmp_path / "node_modules").mkdir()
    assert setup_jarvis._node_modules_ready() is False
    (tmp_path / "node_modules" / "agent-tars").write_text("x")
    assert setup_jarvis._node_modules_ready() is True


def _fake_node_repo(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    monkeypatch.setattr(setup_jarvis, "PACKAGE_JSON", tmp_path / "package.json")
    monkeypatch.setattr(setup_jarvis, "check_node", lambda: (True, "v20.11.0"))
    monkeypatch.setattr(setup_jarvis.shutil, "which",
                        lambda name: "C:/npm.cmd" if name == "npm" else None)


class _NodeResult:
    def __init__(self, code: int, text: str = ""):
        self.returncode = code
        self.stdout = text
        self.stderr = ""


def test_node_ladder_takes_the_next_rung_after_a_failure(tmp_path, monkeypatch):
    _fake_node_repo(tmp_path, monkeypatch)
    seen: list[str] = []
    state = {"rung": 0, "repaired": False}

    def fake_npm(args, timeout=None):
        state["rung"] += 1
        seen.append(" ".join(args))
        return _NodeResult(1 if state["rung"] == 1 else 0)

    monkeypatch.setattr(setup_jarvis, "_npm_run", fake_npm)
    monkeypatch.setattr(setup_jarvis, "_repair_npm",
                        lambda has_lock: state.__setitem__("repaired", True))
    reads = {"n": 0}

    def fake_ready():
        reads["n"] += 1
        return True

    monkeypatch.setattr(setup_jarvis, "_node_modules_ready", fake_ready)

    assert setup_jarvis.install_node_deps() is True
    assert state["rung"] == 2          # first rung failed, second succeeded
    assert state["repaired"] is True   # the failure triggered a repair
    assert reads["n"] == 1             # readiness is read on the rung that worked
    assert seen[0].startswith("ci")    # pinned `npm ci` is tried first


def test_node_ladder_reports_a_real_failure(tmp_path, monkeypatch):
    _fake_node_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_jarvis, "_npm_run", lambda args, timeout=None: _NodeResult(1))
    monkeypatch.setattr(setup_jarvis, "_repair_npm", lambda has_lock: None)
    monkeypatch.setattr(setup_jarvis, "_node_modules_ready", lambda: False)

    assert setup_jarvis.install_node_deps() is False


def test_node_install_refuses_without_package_json(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    monkeypatch.setattr(setup_jarvis, "PACKAGE_JSON", tmp_path / "package.json")
    monkeypatch.setattr(setup_jarvis, "check_node", lambda: (True, "v20.11.0"))
    monkeypatch.setattr(setup_jarvis.shutil, "which",
                        lambda name: "C:/npm.cmd" if name == "npm" else None)
    assert setup_jarvis.install_node_deps() is False
# ── authorize / key-prompt commands ──────────────────────────────────────────

def test_key_commands_map_words_to_actions():
    assert setup_jarvis.KEY_COMMANDS["auth"] == "open"
    assert setup_jarvis.KEY_COMMANDS["authorize"] == "open"
    assert setup_jarvis.KEY_COMMANDS["clear"] == "-"
    assert setup_jarvis.KEY_COMMANDS["verify"] == "test"
    assert setup_jarvis.KEY_COMMANDS["import"] == "env"
    assert setup_jarvis.KEY_COMMANDS["skip"] == "keep"


def test_authorize_opens_only_the_missing_providers(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_read_config",
                        lambda: {"gemini_api_key": "already-set"})
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: True)
    opened: list[str] = []
    monkeypatch.setattr(setup_jarvis, "_open_url",
                        lambda url: opened.append(url) or True)

    assert setup_jarvis.authorize_keys() is True
    assert opened, "a missing provider's page should have been opened"
    # Gemini is already set, so its page is never opened.
    assert "https://aistudio.google.com/app/apikey" not in opened


def test_authorize_skips_opening_when_declined(monkeypatch):
    monkeypatch.setattr(setup_jarvis, "_read_config", lambda: {})
    monkeypatch.setattr(setup_jarvis, "_confirm", lambda *a, **k: False)
    opened: list[str] = []
    monkeypatch.setattr(setup_jarvis, "_open_url",
                        lambda url: opened.append(url) or True)
    assert setup_jarvis.authorize_keys() is True
    assert opened == []


def test_help_for_names_the_field_and_its_link(capsys):
    setup_jarvis._help_for("gemini_api_key")
    out = capsys.readouterr().out
    assert "gemini_api_key" in out and "aistudio.google.com" in out


def test_prompt_key_asks_for_the_new_api_key(monkeypatch):
    seen = {}

    def fake_input(prompt=""):
        seen["prompt"] = prompt
        return "AQ.Ab8_test_value"

    monkeypatch.setattr("builtins.input", fake_input)
    assert setup_jarvis._prompt_key("New API KEY") == "AQ.Ab8_test_value"
    assert "New API KEY" in seen["prompt"]
    assert seen["prompt"].rstrip().endswith(":")

# ── packaging: Convert to .exe ───────────────────────────────────────────────

def test_build_and_reset_are_still_reachable():
    main_keys = [key for key, _t, _d, _x in setup_jarvis.MAIN_MENU]
    assert "9" in main_keys, "the main menu has no Build .exe row"
    maintain_keys = [key for key, _t, _d, _x in setup_jarvis.SUBMENUS["maintain-menu"]]
    assert "r" in maintain_keys, "Clean & maintain has no Reset data row"


def test_build_args_name_the_app_and_bundle_the_saved_data(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    for folder in ("config", "data", "memory", "assets"):
        (tmp_path / folder).mkdir()

    args = setup_jarvis.build_pyinstaller_args("Jarvis OS")

    assert args[args.index("--name") + 1] == "Jarvis OS"
    assert "--onedir" in args and "--windowed" in args
    bundled = [args[i + 1] for i, item in enumerate(args) if item == "--add-data"]
    for folder in ("config", "data", "memory", "assets"):
        assert any(entry.endswith(f"{os.pathsep}{folder}") for entry in bundled)
    assert args[-1] == str(tmp_path / "main.py")


def test_onefile_and_console_switch_the_build_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    args = setup_jarvis.build_pyinstaller_args("Jarvis OS", mode="onefile",
                                               windowed=False)
    assert "--onefile" in args and "--onedir" not in args
    assert "--windowed" not in args


def test_the_build_only_bundles_folders_that_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    args = setup_jarvis.build_pyinstaller_args("Jarvis OS")
    bundled = [args[i + 1] for i, item in enumerate(args) if item == "--add-data"]
    assert len(bundled) == 1 and bundled[0].endswith(f"{os.pathsep}config")


# ── Reset data ───────────────────────────────────────────────────────────────

def test_reset_plan_backs_up_the_stores_and_clears_only_caches(tmp_path):
    plan = setup_jarvis.reset_plan(tmp_path)
    names = [Path(path).name for path in plan["backup_files"]]
    assert "api_keys.json" in names
    assert "brain_memory.db" in names
    assert "conversations.db" in names
    cleared = " ".join(plan["clear_dirs"])
    assert "cache" in cleared and "logs" in cleared
    # The source tree is never part of a reset.
    assert not any("main.py" in path for path in plan["backup_files"])
    assert not any("setup_jarvis.py" in path for path in plan["clear_dirs"])


def test_the_cli_exposes_build_and_reset():
    args = setup_jarvis.build_parser().parse_args(
        ["--build-exe", "--name", "Jarvis OS"])
    assert args.build_exe is True and args.name == "Jarvis OS"
    assert setup_jarvis.build_parser().parse_args(["--reset"]).reset is True


def test_the_parser_accepts_the_operator_tool_flags():
    args = setup_jarvis.build_parser().parse_args(
        ["--speed-doctor", "--latency-report", "--settings"])
    assert args.speed_doctor is True
    assert args.latency_report is True
    assert args.settings is True


# ── Speed doctor / settings editor / latency report ─────────────────────────

def test_speed_doctor_flags_a_slow_voice_mode_and_applies_the_fast_profile(
        tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({
        "voice_output_mode": "gemini_piper",
        "features": {"voice_activity_detection": False},
        "gemini_api_key": "test-key",
    }), encoding="utf-8")
    monkeypatch.setattr(setup_jarvis, "CONFIG_FILE", cfg)
    monkeypatch.setattr(setup_jarvis, "SETTINGS_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(setup_jarvis, "probe_key",
                        lambda *a, **k: ("ready", "Google accepted the key"))

    summary = setup_jarvis.speed_doctor(apply=True)

    assert summary["applied"] is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["voice_output_mode"] == "gemini"
    assert data["features"]["voice_activity_detection"] is True
    backups = list((tmp_path / "backups").glob("speeddoctor_*.json"))
    assert backups, "the fast profile must save a backup first"
    saved = json.loads(backups[0].read_text(encoding="utf-8"))
    assert saved["voice_output_mode"] == "gemini_piper", \
        "the backup holds the OLD profile"


def test_speed_doctor_reports_a_fast_profile_as_clean(tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({
        "voice_output_mode": "gemini",
        "features": {"voice_activity_detection": True, "laya_ai": True},
        "gemini_api_key": "test-key",
    }), encoding="utf-8")
    monkeypatch.setattr(setup_jarvis, "CONFIG_FILE", cfg)
    monkeypatch.setattr(setup_jarvis, "SETTINGS_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(setup_jarvis, "probe_key",
                        lambda *a, **k: ("ready", "Google accepted the key"))

    summary = setup_jarvis.speed_doctor(apply=True)

    assert summary["applied"] is False
    assert summary["findings_fixed"] == []
    assert not list((tmp_path / "backups").glob("*")), \
        "a clean profile changes nothing, so nothing is backed up"


def test_settings_editor_writes_validated_values(tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({"features": {}}), encoding="utf-8")
    monkeypatch.setattr(setup_jarvis, "CONFIG_FILE", cfg)
    monkeypatch.setattr(setup_jarvis, "SETTINGS_BACKUP_DIR", tmp_path / "backups")

    # One answer per SETTINGS_FIELDS row, in order:
    # name · user · provider · model · voice · VAD · wake · sens · proactive ·
    # narrator · Laya
    replies = iter(["JARVIS", "", "openai", "", "gemini", "off",
                    "", "", "", "", ""])
    monkeypatch.setattr(setup_jarvis, "ask",
                        lambda prompt, default="": next(replies, ""))

    assert setup_jarvis.edit_settings() is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["assistant_name"] == "JARVIS"
    assert data["ai_provider"] == "openai"
    assert data["voice_output_mode"] == "gemini"
    assert data["features"]["voice_activity_detection"] is False
    assert "wake_word" not in data["features"], "Enter keeps the current value"
    assert list((tmp_path / "backups").glob("settings_*.json")), \
        "a write must save a backup first"


def test_settings_editor_rejects_a_value_outside_the_allowed_set(
        tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({"ai_provider": "gemini"}), encoding="utf-8")
    monkeypatch.setattr(setup_jarvis, "CONFIG_FILE", cfg)
    monkeypatch.setattr(setup_jarvis, "SETTINGS_BACKUP_DIR", tmp_path / "backups")

    replies = iter(["", "", "microsoft"])
    monkeypatch.setattr(setup_jarvis, "ask",
                        lambda prompt, default="": next(replies, ""))

    assert setup_jarvis.edit_settings() is True

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["ai_provider"] == "gemini", "an invalid choice is never written"
    assert "is not allowed here" in capsys.readouterr().out


def test_latency_report_parses_the_last_session(tmp_path, monkeypatch, capsys):
    log = tmp_path / "data" / "logs" / "jarvis.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "[STARTUP] UI_CREATED  elapsed=1.900s\n"
        "[STARTUP] GEMINI_LIVE_CONNECTED (gemini-3.8-live)  elapsed=9.500s\n"
        "[JARVIS] ⏱ Reply latency: 812 ms (end of speech → first audio)\n"
        "[JARVIS] ⏱ Reply latency: 1200 ms (end of speech → first audio)\n"
        "[JARVIS] Laya: decision model ready on CPU (models/laya, 95.0s load, "
        "torch threads=2)\n"
        "[JARVIS] ⚠ Voice tuning rejected — server-side VAD active: each voice "
        "reply gains ~800 ms of silence waiting.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)

    report = setup_jarvis.latency_report()

    out = capsys.readouterr().out
    assert report["turns"] == 2
    assert report["vad_downgraded"] is True
    # avg of 812 + 1200 = 1006; last = 1200; Laya load = 95 s
    assert "1006" in out and "1200" in out and "95" in out
    assert "LATENCY" in out


def test_latency_report_says_so_when_the_log_is_missing(tmp_path, monkeypatch,
                                                        capsys):
    monkeypatch.setattr(setup_jarvis, "ROOT", tmp_path)
    assert setup_jarvis.latency_report() == {}
    assert "No log yet" in capsys.readouterr().out
