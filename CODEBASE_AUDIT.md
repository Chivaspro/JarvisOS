# CODEBASE AUDIT — JARVIS OS V7

Audit + repair pass. Business date: 2026-10-09. Working tree: no Git commits
(everything untracked), so all changes were made surgically and none are
destructive. Companion ledger: `TODO.md`.

---

## 1. Scan coverage

Inspected (first-party only):

| Area | What was read |
| --- | --- |
| Entry chain | `main.py` (~7.6k lines: boot, `_DeferredModules`, `JarvisLive`, tool dispatch, shutdown), `start_jarvis.py`, `run_jarvis.bat/.vbs`, `install_dependencies.bat`, `setup_jarvis.py` (spot checks) |
| UI framework | `ui.py` (~23.5k lines: HUD, `_ActivityTimeline`, command window, panels), `ui_settings.py` (Settings + PERFORMANCE tab wiring), `ui_perfomance.py` (full read), `ui_theme.py` (spot checks) |
| Runtime state | `data/logs/jarvis_crash.log` (26 native dumps), `data/logs/jarvis-20261009.jsonl`, `jarvis_startup_error.log` (stale) |
| Dependencies | `.venv/` (Python 3.14.8), `requirements.txt`, `core/dependency_ops.py` (full read of reconcile path) |
| Subsystems (targeted) | `core/stability.py` (shutdown/faulthandler), `core/laya_decision.py` (load path), `voice/wake.py` (detector + manager), `vision/config.py` (`detect_cuda`), `mcp_host/bridge.py` + `manager.py` (publish path), `app/plugin_loader.py` (registry), `observability/` (aliases) |
| Tests | `pytest.ini`, `tests/conftest.py`, `tests/ui/conftest.py`, `tests/unit/test_laya_decision.py`, `tests/unit/test_mcp_host.py`, `tests/ui/test_performance_tab.py`, runner conventions |

Excluded on purpose (vendored/generated, per project rules): `.venv/`,
`node_modules/` (Agent TARS), `AgentReach/`, `UI-TARS/`, `models/` weights,
`assets/`, `build/`, `dist/`, `__pycache__/`, caches.

## 2. Architecture (as found)

- **Launch chain:** `run_jarvis.bat/.vbs` → `start_jarvis.py` (stdlib-only
  interpreter resolver) → `main.py` → stability guards → single-instance
  mutex → PyQt6 HUD (`ui.py`) → background backend thread (`_DeferredModules`
  imports, then Gemini Live session + tool dispatch).
- **UI:** PyQt6, offscreen-testable (`QT_QPA_PLATFORM=offscreen`,
  `JARVIS_DISABLE_WEBENGINE=1` in tests). Settings is
  `ui_settings.SettingsWindow`, opened from the HUD as
  `_full_settings_window` (`ui.py:19764`).
- **State writers:** one guarded writer per store (`app/config_guard.py`,
  `config/settings.py`).
- **Degrade-never-crash conventions** documented in `JARVIS_ARCHITECTURE.md`
  and honored in the code paths inspected.

## 3. Discovered problems, root causes, evidence

### P0-A — Native crash (0xC0000005) in the microphone watchdog (CONFIRMED, FIXED)

- **Symptom:** process died ~20-30 s after boot with
  `STATUS_ACCESS_VIOLATION`; crash log holds 26 native dumps across sessions
  (audio/mic threads: `sounddevice _wrap_callback`, `voice/wake.py`,
  `main.py:5262`).
- **Reproduced 2/2:** offscreen boot (45 s window) crashed with exit code
  `-1073741819`; dumps showed `jarvis-backend` inside the watchdog reopen at
  `main.py:5382 stale.stop()` and at `main.py:5375 _open_mic()` of the
  replacement stream.
- **Root cause:** the watchdog kept the stalled stream open while it opened
  and started a second stream for the same device, then stopped/closed the
  stale one; when the device never recovered it repeated the open/stop/close
  cycle forever. Every cycle is a PortAudio stream-lifecycle race window;
  concurrently live streams on one device is the trigger.
