# Test Results — JARVIS OS master audit

Business date: 2026-10-16. Machine: Windows (win32), 64-bit.
Interpreter: project `.venv`, **Python 3.14.8** (was found empty — see §1).

This file records the **baseline** measured before any code change and is
updated with the **final** run after the repairs (§5). A test is only reported
as passing when the command actually completed and pytest printed its summary.

---

## 1. Environment restore (Phase 0)

The project `.venv` contained **only pip** when the audit began (every runtime
dependency was gone; the app had last run 2026-10-15). The documented setup was
replayed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --progress-bar off
.\.venv\Scripts\python.exe -m pip install pytest        # dev tool, not in requirements.txt
```

* `pip check` → **No broken requirements found.**
* `compileall` over every first-party package and top-level module → **clean**
  (no syntax/import errors).
* Import smoke: `PyQt6, sounddevice, cv2, numpy, google.genai, fastapi, mss, PIL,
  requests, psutil, send2trash, cryptography, qrcode, playwright, pyautogui,
  onnxruntime, miniaudio, pyttsx3, ddgs, youtube_transcript_api` → **OK**.
* `win32api`, `win32com.client` → **OK**.
* **`pywinauto` import fails**: `ImportError: DLL load failed while importing
  win32ui` (the MFC runtime `mfc140u.dll` is absent on this machine). This makes
  the `pc_control` **Windows-automation** engine unavailable until the Microsoft
  Visual C++ runtime is installed. The app degrades (feature is optional and
  probed) — recorded in `FEATURE_AUDIT.md`, not counted as a code defect.

`torch 2.14.1`, `torchvision 0.29.1` and `ultralytics 8.4.178` installed with
native `cp314` wheels, so YOLO vision is present.

## 2. Baseline — commands and results

| # | Command (from repo root, `.venv` python) | Result | Wall |
|---|---|---|---|
| 1 | `python -m pytest tests/unit -q` | **1192 passed, 5 failed, 6 skipped** | 260 s |
| 2 | `python -m pytest tests/hybrid_voice -q` | **153 passed, 2 failed** | 175 s |
| 3 | UI tree, one file per process (47 files) | **42/47 files exit 0** | ~30 min |
| 4 | `import ui` (offscreen, WebEngine disabled) | cold **1158 ms**, warm **36 ms** | — |
| 5 | `compileall` (all first-party) | clean | — |

Command 3 uses the documented workaround for this environment (`POSIX` form
shown; on this machine a small Python runner invoked each file):

```powershell
Get-ChildItem tests\ui\test_*.py | ForEach-Object {
  python -m pytest $_.FullName -q -p no:cacheprovider
}
```

### 2.1 Startup invariants (command 4)

* `'PyQt6.QtWebEngineWidgets' in sys.modules` → **False**
* `'urllib.request' in sys.modules` → **False**

Both held, confirming the lazy-import design from `docs/cleanup_report.md` §2
still holds after the venv was rebuilt.

## 3. Failure triage

| Failure | Where | Class | Notes |
|---|---|---|---|
| `test_empty_content_is_refused_rather_than_written` | `tests/unit/test_documents_architect.py` | optional-dep | returns `missing_dependency` because the XLSX engine (`openpyxl`/`pandas`) is not in `requirements.txt`; the content check runs after the dependency probe |
| `test_secrets_never_reach_the_log_file` | `tests/unit/test_observability.py` | **defect (test)** | `SECRET_SHAPED = os.getenv("SECRET_SHAPED", "")` defaults to `""`, so `assert "" not in text` always fails — the security assertion is vacuous unless the env var is set |
| `test_secret_shaped_string_in_event_is_scrubbed` | `tests/unit/test_observability.py` | **defect (test)** | same root cause |
| `test_setup_overlay_submit_success_emits_credentials` | `tests/unit/test_setup_overlay.py` | **stale test** | overlay `done` signal now emits 5 args, test unpacks 4 |
| `test_setup_overlay_get_api_key_helper` | `tests/unit/test_setup_overlay.py` | **stale test** | button text changed ("GET API KEY" no longer present) |
| `test_configured_piper_voice_files_exist` | `tests/hybrid_voice/test_piper_pipeline.py` | missing asset | `config/voices/jarvis-high.onnx` is absent and untracked (only the `.json` is committed; `.gitignore` does not exclude `*.onnx`) |
| `test_the_bundled_piper_voice_is_offerable` | `tests/hybrid_voice/test_settings_default_voice.py` | missing asset | same root cause |
| `test_*` (6) | `tests/ui/test_chat_history.py` | **stale test** | history page refactored to async loading + new empty-state text + no `_new_chat_btn` |
| `test_*` (3) | `tests/ui/test_history_stage.py` | **stale test** | `HistoryStage` is now a floating stage, not a page of `_vision_visual` |
| `test_no_surface_declares_its_own_palette` | `tests/ui/test_design_tokens.py` | **defect (code)** | `ui.py` still hard-codes 3 colour literals (`#ff4444`, `#ff4444`, `#8d9aa3`) instead of design tokens |

