import os
import shutil
import shlex
import subprocess
import sys
import json
import re
import time
from pathlib import Path
from typing import NamedTuple


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
# Model-router roles: planner needs to think, writer needs to code.
# Resolve + fallback live in core/model_router.py (Settings → AI → Model roles).
MODEL_PLANNER    = "reasoning"
MODEL_WRITER     = "coding"

# ── limits on model-generated build plans ─────────────────────────────────────
# A plan and its run command are model output: they must not be able to turn a
# single request into an unbounded number of model calls, a disk-filling build,
# or an arbitrary executable launched on this machine.
MAX_PLAN_FILES = 40
MAX_FILE_CHARS = 400_000
_LANGUAGES = {"python", "py", "javascript", "js", "typescript", "ts"}
#: First token of a planned run command → how it is launched. Anything else is
#: refused rather than executed (argv list only — no shell, no guessing).
_RUN_HEADS = {
    "python": "python", "python3": "python", "py": "python",
    "node": "node", "nodejs": "node",
    "npm": "npm", "npx": "npx", "yarn": "yarn", "pnpm": "pnpm",
}


def _split_run_command(text: str) -> list[str]:
    """argv tokens for a planned run command — never a shell.

    ``str.split()`` mangles a quoted argument (``python "my script.py"`` became
    two broken tokens); POSIX ``shlex`` would eat Windows backslash paths. So:
    non-POSIX shlex (quotes stay attached) with surrounding quotes stripped,
    which handles both shapes without ever interpreting shell syntax. An empty
    list means the command was empty or ambiguous (unbalanced quotes) — the
    caller refuses instead of guessing.
    """
    if not str(text or "").strip():
        return []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return []
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'"
            else t for t in tokens]


def _get_api_key() -> str:
    """The canonical Gemini key: environment → api_keys.json (never JSON alone).

    Delegating to ``config.settings.api_key`` keeps this module in agreement with
    Settings and the API HUB about whether a key is configured; reading the file
    directly returned an empty string while the hub reported the key as ready.
    """
    try:
        from config.settings import api_key
        return api_key("gemini_api_key")
    except Exception:
        return ""


def _get_model(model_name: str):
    """model_name is a model-router role ("reasoning"/"coding") or a raw id."""
    from core import model_router
    from google import genai
    _c = genai.Client(api_key=_get_api_key())

    class _W:
        def generate_content(self, contents):
            if model_name in model_router.ROLES:
                return model_router.generate(_c, contents=contents, role=model_name)
            return _c.models.generate_content(model=model_name, contents=contents)

    return _W()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:

    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _classify_error(output: str) -> str:

    low = output.lower()

    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    
    if "cannot import" in low or "importerror" in low:
        return "import_error"

    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    return "none"


class _RunOutcome(NamedTuple):
    """What a project run actually did.

    The previous code judged a run only by the *text* it printed, so a program
    that died with a non-zero exit status and printed nothing — or printed an
    error the text classifier did not recognise — was reported to the user as
    "working". The exit status is the one signal a subprocess always gives, so
    it is carried here and the success decision is made on it first.
    """

    output: str
    exit_code: int | None = None   # None when the process never started
    timed_out: bool = False
    started: bool = True


def _format_output(stdout: str, stderr: str) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(f"STDOUT:\n{stdout.strip()}")
    if stderr and stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    return "\n\n".join(parts) if parts else "Ran with no output."


def _has_error(outcome: "_RunOutcome") -> bool:
    """Did the run fail?

    A non-zero exit status is authoritative — it is the process's own statement
    that it did not finish successfully, and it does not depend on language,
    output buffering, or whether the message matches an English error pattern.
    A run that never started (missing interpreter/executable) also counts as not
    verified: "command not found" must never be reported as a working project.
    Text classification still runs, so a program that exits 0 but prints a
    traceback is not shrugged off. A timeout is not a failure by itself — a
    server or GUI is expected to keep running — but if the run printed an error
    before it hung, that error is real evidence and counts as a failure (so
    _build_project never claims "no error seen" while showing a traceback).
    """
    if outcome.timed_out:
        return _classify_error(outcome.output or "") != "none"
    if not outcome.started:
        return True
    if outcome.exit_code is not None and outcome.exit_code != 0:
        return True
    text = (outcome.output or "").strip()
    if not text or text == "Ran with no output.":
        return False
    return _classify_error(outcome.output) != "none"