- **Fix (`main.py`):** abort+close the stale stream BEFORE opening its
  replacement (one live stream only), give up after 3 consecutive silent
  reopens (voice degrades to text), reset the budget only after ~10 s of
  audio continuity since the last reopen.

### P0-B — UI freeze: activity feed rebuilt every card on every log line (CONFIRMED, FIXED)

- **Symptom:** HUD freezes during log bursts (MCP hot-registration,
  transcripts); crash dumps show the main thread inside
  `ui.py _render → _make_card` while only logging.
- **Evidence:** `_ActivityTimeline.add_entry()` called `_render()` per line;
  `_render()` destroys and recreates up to 60 cards (4 widgets each) per call.
  Benched: the old pattern natively crashed (`0xC0000005`) within ~60 rapid
  rebuilds (reproduced twice; animated cards deleted mid-flight). The new
  path: 300 lines with per-line event processing → exit 0, cap correct,
  ~2.1 ms/line deferred.
- **Fix (`ui.py`):** incremental paint — insert only the new card, trim the
  oldest at the cap; full repaint only on filter change.

### P0-C — Startup `pip install` inside the live process (CONFIRMED, FIXED)

- **Evidence:** JSONL `dependencies.startup_sync`: "installed now: PyQt6
  (import not confirmed), PyQt6-WebEngine (import not confirmed)" on every
  boot; crash dumps show `jarvis-dep-sync → _pip_install` running while the
  app is live.
- **Root cause:** `core/dependency_ops._module_for("PyQt6")` derived the
  lowercase module `pyqt6`; `importlib.util.find_spec` is case-sensitive, so
  both Qt distributions probed missing forever.
- **Fix (`core/dependency_ops.py`):** `_PIP_TO_IMPORT` maps
  `pyqt6 → PyQt6`, `pyqt6-webengine → PyQt6.QtWebEngineWidgets`.
  Verified in JSONL: "already present: 33 / installed now: nothing was
  missing".

### P0-D — Wasted `torch` import for an unavailable decision model (CONFIRMED, FIXED)

- **Evidence:** every boot logs "loading decision model … torch threads=…"
  then "model load failed (ModuleNotFoundError: No module named 'laya')";
  crash dumps show `laya-init` mid-`import torch` (heavy native import)
  competing with UI/audio.
- **Root cause:** `_load_blocking` called `_cap_torch_threads()` (full torch
  import) before `import laya`; the runtime package is not installed, so the
  import was guaranteed waste.
- **Fix (`core/laya_decision.py`):** probe `find_spec("laya")` first; missing
  → the same honest error, no torch import.

### P1-E — `ui_perfomance.py` "does not open" (NOT REPRODUCIBLE in this tree)

- Module imports, `PerformanceTab` instantiates, and the real
  `SettingsWindow` builds the PERFORMANCE tab (integration test + lifecycle
  script: open → scan → cancel → close → reopen, single scan thread, no
  leak, exactly one tab). The reported failure belongs to an older build; no
  code change was needed. (The stale `jarvis_startup_error.log` also predates
  the current `main.py`.)

### P2-F — MCP tools re-registered O(n²) at startup (CONFIRMED, LEFT AS-IS)

- Evidence: boot log registers every tool of every connected server on each
  new server connection (`bridge.publish()` per `manager._connect`), ~4
  duplicate bursts per boot. Idempotent but wasteful + log noise. Left
  unchanged: deduping requires session-rebind semantics review (a reconnect
  must refresh runner closures) — documented in `TODO.md`.

### P3-G — Spotify MCP: RESOLVED BY REMOVAL (2026-10-09 evening)

- The missing `mcp_host/servers/spotify` module made the catalog-declared
  Spotify server unreachable. Per the owner's request the Spotify MCP was
  removed outright: builtin spec + preset (`mcp_host/catalog.py`), saved config
  entry (`data/mcp/servers.json`, backup `.bak-20261009`), quick-setup button
  and name map (`ui_mcp_panel.py`), icon entries (`mcp_host/icons.py`), the 12
  obsolete tests (replaced by a builtin-list test asserting Spotify is NOT
  shipped), and the architecture doc mention. The Spotify *service*
  integrations (media playback, connectors, `control_pc_open_spotify` plugin)
  are not MCP and were kept.