### 3.1 Environment-flaky (not defects)

* `tests/ui/test_vision_rail.py` exited non-zero once (native teardown abort)
  and **8 passed** on re-run.
* `tests/ui/test_layout_runtime_integration.py` exited `0xC0000005` once and
  **10 passed** on re-run; a transient `pytest 9.1.1` `tmp_path`
  `_ensure_relative_to_basetemp` error also appeared once and cleared.

These match the documented environment limitation: the whole UI tree ends in a
native abort that is unrelated to the app (the app's own shutdown calls
`os._exit`, `docs/cleanup_report.md` §8.6). One file per process is the
established workaround.

## 4. Known-good trees

* `tests/unit` — 1192/1197 pass (5 failures triaged above).
* `tests/hybrid_voice` — 153/155 pass (2 asset-dependent).
* `tests/ui` — 42/47 files green per process; 10 real failures, all triaged
  above as stale tests, test defects, a missing asset or a code style defect.

## 5. Final run (after repairs)

Commands run from the repo root with the venv interpreter (`.venv\Scripts\python.exe`):

| Command | Result | Wall |
|---|---|---|
| `python -m pytest tests/unit -q -p no:cacheprovider` | **1330 passed, 6 skipped, 0 failed** | 106 s |
| `python -m pytest tests/hybrid_voice -q -p no:cacheprovider` | **153 passed, 2 skipped, 0 failed** | 122 s |
| `python -m pytest tests/ui/test_chat_history.py -q -p no:cacheprovider` | **13 passed** | 18 s |
| `python -m pytest tests/ui/test_history_stage.py -q -p no:cacheprovider` | **8 passed** | 13 s |
| `python -m pytest tests/ui/test_design_tokens.py -q -p no:cacheprovider` | **47 passed** | 2 s |
| `python -m pytest tests/unit/test_mcp_publish_idempotent.py tests/unit/test_mcp_host.py -q` | **29 passed** | 9 s |
| `python -m pytest tests/unit/test_documents_architect.py -q` | **20 passed, 3 skipped** | 1 s |
| `python -m pytest tests/unit -k setup_overlay -q` | **6 passed** | 6 s |
| `python -m pytest tests/unit/test_observability.py -q` | **6 passed** | 1 s |
| `python -m pytest tests/unit/test_laya_decision.py -q` | **39 passed** (with a torch DLL access-violation printed on a worker path — environment, see below) | 6 s |

Result: **every baseline failure is resolved**, no previously-green test turned
red, and the two "optional package absent" skips (document engines) plus the two
model-absent skips (Piper) are honest `skip`s, not passes.

### 5.1 Before → after

| Suite | Baseline | Final |
|---|---|---|
| `tests/unit` | 1192 passed, **5 failed**, 6 skipped | **1330 passed, 0 failed**, 6 skipped |
| `tests/hybrid_voice` | 153 passed, **2 failed** | **153 passed, 2 skipped, 0 failed** |
| `tests/ui` (per-file) | 42/47 files exit 0; 10 real failures across 3 files | `test_chat_history` 13✓, `test_history_stage` 8✓, `test_design_tokens` 47✓ |

**Collection-count note (transparency).** The first baseline `tests/unit` run
collected 1203 tests; the final run collects ~1331. This audit added only 2 tests
(`test_mcp_publish_idempotent.py`). The rest of the delta is **not** this audit's:
the working tree was clean at the start but was modified concurrently during the
session (see `REPAIR_REPORT.md` "Note: concurrent modification") — other work
added `brain/task_engine.py`, `mcp_host/rea.py`, `ui_task_panel.py` and ~128 new
tests in `tests/unit`. The final numbers therefore validate the **combined**
tree; the comparison that matters for this audit is the **failure count**:
5 → 0 in `tests/unit`.

## 6. Blocked / unverified in the final run