class RateLimitError(Exception):
    pass


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".

JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _safe_project_path(project_dir: Path, rel_path: str) -> Path | None:
    """Resolve a planned file path inside the project, or None when it escapes.

    The file list is model output. A path such as ``../../evil.py``, an absolute
    path, a drive-qualified ``C:x`` or a UNC path must never let a build write
    outside the project folder, so every write goes through this check.
    """
    raw = str(rel_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    root = Path(project_dir).resolve()
    try:
        target = (root / candidate).resolve()
    except Exception:
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None            # ".." climbed out of the project
    return target


def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)

        if len(code) > MAX_FILE_CHARS:
            raise ValueError(
                f"Refusing to write {file_path!r}: the model returned "
                f"{len(code)} characters (limit {MAX_FILE_CHARS})")

        target = _safe_project_path(project_dir, file_path)
        if target is None:
            raise ValueError(f"Refusing to write outside the project: {file_path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

_VENV_DIRNAME = ".venv"


def _project_python(project_dir: Path) -> str | None:
    """The project's own interpreter, when its venv already exists."""
    rel = (Path("Scripts") / "python.exe") if os.name == "nt" else (Path("bin") / "python")
    exe = Path(project_dir) / _VENV_DIRNAME / rel
    return str(exe) if exe.exists() else None


def _ensure_project_venv(project_dir: Path) -> tuple[str, str]:
    """(interpreter, note) — give the project its own environment first.

    A generated project's dependencies used to be installed into the SAME
    interpreter that runs JARVIS, where a `pip install` can upgrade or break the
    assistant's own packages. A project-local venv keeps the two apart.

    If creation fails the assistant's interpreter is used, and the returned
    note says so — the fallback must never be silent, because a silent fallback
    is exactly how the assistant's environment got polluted before.
    """
    existing = _project_python(project_dir)
    if existing:
        return existing, ""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "venv", _VENV_DIRNAME],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=240, cwd=str(project_dir),
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "venv failed").strip()[:200])
        created = _project_python(project_dir)
        if not created:
            raise RuntimeError("the new venv has no interpreter")
        print(f"[DevAgent] 🧪 Project venv ready: {created}")
        return created, ""
    except Exception as e:
        note = (f"Could not create a project virtual environment ({e}); "
                f"installing into JARVIS's own interpreter instead.")
        print(f"[DevAgent] ⚠️ {note}")
        return sys.executable, note