### P3-I — HUD flash / repair-card card loop (2026-10-09 evening)

- `McpManageDialog` wired a button to a missing `_remove` method, so the
  dialog raised during construction; the auto-heal sentry captured it and
  surfaced the repair card. Each subsequent event re-ran
  `_set_panel_visible(..., True)`, which restarted the fade, re-fit geometry
  and called `activateWindow()` — the blinking HUD window seen while speaking
  or typing. Fixed: `_remove` implemented; a visible repair panel is only
  refreshed (raise), never re-shown/activated. Tests:
  `tests/ui/test_mcp_manage_dialog.py`, `tests/ui/test_repair_card_focus.py`.

### P3-J — First typed message dropped before connect (2026-10-09 evening)

- `_on_text_command` silently returned while the Live session was still
  connecting (~13-16 s window with the chat window already usable). Fixed with
  a bounded pre-session queue, a one-time notice, and a flush after the startup
  briefing. Tests: `tests/unit/test_pending_text_queue.py`.

### P3-K — Chat log could stay above the fold (2026-10-09 evening)

- `_RoundedChatLog._scroll_bottom` could read a stale scroll maximum on its
  deferred tick and give up, leaving the newest message unreachable. Fixed
  with a layout flush and a synchronous re-read; one coalesced scroll per
  burst is preserved. Tests: `tests/ui/test_chat_autoscroll.py` (including
  the scroll-up-then-new-message scenario).

### P3-L — Typing-bubble paint raised on every repaint (2026-10-10)

- `ui.py:10833` bound the bubble painter with
  `self._paint_bubble.__get__(self._bubble, QFrame)` — a no-op on an already
  bound method, so `QFrame.paintEvent(row, event)` raised
  `TypeError: first argument ... must have type 'QFrame'` once per visible
  frame (285 recorded tracebacks). Fixed with `types.MethodType(...)` bound
  to the bubble plus a guarded, always-ended painter. Tests:
  `tests/ui/test_typing_bubble_paint.py`.

### P3-M — Protected faults never reached the approval UI (2026-10-10)

- `recovery/analyzer.parse` skipped every protected file (`main.py`, `ui*.py`),
  so a fault like the P3-L one produced "found no first-party file", no
  `repair_proposed` event, and a permanently empty patch preview in the HUD
  repair card ("UI Approval component failing to display"). Fixed: protected
  first-party frames are returned as candidates (`risk="protected"`), the
  patcher synthesises their proposal, and `_auto_allowed()` still refuses all
  automatic application — explicit on-screen approval remains mandatory.
  Tests: `tests/ui/test_repair_approval_display.py`,
  `tests/unit/test_recovery_auto_heal.py` (updated semantics).

### P3-N — Floating panels could not be dragged by their title (2026-10-10)

- The shared header rail only tagged the bar itself; the title/subtitle labels
  consumed the mouse press, so dragging the visible title did nothing. Fixed:
  the labels are `WA_TransparentForMouseEvents`, the press reaches the tagged
  bar and `MainWindow.eventFilter` moves the window (position remembered).
  Tests: `tests/ui/test_panel_drag.py`.

### P3-H — Stale/failing unrelated tests (CONFIRMED, PRE-EXISTING)

- `test_agent_reach_worker_runs_inside_its_environment` needs
  `C:\Users\Administrator\...\python.exe` (original dev machine path).
- `test_documents_architect::test_empty_content_...` reports
  `missing_dependency` (optional document lib not installed in `.venv`).
- Two `test_setup_overlay` tests are stale against the current overlay
  (signal arity / button names).
  None touch code changed in this pass; documented, not "fixed" by weakening
  the tests.

## 4. Repair plan (executed)

