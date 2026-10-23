"""dev_agent truthfulness and dependency-safety regression tests.

Everything here is offline: the model is never called, nothing is pip-installed,
and no test touches the real `data/` or `Desktop/JarvisProjects`. The run
detection tests spawn real child processes, because the defect being pinned is
exactly about the exit status of a real process — a text-only fake would not
have caught it.

Pins (docs/REPAIR_REPORT.md style):
* a silent non-zero exit is a failure, not "the project is working";
* a timeout is reported honestly and never as "working";
* a run that never started is not verified;
* no Gemini key is refused before any model call or approval card;
* auto-install maps known import aliases, and refuses stdlib/project-internal
  names instead of downloading a wrong package;
* generated projects get their own venv, with a *reported* fallback;
* VS Code is only reported as opened when a real launcher exists.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import code_helper as ch  # noqa: E402
from actions import dev_agent as da    # noqa: E402


@pytest.fixture()
def folder():
    """A private project directory (tempfile: pytest's tmp base is not writable here)."""
    with tempfile.TemporaryDirectory(prefix="jarvis-devagent-") as f:
        yield Path(f)


def _write(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def _plan(files=None, run_command="python main.py"):
    return {
        "project_name": "proj",
        "entry_point": "main.py",
        "files": files or [{"path": "main.py", "description": "entry", "imports": []}],
        "run_command": run_command,
        "dependencies": [],
    }


def _drive_build(monkeypatch, folder, outcome, attempts=2):
    """Run _build_project with the model, files, editor and subprocess faked.

    Returns (message, fix_calls). The only real part left is the decision logic
    under test.
    """
    monkeypatch.setattr(da, "PROJECTS_DIR", folder)
    monkeypatch.setattr(da, "MAX_FIX_ATTEMPTS", attempts)
    monkeypatch.setattr(da, "_plan_project", lambda desc, lang: _plan())
    monkeypatch.setattr(da, "_write_file", lambda **kw: "print('hi')\n")
    monkeypatch.setattr(da, "_open_vscode", lambda d: True)
    monkeypatch.setattr(da, "_ensure_project_venv", lambda d: (sys.executable, ""))
    monkeypatch.setattr(da, "_run_project", lambda *a, **kw: outcome)
    monkeypatch.setattr(da.time, "sleep", lambda *_: None)

    fix_calls: list[dict] = []
    monkeypatch.setattr(da, "_fix_files", lambda **kw: fix_calls.append(kw) or {})
    msg = da._build_project("do a thing", "python", "proj", 5)
    return msg, fix_calls


# ── run truthfulness (real subprocesses) ──────────────────────────────────────

def test_silent_non_zero_exit_is_a_failure(folder):
    _write(folder, "silent.py", "raise SystemExit(3)\n")
    outcome = da._run_project("python silent.py", folder, 30)
    assert outcome.exit_code == 3
    assert outcome.timed_out is False
    assert da._has_error(outcome) is True


def test_clean_exit_is_not_a_failure(folder):
    _write(folder, "ok.py", "print('hello')\n")
    outcome = da._run_project("python ok.py", folder, 30)
    assert outcome.exit_code == 0
    assert da._has_error(outcome) is False
    assert "hello" in outcome.output


def test_traceback_is_parsed_to_the_right_file_and_line(folder):
    _write(folder, "boom.py", "def main():\n    raise ValueError('boom')\n\nmain()\n")
    outcome = da._run_project("python boom.py", folder, 30)
    assert da._has_error(outcome) is True
    assert da._classify_error(outcome.output) == "runtime_error"
    path, line = da._parse_traceback(outcome.output, ["boom.py"])
    assert path == "boom.py"
    assert line == 2


def test_timeout_is_not_a_failure_but_is_marked(folder):
    _write(folder, "slow.py", "import time\n\ntime.sleep(30)\n")
    outcome = da._run_project("python slow.py", folder, 1)
    assert outcome.timed_out is True
    assert da._has_error(outcome) is False


def test_a_timeout_that_printed_an_error_is_a_failure(folder):
    # Prints a real traceback, THEN hangs: the error is evidence, so the run
    # must not be reported as "no error seen".
    _write(folder, "hang.py",
           "import time, traceback\n\n"
           "try:\n    raise ValueError('boom')\n"
           "except ValueError:\n    traceback.print_exc()\n\n"
           "time.sleep(30)\n")
    outcome = da._run_project("python hang.py", folder, 1)
    assert outcome.timed_out is True
    assert da._has_error(outcome) is True


def test_a_command_that_never_started_is_not_verified(folder):
    outcome = da._run_project("definitely-not-a-real-binary-xyz", folder, 10)
    assert outcome.started is False
    assert da._has_error(outcome) is True


# ── the reports the user actually hears ───────────────────────────────────────

def test_build_reports_clean_exit_as_success(monkeypatch, folder):
    msg, fix_calls = _drive_build(
        monkeypatch, folder, da._RunOutcome("STDOUT:\nhello", exit_code=0))
    assert "runs and exits cleanly" in msg
    assert fix_calls == []


def test_build_never_calls_a_timeout_working(monkeypatch, folder):
    msg, fix_calls = _drive_build(
        monkeypatch, folder,
        da._RunOutcome("STDOUT:\nserver up", exit_code=-9, timed_out=True))
    assert "is working" not in msg
    assert "kept running" in msg
    assert fix_calls == []


def test_build_does_not_claim_no_error_seen_when_a_timeout_printed_one(monkeypatch, folder):
    msg, fix_calls = _drive_build(
        monkeypatch, folder,
        da._RunOutcome("STDERR:\nTraceback (most recent call last):\nValueError: boom",
                       exit_code=-9, timed_out=True))
    assert "no error seen" not in msg
    assert len(fix_calls) == 1
    assert "Traceback" in fix_calls[0]["error_output"]


def test_build_reports_a_silent_non_zero_exit_as_failure(monkeypatch, folder):
    msg, fix_calls = _drive_build(
        monkeypatch, folder, da._RunOutcome("Ran with no output.", exit_code=3))
    assert "is working" not in msg
    assert "couldn't fully fix" in msg
    assert "(exit 3)" in msg
    assert len(fix_calls) == 1
    # The fix prompt is told the exit status: it is the only evidence there was.
    assert "exited with status 3" in fix_calls[0]["error_output"]


# ── credential pre-flight ─────────────────────────────────────────────────────

def test_no_key_is_refused_before_any_model_call(monkeypatch):
    import config.settings as settings
    import security.coding_approval as approval

    monkeypatch.setattr(settings, "get_feature", lambda name, default=None: True)
    monkeypatch.setattr(da, "_get_api_key", lambda: "")

    called: list = []
    monkeypatch.setattr(da, "_build_project",
                        lambda **kw: called.append("build") or "built")
    monkeypatch.setattr(approval, "request_approval",
                        lambda *a, **k: called.append("approval") or True)

    msg = da.dev_agent({"description": "build a thing"})
    assert "No Gemini API key" in msg
    assert called == []


def test_timeout_value_is_coerced():
    assert da._coerce_timeout("thirty") == 30
    assert da._coerce_timeout(None) == 30
    assert da._coerce_timeout("45") == 45
    assert da._coerce_timeout(0) == 1
    assert da._coerce_timeout(-5) == 1
    assert da._coerce_timeout(10000) == 600


def test_model_failures_become_actionable_sentences():
    auth = da._describe_model_failure(
        RuntimeError("Gemini rejected the API key (401)"))
    assert "Settings" in auth
    rate = da._describe_model_failure(
        RuntimeError("All models for role 'reasoning' failed (rate_limit): 429"))
    assert "rate-limiting" in rate


# ── dependency safety ─────────────────────────────────────────────────────────

def test_import_aliases_and_refusals():
    assert da._pip_name_for("cv2") == ("opencv-python", "")
    assert da._pip_name_for("PIL") == ("pillow", "")
    assert da._pip_name_for("yaml") == ("pyyaml", "")
    assert da._pip_name_for("requests") == ("requests", "")

    pkg, why = da._pip_name_for("json")
    assert pkg is None and "standard library" in why

    pkg, why = da._pip_name_for("utils", frozenset({"utils"}))
    assert pkg is None and "project's own modules" in why

    pkg, why = da._pip_name_for("weird-name!")
    assert pkg is None and "will not guess" in why


def test_aliases_come_from_the_authoritative_table_not_a_local_copy():
    """One table: dev_agent must agree with core.dependency_ops (no local copy)."""
    from core.dependency_ops import IMPORT_TO_PIP

    assert not hasattr(da, "_IMPORT_TO_PIP"), "the duplicated table must be gone"
    assert da._pip_name_for("google") == ("google-genai", "")
    assert da._import_aliases()["google"] == IMPORT_TO_PIP["google"]
    assert da._pip_name_for("PIL") == (IMPORT_TO_PIP["PIL"], "")


def test_install_dependencies_refuses_pip_options(monkeypatch, folder):
    def _boom(*a, **k):
        raise AssertionError("pip must never run for refused entries")

    monkeypatch.setattr(da.subprocess, "run", _boom)
    msg = da._install_dependencies(
        ["--index-url=http://evil/", "-r requirements.txt"], folder, python_exe="PY")
    assert "Nothing installed" in msg


def test_install_dependencies_never_passes_options_to_pip(monkeypatch, folder):
    seen: list = []

    class _R:
        def __init__(self, code):
            self.returncode = code

    def _fake_run(cmd, **kw):
        seen.append(cmd)
        return _R(1 if "show" in cmd else 0)

    monkeypatch.setattr(da.subprocess, "run", _fake_run)
    msg = da._install_dependencies(
        ["requests", "--index-url=http://evil/"], folder, python_exe="PY")
    flat = [str(item) for cmd in seen for item in cmd]
    assert not any("index-url" in item for item in flat)
    assert seen[-1] == ["PY", "-m", "pip", "install", "requests"]
    assert "Refused" in msg


def test_auto_install_refuses_without_shelling_out(monkeypatch, folder):
    def _boom(*a, **k):
        raise AssertionError("pip must never run for a refusal")

    monkeypatch.setattr(da.subprocess, "run", _boom)
    assert da._try_auto_install("No module named 'json'", folder) is False
    assert da._try_auto_install("No module named 'utils'", folder,
                                project_modules=frozenset({"utils"})) is False


def test_auto_install_maps_alias_into_the_project_interpreter(monkeypatch, folder):
    seen: dict = {}

    class _R:
        returncode = 0

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(da.subprocess, "run", _fake_run)
    ok = da._try_auto_install("ModuleNotFoundError: No module named 'cv2'",
                              folder, python_exe="PY")
    assert ok is True
    assert seen["cmd"] == ["PY", "-m", "pip", "install", "opencv-python"]


def test_project_module_names_cover_files_and_packages():
    names = da._project_module_names([
        {"path": "main.py"}, {"path": "utils/helpers.py"},
    ])
    assert "main" in names
    assert "utils" in names
    assert "helpers" in names
    assert "__init__" not in names


# ── project venv isolation ────────────────────────────────────────────────────

def test_project_python_is_none_without_a_venv(folder):
    assert da._project_python(folder) is None


def test_ensure_project_venv_falls_back_and_says_so(monkeypatch, folder):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "venv is unavailable here"

    monkeypatch.setattr(da.subprocess, "run", lambda *a, **k: _R())
    exe, note = da._ensure_project_venv(folder)
    assert exe == da.sys.executable
    assert "Could not create a project virtual environment" in note


def test_ensure_project_venv_reuses_an_existing_venv(monkeypatch, folder):
    rel = (Path("Scripts") / "python.exe") if da.os.name == "nt" else (Path("bin") / "python")
    exe_path = folder / da._VENV_DIRNAME / rel
    exe_path.parent.mkdir(parents=True)
    exe_path.write_text("", encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("an existing venv must not be rebuilt")

    monkeypatch.setattr(da.subprocess, "run", _boom)
    exe, note = da._ensure_project_venv(folder)
    assert exe == str(exe_path)
    assert note == ""


def test_install_dependencies_targets_the_project_interpreter(monkeypatch, folder):
    seen: list = []

    class _R:
        def __init__(self, code):
            self.returncode = code

    def _fake_run(cmd, **kw):
        seen.append(cmd)
        # The probe must report the package as missing, so the install runs.
        return _R(1 if "show" in cmd else 0)

    monkeypatch.setattr(da.subprocess, "run", _fake_run)
    msg = da._install_dependencies(["requests"], folder, python_exe="PY")
    assert "Installed into the project environment" in msg
    assert seen[0] == ["PY", "-m", "pip", "show", "requests"]
    assert seen[-1] == ["PY", "-m", "pip", "install", "requests"]


def _drive_venv_choice(monkeypatch, folder, language, run_command="python main.py"):
    """Run a build and report whether a project venv was requested."""
    monkeypatch.setattr(da, "PROJECTS_DIR", folder)
    monkeypatch.setattr(da, "MAX_FIX_ATTEMPTS", 1)
    monkeypatch.setattr(da, "_plan_project",
                        lambda desc, lang: _plan(run_command=run_command))
    monkeypatch.setattr(da, "_write_file", lambda **kw: "print('hi')\n")
    monkeypatch.setattr(da, "_open_vscode", lambda d: True)
    monkeypatch.setattr(da.time, "sleep", lambda *_: None)
    monkeypatch.setattr(da, "_ensure_project_venv",
                        lambda d: venv_calls.append(d) or ("PROJECT_PY", ""))
    used: dict = {}

    def _fake_run(*a, **kw):
        used.update(kw)
        return da._RunOutcome("", exit_code=0)

    monkeypatch.setattr(da, "_run_project", _fake_run)
    venv_calls: list = []
    da._build_project("do a thing", language, "proj", 5)
    return venv_calls, used


def test_build_creates_a_project_venv_even_without_declared_dependencies(monkeypatch, folder):
    # No declared dependencies: auto-install must still have a project venv to
    # use, or it would fall back to JARVIS's own interpreter (the old leak).
    venv_calls, used = _drive_venv_choice(monkeypatch, folder, "python")
    assert venv_calls, "a Python project must get its own venv regardless of dependencies"
    assert used.get("python_exe") == "PROJECT_PY"


def test_build_treats_a_python_run_command_as_python(monkeypatch, folder):
    venv_calls, _ = _drive_venv_choice(monkeypatch, folder, "", run_command="python main.py")
    assert venv_calls, "a 'python …' run command is a Python project"


def test_build_skips_the_venv_for_a_non_python_project(monkeypatch, folder):
    venv_calls, _ = _drive_venv_choice(monkeypatch, folder, "javascript",
                                       run_command="npm start")
    assert venv_calls == []


# ── VS Code launch honesty ────────────────────────────────────────────────────

def test_vscode_missing_is_reported(monkeypatch, folder):
    monkeypatch.setattr(da, "_VSCODE_CANDIDATES", ("code",))
    monkeypatch.setattr(da.shutil, "which", lambda name: None)
    assert da._open_vscode(folder) is False


def test_vscode_found_is_launched(monkeypatch, folder):
    monkeypatch.setattr(da, "_VSCODE_CANDIDATES", ("code",))
    monkeypatch.setattr(da.shutil, "which", lambda name: "C:/fake/code.cmd")
    monkeypatch.setattr(da.time, "sleep", lambda *_: None)
    started: list = []
    monkeypatch.setattr(da.subprocess, "Popen",
                        lambda cmd, **kw: started.append(cmd))
    assert da._open_vscode(folder) is True
    assert started == [["C:/fake/code.cmd", str(folder)]]


# ── path validation (the file list is model output) ──────────────────────────

def test_safe_project_path_accepts_paths_inside_the_project(folder):
    target = da._safe_project_path(folder, "utils/helpers.py")
    assert target is not None
    assert target.parent == (folder / "utils").resolve()


@pytest.mark.parametrize("bad", [
    "../evil.py",
    "a/../../evil.py",
    "..\\evil.py",
    "",
    "   ",
])
def test_safe_project_path_rejects_escapes(folder, bad):
    assert da._safe_project_path(folder, bad) is None


def test_safe_project_path_rejects_absolute_paths(folder):
    absolute = "C:/Windows/Temp/evil.py" if da.os.name == "nt" else "/etc/passwd"
    assert da._safe_project_path(folder, absolute) is None


class _StubResponse:
    def __init__(self, text):
        self.text = text


class _StubModel:
    def __init__(self, text):
        self._text = text

    def generate_content(self, prompt):
        return _StubResponse(self._text)


def test_write_file_writes_inside_the_project(monkeypatch, folder):
    monkeypatch.setattr(da, "_get_model", lambda role: _StubModel("print('hi')"))
    code = da._write_file(
        file_info={"path": "pkg/mod.py", "description": "x", "imports": []},
        project_description="d",
        all_files=[{"path": "pkg/mod.py", "description": "x"}],
        language="python",
        project_dir=folder,
        already_written={},
    )
    assert (folder / "pkg" / "mod.py").read_text(encoding="utf-8") == code


def test_write_file_refuses_an_escaping_path(monkeypatch, folder):
    monkeypatch.setattr(da, "_get_model", lambda role: _StubModel("print('hi')"))
    with pytest.raises(ValueError):
        da._write_file(
            file_info={"path": "../escape.py", "description": "x", "imports": []},
            project_description="d",
            all_files=[{"path": "../escape.py", "description": "x"}],
            language="python",
            project_dir=folder,
            already_written={},
        )
    assert not (folder.parent / "escape.py").exists()


def test_fix_files_refuses_an_escaping_path(monkeypatch, folder):
    monkeypatch.setattr(da, "_get_model", lambda role: _StubModel("print('fixed')"))
    traceback_text = (
        'Traceback (most recent call last):\n'
        '  File "C:/tmp/escape.py", line 3, in <module>\n'
        'ValueError: boom'
    )
    updated = da._fix_files(
        error_output=traceback_text,
        project_description="d",
        all_files=[{"path": "../escape.py", "description": "x"}],
        file_codes={"../escape.py": "print('broken')"},
        language="python",
        project_dir=folder,
        entry_point="main.py",
    )
    assert updated == {}
    assert not (folder.parent / "escape.py").exists()


# ── code_helper mirror ────────────────────────────────────────────────────────

def test_code_helper_silent_non_zero_exit_is_a_failure(folder):
    path = _write(folder, "silent.py", "raise SystemExit(3)\n")
    outcome = ch._run_file(path, [], 30)
    assert outcome.exit_code == 3
    assert ch._has_error(outcome) is True


def test_code_helper_benign_counts_are_not_an_error():
    # "0 failed" and "0 errors" are the false positives the old word list made.
    assert ch._has_error(ch._RunOutcome("Output:\n0 failed, 12 passed", exit_code=0)) is False
    assert ch._has_error(ch._RunOutcome("Output:\n0 errors, 12 checks passed", exit_code=0)) is False
    assert ch._has_error(ch._RunOutcome("Output:\nerrors=0", exit_code=0)) is False


def test_code_helper_traceback_at_exit_zero_is_still_an_error():
    outcome = ch._RunOutcome(
        "Output:\nTraceback (most recent call last):\nValueError: x", exit_code=0)
    assert ch._has_error(outcome) is True
    assert ch._has_error(ch._RunOutcome("Output:\nError: something", exit_code=0)) is True


def test_code_helper_run_action_surfaces_the_exit_status(folder):
    path = _write(folder, "silent.py", "raise SystemExit(3)\n")
    text = ch._run_action(str(path), [], 30, None)
    assert "exited with status 3" in text


def test_code_helper_timeout_is_reported_not_claimed_working(folder):
    path = _write(folder, "slow.py", "import time\n\ntime.sleep(30)\n")
    outcome = ch._run_file(path, [], 1)
    assert outcome.timed_out is True
    assert ch._has_error(outcome) is False
    text = ch._run_action(str(path), [], 1, None)
    assert "kept running" in text


# ── the run command is model output: argv only, allowlisted heads ────────────

def test_run_command_refuses_an_arbitrary_executable(folder):
    # A model-named executable must never be launched: no shell, allowlist only.
    outcome = da._run_project("powershell -Command Remove-Item -Recurse", folder, 5)
    assert outcome.started is False
    assert da._has_error(outcome) is True
    assert "will not run" in outcome.output


def test_run_command_allowlist_covers_python_and_node_only():
    assert da._RUN_HEADS["python"] == "python"
    assert da._RUN_HEADS["py"] == "python"
    assert da._RUN_HEADS["node"] == "node"
    for illegal in ("powershell", "cmd", "bash", "curl", "rm", "sh"):
        assert illegal not in da._RUN_HEADS


def test_run_command_handles_quoted_arguments_without_a_shell(folder):
    _write(folder, "my script.py", "print('quoted ok')\n")
    outcome = da._run_project('python "my script.py"', folder, 30)
    assert outcome.exit_code == 0
    assert "quoted ok" in outcome.output


def test_run_command_with_unbalanced_quotes_is_refused_not_guessed(folder):
    outcome = da._run_project('python "broken.py', folder, 5)
    assert outcome.started is False
    assert "unbalanced quotes" in outcome.output


def test_split_run_command_never_returns_shell_text():
    assert da._split_run_command("") == []
    assert da._split_run_command("   ") == []
    assert da._split_run_command('python "a b.py"') == ["python", "a b.py"]
    # Windows backslash paths survive (POSIX shlex would eat them).
    assert da._split_run_command(r"python C:\proj\main.py") == ["python", r"C:\proj\main.py"]


# ── the plan is model output: validated before any side effect ───────────────

def _validate(plan, language="python", root=None):
    return da._validate_plan(plan, language, root or Path("."))


def test_plan_schema_requires_at_least_one_file():
    ok, why = _validate({"files": [], "entry_point": "main.py"})
    assert ok is None and "no files" in why


def test_plan_schema_caps_the_file_count():
    files = [{"path": f"f{i}.py"} for i in range(da.MAX_PLAN_FILES + 1)]
    ok, why = _validate({"files": files, "entry_point": "f0.py"})
    assert ok is None and "limit" in why


def test_plan_schema_rejects_an_entry_point_not_in_the_files():
    ok, why = _validate({"files": [{"path": "main.py"}], "entry_point": "app.py"})
    assert ok is None and "entry point" in why


def test_plan_schema_rejects_an_escaping_path():
    ok, why = _validate({"files": [{"path": "../evil.py"}], "entry_point": "../evil.py"})
    assert ok is None and "escape" in why


def test_plan_schema_rejects_duplicate_paths():
    ok, why = _validate({"files": [{"path": "a.py"}, {"path": "./a.py"}],
                         "entry_point": "a.py"})
    assert ok is None and "repeats" in why


def test_plan_schema_rejects_a_foreign_run_command():
    ok, why = _validate({"files": [{"path": "main.py"}], "entry_point": "main.py",
                         "run_command": "powershell -c evil"})
    assert ok is None and "run command starts with" in why


def test_plan_schema_rejects_a_pip_option_dependency():
    ok, why = _validate({"files": [{"path": "main.py"}], "entry_point": "main.py",
                         "dependencies": ["--index-url=http://evil/"]})
    assert ok is None and "pip option" in why


def test_plan_schema_rejects_an_unsupported_language():
    ok, why = _validate({"files": [{"path": "main.py"}], "entry_point": "main.py"},
                        language="cobol")
    assert ok is None and "not supported" in why


def test_plan_schema_accepts_and_normalises_a_good_plan():
    ok, why = _validate({"files": [{"path": "pkg\\mod.py", "imports": ["os"]}],
                         "entry_point": "pkg/mod.py",
                         "dependencies": "requests, flask",
                         "run_command": "python pkg/mod.py"})
    assert why == ""
    assert ok["files"][0]["path"] == "pkg/mod.py"
    assert ok["dependencies"] == ["requests", "flask"]
    assert ok["entry_point"] == "pkg/mod.py"


def test_plan_schema_infers_a_missing_run_command():
    ok, why = _validate({"files": [{"path": "main.py"}], "entry_point": "main.py"})
    assert why == "" and ok["run_command"] == "python main.py"


def test_a_malformed_plan_creates_nothing(monkeypatch, folder):
    monkeypatch.setattr(da, "PROJECTS_DIR", folder)
    monkeypatch.setattr(da, "_plan_project",
                        lambda desc, lang: {"files": [], "entry_point": "main.py"})
    wrote: list = []
    monkeypatch.setattr(da, "_write_file", lambda **kw: wrote.append(kw) or "x")

    msg = da._build_project("do a thing", "python", "proj", 5)
    assert "not usable" in msg
    assert wrote == []
    assert not (folder / "proj").exists()


def test_build_never_writes_into_an_existing_project_folder(monkeypatch, folder):
    existing = folder / "proj"
    existing.mkdir()
    (existing / "keepme.txt").write_text("user data", encoding="utf-8")

    monkeypatch.setattr(da, "PROJECTS_DIR", folder)
    monkeypatch.setattr(da, "MAX_FIX_ATTEMPTS", 1)
    monkeypatch.setattr(da, "_plan_project", lambda desc, lang: _plan())
    monkeypatch.setattr(da, "_open_vscode", lambda d: True)
    monkeypatch.setattr(da, "_ensure_project_venv", lambda d: (sys.executable, ""))
    monkeypatch.setattr(da, "_run_project",
                        lambda *a, **kw: da._RunOutcome("", exit_code=0))
    monkeypatch.setattr(da.time, "sleep", lambda *_: None)
    seen: dict = {}
    monkeypatch.setattr(da, "_write_file",
                        lambda **kw: seen.update(kw) or "print('hi')\n")

    da._build_project("do a thing", "python", "proj", 5)

    assert (existing / "keepme.txt").read_text(encoding="utf-8") == "user data"
    assert (folder / "proj-2").is_dir()
    assert seen["project_dir"] == folder / "proj-2"


def test_write_file_refuses_an_oversized_response(monkeypatch, folder):
    monkeypatch.setattr(da, "MAX_FILE_CHARS", 200)
    monkeypatch.setattr(da, "_get_model", lambda role: _StubModel("x" * 500))
    with pytest.raises(ValueError):
        da._write_file(
            file_info={"path": "big.py", "description": "x", "imports": []},
            project_description="d",
            all_files=[{"path": "big.py"}],
            language="python",
            project_dir=folder,
            already_written={},
        )
    assert not (folder / "big.py").exists()