def _install_dependencies(
    dependencies: list[str],
    project_dir: Path,
    python_exe: str | None = None,
) -> str:
    """Install a generated project's declared dependencies.

    Everything goes through ``python_exe`` — the project venv when one exists —
    so the assistant's own packages are never touched. Every entry is validated
    as a plain pip requirement first: the list comes from the model's plan, and
    an entry like ``--index-url=…`` or ``-r requirements.txt`` would otherwise
    reach pip as an option rather than a package (argument injection).
    """
    if not dependencies:
        return "No external dependencies."

    try:
        from core.dependency_ops import is_valid_requirement
    except Exception as e:
        # Fail closed: without the validator nothing gets installed, rather
        # than installing whatever the plan happened to contain.
        return f"Nothing installed — the requirement validator is unavailable ({e})."

    safe: list[str] = []
    refused: list[str] = []
    for dep in dependencies:
        text = str(dep).strip()
        (safe if is_valid_requirement(text) else refused).append(text)
    if refused:
        print(f"[DevAgent] 🚫 Refused unusable dependency entries: {refused}")
    if not safe:
        return (f"Nothing installed — every declared dependency was refused as "
                f"unusable: {refused}")

    interpreter = python_exe or sys.executable
    to_install = []
    for dep in safe:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        result = subprocess.run(
            [interpreter, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            to_install.append(dep)
        else:
            print(f"[DevAgent] ✓ Already installed: {pkg_name}")

    if not to_install:
        missed = (f" Refused: {refused}." if refused else "")
        return f"All dependencies already installed: {', '.join(safe)}.{missed}"

    print(f"[DevAgent] 📦 Installing: {to_install}")
    try:
        result = subprocess.run(
            [interpreter, "-m", "pip", "install"] + to_install,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=180, cwd=str(project_dir)
        )
        if result.returncode == 0:
            note = (f" Refused: {refused}." if refused else "")
            return (f"Installed into the project environment: "
                    f"{', '.join(to_install)}.{note}")
        return f"Install warning (non-fatal): {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "Dependency install timed out (non-fatal)."
    except Exception as e:
        return f"Install error (non-fatal): {e}"

_VSCODE_CANDIDATES: tuple[str, ...] = (
    "code",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Microsoft VS Code"
        / "bin" / "code.cmd"),
    r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
)


def _open_vscode(project_dir: Path) -> bool:
    """Open the project in VS Code; True only when a real launcher was found.

    ``code`` is normally on PATH and lands as ``code.cmd``; the two absolute
    Windows install paths cover the case where it is not. The old code reported
    "VSCode opened" for any Popen that did not raise — and with ``shell=True``
    that was almost always, even on a machine with no editor installed.
    """
    for cmd in _VSCODE_CANDIDATES:
        if os.path.isabs(cmd):
            exe = cmd if Path(cmd).exists() else None
        else:
            exe = shutil.which(cmd)
        if not exe:
            continue
        try:
            subprocess.Popen(
                [exe, str(project_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)
            print(f"[DevAgent] 💻 VSCode opened: {project_dir}")
            return True
        except Exception:
            continue
    return False

def _kill_process_tree(proc) -> None:
    """Stop the run and anything it spawned.

    A generated server keeps child processes; killing only the direct child
    leaves them holding the port. ``taskkill /T`` ends the tree on Windows;
    elsewhere the plain kill is what is available.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
            return
        except Exception:
            pass   # fall through to the plain kill
    try:
        proc.kill()
    except Exception:
        pass


def _run_project(
    run_command: str,
    project_dir: Path,
    timeout: int = 30,
    python_exe: str | None = None,
) -> _RunOutcome:
    """Run the project and report what actually happened (output, exit, timeout).

    Unlike the old string-returning version, the exit status is captured and the
    caller decides success from it — not from the text the program happened to
    print. On timeout the whole process tree is stopped, so a generated server
    cannot be left running after JARVIS says it stopped it.
    """
    print(f"[DevAgent] 🚀 Running: {run_command}")
    text_tokens = _split_run_command(run_command)
    if not text_tokens:
        stripped = str(run_command or "").strip()
        reason = ("Empty run command." if not stripped else
                  f"I could not read the run command {stripped!r} (unbalanced "
                  f"quotes), so I did not start it.")
        return _RunOutcome(reason, started=False)
    parts = text_tokens
    kind = _RUN_HEADS.get(parts[0].lower())
    if kind is None:
        return _RunOutcome(
            f"I will not run {parts[0]!r}: a build may only start a python or "
            f"node project. Change the run command to use python/node.",
            started=False)
    if kind == "python":
        parts[0] = python_exe or sys.executable
    else:
        resolved = shutil.which(parts[0]) or shutil.which(kind)
        if not resolved:
            return _RunOutcome(f"Command not found: {parts[0]}", started=False)
        parts[0] = resolved

    kwargs: dict = {}
    if os.name == "nt":
        # Own process group, so taskkill /T can reach grandchildren too.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            parts,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(project_dir),
            **kwargs,
        )
    except FileNotFoundError as e:
        return _RunOutcome(f"Command not found: {e}", started=False)
    except Exception as e:
        return _RunOutcome(f"Run error: {e}", started=False)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        return _RunOutcome(_format_output(stdout, stderr),
                           exit_code=proc.returncode, timed_out=True)

    return _RunOutcome(_format_output(stdout, stderr), exit_code=proc.returncode)

# ── auto-install: import name → the pip distribution that provides it ─────────
# ONE table, not a local copy: core.dependency_ops.IMPORT_TO_PIP is what the
# diagnostics/install path (main.py) and the evolution Crucible already use, so
# this module can never disagree with them about a package's PyPI name. A local
# copy drifted immediately — it installed the deprecated `google` package where
# the authoritative table resolves `google-genai`.
_STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()))
_ALIAS_CACHE: dict[str, str] | None = None


def _import_aliases() -> dict[str, str]:
    """The authoritative import→pip map, lower-cased for this module's lookups.

    Loaded lazily (and cached) so importing dev_agent never depends on core/
    at import time; an empty value means "part of the standard library — never
    pip install it", mirroring core.dependency_ops.ensure_package's meaning.
    """
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        try:
            from core.dependency_ops import IMPORT_TO_PIP
            _ALIAS_CACHE = {str(k).lower(): str(v) for k, v in IMPORT_TO_PIP.items()}
        except Exception:
            _ALIAS_CACHE = {}
    return _ALIAS_CACHE


def _pip_name_for(
    module: str,
    project_modules: frozenset[str] = frozenset(),
) -> tuple[str | None, str]:
    """(pip distribution, refusal reason) — exactly one is meaningful.

    A name is only turned into an install when that is safe: not a standard
    library module, not a module the project itself provides, and either a known
    alias or a plain module name (pip normalises ``_``/``-``/``.`` between a
    distribution and its import name, so "requests" is its own distribution).
    """
    root = (module or "").split(".")[0].strip()
    if not root:
        return None, "the error did not name a module"
    low = root.lower()
    if low in _STDLIB_MODULES:
        return None, f"'{root}' is part of the Python standard library, so installing it would not help"
    if low in project_modules:
        return None, (f"'{root}' is one of the project's own modules, so this is a "
                      f"code/import problem, not a missing package")
    alias = _import_aliases().get(low)
    if alias:
        return alias, ""
    if alias == "":
        return None, (f"'{root}' is part of the Python standard library, so "
                      f"installing it would not help")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", root):
        return root, ""
    return None, f"'{root}' is not a plain module name, so I will not guess a package for it"


def _try_auto_install(
    error_output: str,
    project_dir: Path,
    python_exe: str | None = None,
    project_modules: frozenset[str] = frozenset(),
) -> bool:
    """Install the module a ModuleNotFoundError names, when that is safe.

    Returns True only when a package was actually installed. A refusal (stdlib
    name, one of the project's own modules, an unmappable token) returns False so
    the caller falls through to the code-fix loop rather than installing the
    wrong package. Installs go to the project's own venv (``python_exe``).
    """
    pattern = re.compile(
        r"No module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE
    )
    match = pattern.search(error_output)
    if not match:
        return False

    pkg, refusal = _pip_name_for(match.group(1), project_modules)
    if not pkg:
        print(f"[DevAgent] 🚫 Not auto-installing: {refusal}")
        return False

    print(f"[DevAgent] 🔧 Auto-installing missing package: {pkg}")
    try:
        result = subprocess.run(
            [python_exe or sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=180, cwd=str(project_dir)
        )
        return result.returncode == 0
    except Exception:
        return False

def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
) -> dict[str, str]:

    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    files_to_fix: list[str] = []

    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)

            if len(fixed) > MAX_FILE_CHARS:
                print(f"[DevAgent] 🚫 Refusing to fix {fix_path}: "
                      f"{len(fixed)} chars (limit {MAX_FILE_CHARS})")
                continue

            target = _safe_project_path(project_dir, fix_path)
            if target is None:
                print(f"[DevAgent] 🚫 Refusing to fix outside the project: {fix_path!r}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fixed, encoding="utf-8")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes

def _project_module_names(files: list[dict]) -> frozenset[str]:
    """Names the project's own files provide (for the auto-install guard).

    A ModuleNotFoundError for one of these is a code/import bug to fix — not a
    package to download — so the names are passed to _try_auto_install.
    """
    names: set[str] = set()
    for fi in files or []:
        path = str(fi.get("path") or "")
        if not path:
            continue
        p = Path(path)
        if p.stem and p.stem != "__init__":
            names.add(p.stem.lower())
        if p.parts:
            first = Path(p.parts[0])
            if first.suffix == ".py":
                first = first.with_suffix("")
            if first.name and first.name != "__init__":
                names.add(first.name.lower())
    return frozenset(names)


def _fresh_project_dir(proj_name: str) -> tuple[Path, str]:
    """A project folder that will not overwrite unrelated existing content.

    The requested folder is reused only when it is empty; otherwise the first
    free ``<name>-N`` sibling is used, so an existing project (or anything else
    under JarvisProjects) is never silently written into.
    """
    def _free(p: Path) -> bool:
        try:
            return not p.exists()
        except OSError:
            return False

    def _empty_dir(p: Path) -> bool:
        try:
            return p.is_dir() and not any(p.iterdir())
        except OSError:
            return False

    base = PROJECTS_DIR / proj_name
    if _free(base) or _empty_dir(base):
        return base, proj_name
    for n in range(2, 1000):
        cand = PROJECTS_DIR / f"{proj_name}-{n}"
        if _free(cand):
            return cand, f"{proj_name}-{n}"
    return PROJECTS_DIR / f"{proj_name}-new", f"{proj_name}-new"


def _validate_plan(
    plan: dict,
    language: str,
    project_dir: Path,
) -> tuple[dict | None, str]:
    """Vet the planner's JSON before ANYTHING is created on disk.

    Returns ``(cleaned_plan, "")`` when the plan is structurally safe to build,
    else ``(None, reason)``. File paths are re-checked by ``_safe_project_path``
    at write time; this rejects an unusable plan before the first mkdir or model
    call, so a malformed plan cannot leave half a project (or a runaway build)
    behind.
    """
    lang = (language or "").strip().lower()
    if lang and lang not in _LANGUAGES:
        # An explicit, unsupported language is refused; a blank one is inferred
        # from the run command below (the model sometimes omits it).
        return None, (f"language {language!r} is not supported — I build python, "
                      f"javascript or typescript projects")
    if not isinstance(plan, dict):
        return None, "the planner did not return an object"

    files = plan.get("files")
    if not isinstance(files, list) or not files:
        return None, "the plan listed no files"
    if len(files) > MAX_PLAN_FILES:
        return None, (f"the plan asked for {len(files)} files (limit "
                      f"{MAX_PLAN_FILES})")

    cleaned: list[dict] = []
    seen: set[str] = set()
    for fi in files:
        if not isinstance(fi, dict):
            return None, "a file entry was not an object"
        path = str(fi.get("path") or "").strip().replace("\\", "/")
        if not path:
            return None, "a file entry had no path"
        target = _safe_project_path(project_dir, path)
        if target is None:
            return None, f"the file path {path!r} would escape the project folder"
        # De-duplicate on the RESOLVED path, so "a.py" and "./a.py" count once.
        key = os.path.normcase(str(target))
        if key in seen:
            return None, f"the file list repeats {path!r}"
        seen.add(key)
        imports = fi.get("imports", [])
        cleaned.append({"path": path,
                        "description": str(fi.get("description") or ""),
                        "imports": [str(x) for x in imports]
                        if isinstance(imports, list) else []})

    entry = str(plan.get("entry_point") or "").strip().replace("\\", "/")
    if not entry:
        entry = cleaned[0]["path"]
    entry_target = _safe_project_path(project_dir, entry)
    if entry_target is None or os.path.normcase(str(entry_target)) not in seen:
        return None, f"the entry point {entry!r} is not in the file list"

    deps = plan.get("dependencies", [])
    if isinstance(deps, str):
        deps = [d for d in re.split(r"[,\s]+", deps) if d]
    if not isinstance(deps, list):
        return None, "dependencies was not a list"
    deps = [str(d).strip() for d in deps if str(d).strip()]
    if any(d.startswith("-") for d in deps):
        return None, "a dependency looked like a pip option, not a package name"

    run_command = str(plan.get("run_command") or "").strip()
    if not run_command:
        run_command = f"python {entry}"
    tokens = _split_run_command(run_command)
    if not tokens:
        return None, (f"the run command {run_command!r} could not be read "
                      f"(unbalanced quotes)")
    if tokens[0].lower() not in _RUN_HEADS:
        return None, (f"the run command starts with {tokens[0]!r} — I only start "
                      f"python or node projects")

    return ({"files": cleaned, "entry_point": entry, "dependencies": deps,
             "run_command": run_command}, "")


def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
) -> str:

    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)

    # Pick a folder that will not overwrite unrelated existing content, then vet
    # the plan BEFORE creating anything: a malformed plan must leave no mkdir,
    # no model call and no half-built project behind.
    project_dir, proj_name = _fresh_project_dir(proj_name)
    plan, plan_error = _validate_plan(plan, language, project_dir)
    if plan_error:
        msg = (f"I stopped before creating anything, sir — the project plan was "
               f"not usable: {plan_error}.")
        if speak: speak(msg)
        return msg

    files        = plan["files"]
    entry_point  = plan["entry_point"]
    run_command  = plan["run_command"]
    dependencies = plan["dependencies"]
    project_modules = _project_module_names(files)

    project_dir.mkdir(parents=True, exist_ok=True)

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except Exception as e:
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    # Its own environment first — and for EVERY Python project, not only those
    # that declare dependencies: the auto-install path must never fall back to
    # JARVIS's own interpreter, and a project that omitted a dependency from its
    # plan is exactly the case auto-install exists to handle.
    command = run_command.strip().split()
    is_python = (language.lower() in ("python", "py")
                 or (command and command[0].lower() in ("python", "python3", "py")))
    project_python = _project_python(project_dir)
    if is_python:
        project_python, venv_note = _ensure_project_venv(project_dir)
        if venv_note:
            log(venv_note)

    if dependencies:
        install_result = _install_dependencies(dependencies, project_dir, project_python)
        log(install_result)

    if not _open_vscode(project_dir):
        log(f"VS Code was not found — the project is saved at {project_dir}")

    outcome       = _RunOutcome("", started=False)
    auto_installs = 0

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        log(f"Running project (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")
        outcome = _run_project(run_command, project_dir, timeout, python_exe=project_python)
        log(f"Output preview: {outcome.output[:150]}")

        if not _has_error(outcome):
            if outcome.timed_out:
                # Honest wording: nothing failed, but "working" was never
                # observed either — the run was stopped on the timeout.
                msg = (
                    f"'{proj_name}' kept running for {timeout}s with no error seen, sir — "
                    f"that is expected for a server or a GUI app. I stopped it to carry on. "
                    f"Saved to: {project_dir}"
                )
            else:
                msg = (
                    f"Project '{proj_name}' runs and exits cleanly, sir. "
                    f"Built in {attempt} attempt{'s' if attempt > 1 else ''}. "
                    f"Saved to: {project_dir}"
                )
            if speak: speak(msg)
            body = f"\n\nOutput:\n{outcome.output}" if outcome.output.strip() else ""
            return f"{msg}{body}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        # A silent non-zero exit still deserves an explanation in the fix prompt:
        # the exit status is the only evidence such a program gave us.
        reported = outcome.output
        if outcome.exit_code not in (0, None) and _classify_error(reported) == "none":
            reported = (f"{reported}\n\n[the program exited with status "
                        f"{outcome.exit_code} and printed no recognised error]")

        error_type = _classify_error(reported)
        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(
                reported, project_dir,
                python_exe=project_python, project_modules=project_modules,
            )
            if installed:
                auto_installs += 1
                log("Missing dependency installed, retrying...")
                time.sleep(1)
                continue

        log(f"Fixing errors (type: {error_type})...")
        try:
            updated = _fix_files(
                error_output=reported,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
            )
            file_codes.update(updated)
            time.sleep(1)
        except RateLimitError:
            msg = "Rate limit reached during fix. Project saved, check it manually in VSCode."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Fix step failed: {e}")

    msg = (
        f"I couldn't fully fix '{proj_name}' after {MAX_FIX_ATTEMPTS} attempts, sir. "
        f"Project is saved at {project_dir} — open it in VSCode and check manually."
    )
    if speak: speak(msg)
    exit_note = (f"exit {outcome.exit_code}" if outcome.exit_code is not None
                 else "no exit status")
    return f"{msg}\n\nLast error ({exit_note}):\n{outcome.output[:600]}"


def _coerce_timeout(value, default: int = 30, low: int = 1, high: int = 600) -> int:
    """A usable run timeout from whatever the model sent.

    The tool declaration says INTEGER, but the model can still emit "thirty" or
    a number far outside anything sensible. Junk falls back to the default and
    the result is clamped, so a bad value can never mean "no time at all".
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = default
    return max(low, min(high, seconds))


def _describe_model_failure(error: Exception) -> str:
    """A sentence the user can act on, from the router's error classification."""
    try:
        from core import model_router
        kind = model_router.classify_error(error)
    except Exception:
        kind = "unknown"
    if kind == "auth":
        return ("Gemini rejected the API key, sir — nothing was built. Fix the key "
                "in Settings → AI and ask me again.")
    if kind in ("rate_limit", "quota"):
        return "Gemini is rate-limiting me, sir — nothing was built. Try again shortly."
    if kind in ("timeout", "provider"):
        return ("I could not reach Gemini to write the code, sir — nothing was built. "
                "Check the connection and try again.")
    if kind == "context_overflow":
        return ("That project is too big for one planning prompt, sir — ask for a "
                "smaller project, or split it into parts.")
    return f"The build stopped before it finished, sir: {error}"


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = _coerce_timeout(p.get("timeout", 30))

    if not description:
        return "Please describe the project you want me to build, sir."

    try:
        from config.settings import get_feature
        if not get_feature("coding_agents", True):
            return "Coding is switched off in Settings, so I have not done that."
    except Exception:
        pass

    # Fail fast and clearly when there is no model to write code with: the raw
    # SDK error used to surface only mid-build, as a generic tool failure.
    if not _get_api_key():
        msg = ("No Gemini API key is configured, sir, so the coding agent has no "
               "model to write with. Add the key in Settings → AI (or set "
               "GEMINI_API_KEY) and ask me again.")
        if speak: speak(msg)
        return msg

    try:
        from security.coding_approval import request_approval
        title = f"Build: {description[:80]}"
        detail = (f"Language: {language}\n"
                  f"Project name: {project_name or '(auto)'}\n\n"
                  f"What I'm about to do: plan a file structure, write the "
                  f"project files, install any dependencies into the project's "
                  f"own virtual environment, and run it to check for errors."
                  f"\n\n{description[:400]}")
        if not request_approval(title, detail):
            return "Understood, sir — I have not started that build. Say the word and I will."
    except Exception as exc:
        # Fail closed: coding without a working approval gate is exactly
        # what this gate exists to prevent.
        return f"The coding approval gate is unavailable ({exc}); I have not started that build."

    try:
        return _build_project(
            description  = description,
            language     = language,
            project_name = project_name,
            timeout      = timeout,
            speak        = speak,
            player       = player,
        )
    except RateLimitError:
        msg = "Gemini is rate-limiting me, sir. Nothing was built — try again in a moment."
        if speak: speak(msg)
        return msg
    except Exception as e:
        # A build that dies before finishing (bad key, no network, an SDK change)
        # becomes an actionable sentence instead of a raw exception string.
        msg = _describe_model_failure(e)
        if speak: speak(msg)
        return msg