1. Crash first: mic-watchdog serialization + budget (P0-A). — done, verified.
2. UI freeze: incremental feed (P0-B). — done, verified.
3. Boot hygiene: Qt module mapping (P0-C), laya probe (P0-D). — done, verified.
4. Performance UI: verify through the real window path; no repair needed
   (P1-E). — done, verified.
5. Tests: regression tests for each fix; suite unblocked from the missing
   Spotify import; all failures triaged. — done.
6. Cleanup: nothing deleted; inventory + classification documented (P5).
7. Docs: this file; `TODO.md` ledger.

## 5. Verification snapshot

- `tests/unit`: **1191 passed, 4 failed (pre-existing), 15 skipped**
  (12 Spotify-module skips + 3 pre-existing) in 2:10.
- New regression tests: `tests/ui/test_activity_feed_incremental.py` (4),
  `tests/unit/test_dependency_ops_qt_mapping.py` (2) — green.
- UI spot checks: `test_performance_tab.py` (4), `test_mic_waveform_idle.py`
  (4), `test_logo_paint_perf.py` (11), `test_ui_control_open_verify.py` (15)
  — all green.
- Runtime: three 45 s offscreen boots after fixes → `EXITCODE=0`, no new
  crash dumps (26 before, 26 after), "Process exiting cleanly (force_exit)".
  Before fixes: 2/2 boots crashed `0xC0000005`.
- PERF UI lifecycle script (offscreen, real `SettingsWindow`): open → scan on
  exactly 1 thread → cancel → buttons restored → close (no leaked thread) →
  reopen (exactly 1 tab) → exit 0.

## 6. Remaining risks

See `TODO.md` §Remaining risks (Spotify module, mic edge cases, MCP re-publish
idempotency, startup time, pre-existing test failures).

---

# 2026-10-17 — Master repair, hardening & upgrade pass (delta)

Re-verified the master prompt's eight named defects against the tree as it stood
on 2026-10-17 (the tree already carried prior passes — this ledger,
`docs/REPAIR_REPORT.md`, `docs/FEATURE_AUDIT.md`, `docs/TEST_RESULTS.md`,
`docs/coding_agent_hardening.md`). Labels: **FIXED+TESTED**, **FIXED (not
runtime-tested)**, **NOT FIXED (reason)**, **ROADMAP**.

Baseline before this pass: `tests/unit` **1366 passed, 5 skipped, 0 failed**
(78 s).

## 7.1 Confirmed defects fixed in this pass

### D3 — Permission gate failed OPEN when a switch could not be read (FIXED+TESTED)
* **Before:** `security/permissions.py`'s `off()` returned `False` on any
  exception, so an unreadable switch **authorised** the action;
  `security/approvals.py` (evaluate + check_permission) and
  `main.py::_permission_gate` swallowed identically; `ui.py`'s proxy read a dead
  window's EMPTY feature map as `True` for every switch.
* **Fix:** `off()` fails **closed** (`_state()` — only a definite "on"
  authorises) with a distinct, non-sensitive refusal; the three swallow sites
  refuse instead of `return None`/`pass`; the UI proxy consults the authoritative
  `config.settings.feature_enabled` when the window is gone or the cache read
  raises. Read-only probes (status/list/engines) still answer.
* **Tests:** `tests/unit/test_permissions_fail_closed.py` (122); permission
  suites re-run (257 passed, 5 skipped).

### D4 — Node floor disagreed with `package.json` (FIXED+TESTED)
* **Before:** `setup_jarvis.py` `NODE_MIN_MAJOR = 18` + major-only parse;
  `app/installer.py` `NODE_MIN_MAJOR = 18`; both contradicted the manifests'
  `engines: {node: ">=22.15.0"}`, and the wizard said "18+".
* **Fix:** one floor `(22, 15, 0)` in both modules; robust
  `_parse_node_version` (junk → `(0,0,0)`, minor/patch aware — the old check
  passed v22.14.9); every message aligned, including `setup_jarvis.py:921` and
  `JARVIS_ARCHITECTURE.md`.
* **Tests:** `tests/unit/test_node_version_gate.py`; `test_feature_probes.py`
  updated for the new floor.

