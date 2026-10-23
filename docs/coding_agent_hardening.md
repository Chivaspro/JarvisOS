# Coding agent hardening — `dev_agent` / `code_helper`

Date: 2026-10-17. Scope: the built-in coding tools only. No prompts, tool
declarations, approval/permission gates or model routing were changed.

## What changed and why

| Behaviour | Before | Now |
|---|---|---|
| Success detection | judged only by the **text** the program printed, so a silent non-zero exit was reported as "the project is working" | the process **exit status** decides (`actions/dev_agent.py::_has_error`, `actions/code_helper.py::_has_error`); a non-zero exit is a failure even with no output |
| Timeout | reported as success ("long-running app is likely working") | a clean timeout is reported as *"kept running for Ns with no error seen … I stopped it"* — never "working"; a timeout whose captured output **does** contain an error is treated as a failure and goes through the fix loop, so the report can never claim "no error seen" while showing a traceback |
| Never started | "Command not found" counted as no-error | a run that never started counts as **not verified** (a failure) |
| Timeout cleanup | only the direct child was killed | the whole process tree is stopped (`_kill_process_tree`, `taskkill /T` on Windows) |
| Credential | a missing Gemini key surfaced mid-build as a raw failure | refused up front with an actionable sentence, before any model call or approval card (`dev_agent`) |
| Dependency installs | `pip install` ran in **JARVIS's own interpreter** | **every Python project gets its own `.venv`** under `Desktop/JarvisProjects/<project>/` (not only projects that declare dependencies, so the auto-install path has a project venv to use); falls back to JARVIS's interpreter **and says so** when venv creation fails |
| Declared dependency list | entries went to `pip install` verbatim, so a plan containing `--index-url=…` or `-r requirements.txt` reached pip as an *option* | every entry is validated with `core.dependency_ops.is_valid_requirement` (the same `_NAME_RE` the diagnostics path uses); refused entries are reported and never passed to pip |
| Auto-install of a missing module | pip-installed the token from the traceback, so a project-internal import (`utils`) or a stdlib name (`json`) pulled a wrong package | resolves through the repo's authoritative **`core.dependency_ops.IMPORT_TO_PIP`** (one table, no local copy — a local copy had already started installing the deprecated `google` package where the authoritative table resolves `google-genai`); refuses stdlib names, the project's own modules, and non-module tokens |
| File paths | a planned `files[].path` was joined unchecked (`project_dir / path`) | `_safe_project_path()` refuses absolute, drive-qualified, UNC and `..`-escaping paths in both the write and fix loops (and the plan validator added in parallel reuses it before anything is created on disk) |
| VS Code | reported "opened" for any `Popen` that did not raise (`shell=True`) | the launcher is resolved with `shutil.which`/existence first; otherwise "VS Code was not found — the project is saved at …" |
| `code_helper` output words | `"stderr"`, `"failed"`, `"crash"` anywhere in the output meant "error", so `"0 failed"` triggered a fix loop | exit status first; for an exit-0 run only **traceback-shaped** evidence counts (`traceback (most recent call last)`, `error:`, exception-class names), so `"0 failed"` / `"0 errors"` no longer cause three wasted model calls and a needless rewrite |

Unchanged on purpose: the `dev_agent(parameters, response, player,
session_memory, speak)` public signature (the dispatcher in `main.py` binds to
it), the approval gate (`security/coding_approval.py`), the permission tiers,
the model-router roles ("reasoning" planner / "coding" writer), the plan/write/
fix prompts, and `PROJECTS_DIR`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_dev_agent.py -q -p no:cacheprovider
```

44 tests, offline: the model is never called and nothing is pip-installed. The
run-detection tests spawn real child processes (a silent `SystemExit(3)`, a
raising script, a 30-second sleeper, and a script that prints a traceback and
*then* hangs) because the defect being pinned is about a real process's exit
status and the error evidence it left behind — a text-only double would not have
caught it.

## Known limitation (unchanged)

The planner's `run_command` is still executed as-is (no shell, `cwd=project_dir`).
The first token is replaced by the project/JARVIS interpreter only when it is
`python`/`python3`/`py`; any other executable the model names runs directly. The
coding approval card and the `terminal_control` permission gate cover this, but
restricting `run_command` to an allow-list remains open (tracked in the root
README's "Known maintenance items").