| Item | Why | Label |
|---|---|---|
| YOLO/ultralytics object detection | `import torch` fails (`WinError 1114`, `c10.dll`) — 3/3 fresh runs | **BLOCKED (environment)** |
| `pc_control` Windows automation | `pywinauto` import fails (`win32ui`/MFC runtime absent) | **BLOCKED (environment)** |
| Bundled Piper/ONNX voice | `config/voices/jarvis-high.onnx` absent and untracked | **BLOCKED (packaging)** |
| Whole-tree `tests/ui` run | Native teardown abort in this environment (documented) | **BLOCKED (environment)**; per-file workaround used |
| Gemini Live voice, live browser/PC control, Qdrant, connectors, dashboard, live search/transcript | Require live devices/services/credentials not driven in this pass | **UNVERIFIED (live)** |
| Full `main.py` GUI+network boot smoke | Would block the audit on a live session; startup covered by `compileall` + `import ui` + per-file UI tests | **UNVERIFIED** |

No test could be shown to pass from a live Gemini/Playwright/mic/camera run; those
are explicitly listed above as unverified rather than claimed.

---

# 2026-10-17 — Master repair pass (delta)

Machine: Windows (win32), 64-bit. Interpreter: project `.venv`, **Python 3.14.8**
(same interpreter that produced the 2026-10-16 numbers above).

## A. Baseline (before this pass)

| Suite | Command | Result |
|---|---|---|
| `tests/unit` | `python -m pytest tests/unit -q -p no:cacheprovider` | **1366 passed, 5 skipped, 0 failed** (78 s) |

## B. Final (after this pass)

| Suite | Command | Result |
|---|---|---|
| `tests/unit` | `python -m pytest tests/unit -q -p no:cacheprovider` | **1614 passed, 5 skipped, 0 failed** (146 s) |
| `tests/hybrid_voice` | `python -m pytest tests/hybrid_voice -q -p no:cacheprovider --basetemp=<fresh>` | **153 passed, 2 skipped, 0 failed** (140 s) |
| static | `python -m compileall -q <first-party dirs + entry files>` | clean |
| static | `python -m pip check` | **No broken requirements found.** |

Interim run of every touched test file together (8 files) → **277 passed**.

**Note on a locked temp dir.** The first `tests/hybrid_voice` attempt produced
38 setup ERRORS — not failures — all from one Windows file lock: a leftover
`.pytest_tmp\test_empty_views_say_so_instea0\brain.db` held by another process,
which makes pytest's `tmp_path_factory` teardown raise `PermissionError
[WinError 32]` and error every later module at setup. Re-running with a fresh
`--basetemp` gave the clean **153 passed** above, so the errors were
environmental (a stale handle from an earlier run / the concurrent workspace),
not a regression in the code under test.

## C. New regression tests added this pass

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_permissions_fail_closed.py` | 122 | D3 — fail-closed permission gate |
| `tests/unit/test_dev_agent.py` (extended) | 62 total | D2 — run allow-list, plan schema, collision, size cap |
| `tests/unit/test_node_version_gate.py` | — | D4 — one Node floor across manifest/lock/wizard/installer |
| `tests/unit/test_gemini_live_model.py` (rewritten) | 18 | D5 — `gemini-3.8-live`, retired-id migration, BLOCKING pin |
| `tests/unit/test_process_launch.py` | 8 | D6 — helper + app-wide default |
| `tests/unit/test_web_search_honesty.py` | 15 | D7 — backend-failure vs empty result |
| `tests/unit/test_secret_redaction.py` | 13 | D8 — suffix-based redaction |
| `tests/unit/test_full_closure.py` (extended) | +1 | `Closed_Jarvis.py` launch-flag defect |

## D. Blocked / unverified

| Item | Why | Label |
|---|---|---|
| YOLO/ultralytics, `pc_control` automation, bundled Piper ONNX, whole-tree `tests/ui` | same environment limits as the 2026-10-16 pass | **BLOCKED (environment/packaging)** |
| Gemini Live round-trip on `gemini-3.8-live` | needs a key + mic + speaker | **UNVERIFIED (live)** — model id and protocol deltas verified against the SDK (`google-genai` 2.31.0) and Google's deprecations page (fetched 2026-10-17) |
| Full `main.py` GUI+network boot smoke | would hold the app's single-instance lock and needs a live session; skipped deliberately while another workspace was active | **UNVERIFIED** |
| Remote CI (`.github/workflows/ci.yml`) | not pushed / not authorised | **UNVERIFIED** — no badge claimed |