### D5 — Deprecated Gemini Live model (FIXED, live round-trip NOT runtime-tested)
* **Before:** `models/gemini-2.5-flash-native-audio-preview-12-2025` hard-coded
  in `main.py`, `actions/screen_processor.py`, `voice/hybrid_voice.py` and
  `app/ai_providers.py`. Google's deprecations page (fetched 2026-10-17)
  schedules it for shutdown **2026-11-17**; replacement `gemini-3.8-live`.
* **Fix:** `GEMINI_DEFAULT_MODEL = "gemini-3.8-live"`; all four call sites read
  the one constant; `RETIRED_LIVE_MODELS` migrates a stored retired id with a
  REPORTED note; removed `enable_affective_dialog` (API-removed in 3.8); tool
  declarations pinned to `behavior: "BLOCKING"` with a droppable fallback (3.8
  defaults to NON_BLOCKING async, which this client's same-turn tool flow does
  not implement); `gemini-3.8-live-extended-thinking` deliberately not offered.
* **Tests:** `tests/unit/test_gemini_live_model.py` (18).
* **Not verified:** a real mic/speaker Live round-trip needs a key + hardware —
  run the manual smoke checklist before shipping.

### D6 — Global `Popen` monkeypatch (FIXED+TESTED)
* **Before:** `main.py` replaced `subprocess.Popen` on Windows, forced
  `CREATE_NO_WINDOW` and **dropped any `startupinfo`**, only while `main.py` was
  imported.
* **Fix:** `core/process_launch.py` — `hidden_kwargs()` / `visible_kwargs()` /
  `run_hidden()` / `popen_hidden()` plus an idempotent `install_default()`
  backstop that PRESERVES a caller's `startupinfo` and honours a per-call
  `visible=True` opt-out. `main.py` calls `install_default()`;
  `app/installer.py`'s three children migrated to the explicit helper (with
  timeouts on the playwright and pip installs).
* **Tests:** `tests/unit/test_process_launch.py` (8).

### D7 (partial) — Search conflated "no results" with "backend unavailable" (FIXED+TESTED)
* **Before:** `actions/web_search.py` returned "No results found for: …" when
  DDG raised — a fabricated-looking success hid a failure — and answers carried
  no retrieval timestamp.
* **Fix:** `_ddg_search`/`_ddg_news` return `(results, error)`; the formatters
  name a backend failure distinctly, keep source URLs, and stamp
  `Retrieved: <UTC>`; `_news`/`_compare` distinguish empty from unreachable.
* **Tests:** `tests/unit/test_web_search_honesty.py` (15).

### D8 — Redaction missed future provider keys (FIXED+TESTED)
* **Evidence:** redaction matched only the explicit `SECRET_FIELD_NAMES` set, so
  a new `*_api_key` (e.g. `openai_api_key`) would not be redacted. No committed
  secrets were found (§7.3).
* **Fix:** `_is_secret_key()` = exact set **plus** a credential **suffix** rule
  (`*_api_key`, `*_access_token`, `*_client_secret`, `*password`, `*_secret`,
  `*_token`) — suffix-based so `max_tokens` is NOT over-redacted.
* **Tests:** `tests/unit/test_secret_redaction.py`.

### New — `Closed_Jarvis.py` force-kill always failed on Windows (FIXED+TESTED)
* **Evidence:** `_NO_WINDOW = {"creation_flags": …}` — not a `subprocess.Popen`
  keyword, so the `taskkill` call raised `TypeError`, swallowed by the caller's
  `except Exception`. The recovery tool the app tells the user to run could never
  terminate an orphan.
* **Fix:** corrected to `creationflags`.
* **Tests:** `tests/unit/test_full_closure.py::test_closed_jarvis_launch_flags_are_real_subprocess_kwargs`.

## 7.2 Verified already-fixed (no change) + dev-agent completion

* D1 (dev-agent false build success) and D2 path validation were fixed by prior/
  parallel passes (`_RunOutcome`/`_has_error` exit-code-first,
  `_safe_project_path` wired into `_write_file`/`_fix_files`, venv isolation).
* Log rotation already exists (`observability/logger.py`).
* **Completed in this pass (FIXED+TESTED):** run-command **allow-list**
  (`_RUN_HEADS`; argv via non-POSIX `shlex` with quote stripping — no shell, no
  `str.split()` mangling); **plan schema validation** before any side effect
  (file count ≤ 40, every path passes `_safe_project_path`, resolved-path
  de-dup, entry point in list, language allow-list, dependency guard,
  run-command head check); **folder-collision policy** (never write into an
  existing non-empty project folder — a fresh `<name>-N` sibling is used);
  **per-file size cap** in both write paths.
  **Tests:** `tests/unit/test_dev_agent.py` (62).

## 7.3 Secret scan (report-only, no values echoed)

Scanned all **624 tracked files** and the single commit's full patch for Google/
OpenAI/GitHub/Slack/AWS/Composio key shapes, Bearer tokens and PEM blocks:
**no credential patterns found.** `.env`, `config/api_keys.json` and
`config/voice_config.json` exist locally; the first two are gitignored.
`config/voice_config.json` is intentionally tracked and holds **TTS settings
only** (the hybrid voice key persists to the gitignored `config/api_keys.json`).
No git history rewrite was performed.

## 7.4 Verification snapshot (this pass)

* `tests/unit`: **1614 passed, 5 skipped, 0 failed** (146 s) — baseline was 1366.
  (An earlier interim run showed 1581 + one failure that was a self-inflicted
  test-isolation artifact in `test_process_launch.py`; fixed. Every touched file
  together re-ran **277 passed**.)
* `tests/hybrid_voice`: **153 passed, 2 skipped, 0 failed** (run with a fresh
  `--basetemp`; the first attempt hit 38 setup ERRORS from a locked leftover
  `.pytest_tmp` SQLite file held by another process — environmental, not a
  regression. See `docs/TEST_RESULTS.md`.)
* New test files: `test_permissions_fail_closed.py`, `test_node_version_gate.py`,
  `test_process_launch.py`, `test_secret_redaction.py`,
  `test_web_search_honesty.py`.
* Static: `compileall` on every edited module → clean; `pip check` → no broken
  requirements.

## 7.5 Remaining risks / NOT FIXED

* **Live voice round-trip against `gemini-3.8-live` is user-run only** (key +
  mic + speaker). The model id and the BLOCKING/affective-dialog deltas were
  verified against the SDK (`google-genai` 2.31.0 exposes
  `FunctionDeclaration.behavior`, `LiveConnectConfig.proactivity`) and the
  official deprecations page, but no live handshake ran here. *(Risk: medium;
  next action: run the smoke checklist.)*
* **Live-session `api_version`** still falls back through the existing
  enhanced-audio ladder; a stable-model/v1alpha mismatch is now also caught by
  the fallback (added `"not supported for"` / `"behavior"` triggers), but the
  happy path was not live-tested. *(Risk: low.)*
* **Subprocess call sites not migrated to the explicit helper**
  (`actions/file_processor.py`, `evolution/crucible.py`, `voice/tts.py`, …) are
  covered at runtime by `process_launch.install_default()`, so behaviour is
  unchanged; migrating them is cosmetic follow-up. *(ROADMAP.)*
* **Parallel workspace edits:** another session changed the tree during this pass
  (it landed dev-agent path validation). Every edit re-read its target first; no
  user change was reverted.
* Python floor messaging ("3.13+") vs `MIN_PYTHON = (3, 11)` — reconcile.
  *(ROADMAP.)*

## 7.6 Docs & CI (this pass)

* `readme.md` (2-line stub) → canonical **`README.md`** (case rename; a single
  root README, no duplicate).
* `JARVIS_ARCHITECTURE.md` updated: Node floor, Live model source + retired-id
  migration + BLOCKING pin, fail-closed permissions, `core/process_launch`.
* **`.github/workflows/ci.yml` added** — Linux: byte-compile + full-suite
  collect + Node-gate test; Windows: `tests/unit` + `tests/hybrid_voice`. No
  credentials required. **Not yet run remotely** (no badge).
