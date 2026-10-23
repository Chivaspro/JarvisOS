# Replacement & Consolidation Decisions — JARVIS OS (master audit)

Business date: 2026-10-16. For each candidate the decision is one of
**KEEP · IMPROVE · CONSOLIDATE · REPLACE · OPTIONAL · DEFER**, with the evidence
that justifies it. The default is to keep working implementations; a replacement
is only proposed with the 7-point justification (current implementation,
verified limitation, proposed alternative, expected measurable benefit,
compatibility/migration risk, comparison test, rollback plan) and is not
implemented without those tests.

## 1. Architecture guardrails honoured

* **No new routing dependency.** `app/ai_providers.py` + `app/model_router.py` +
  `services/hub.py` already select providers, roles and honest probes, and are
  covered by tests. **KEEP** — LiteLLM or a second router would add a transitive
  dependency tree and a second place where "which engine ran" can drift.
* **No second orchestrator.** `brain/graph.py` already provides LangGraph-with-
  Python-fallback behind `features.langgraph_orchestrator`, and
  `brain/tasks.py` owns the task lifecycle. **KEEP**.
* **Keep the Gemini Live voice stack.** It is the native real-time path; no
  comparative test shows a defect that warrants replacing it. **KEEP**.
* **Keep SQLite + local retrieval as the baseline.** Mem0 and Qdrant remain
  optional backends (flags default off). **KEEP / OPTIONAL**.
* **Requirements unchanged.** No dependency was added, removed or upgraded in
  this pass (the venv was restored to satisfy the existing `requirements.txt`; a
  dev-only `pytest` was added to the venv, not to `requirements.txt`).

## 2. Candidates examined

### 2.1 MCP tool publishing (`mcp_host/bridge.py`) — **IMPROVE (done)**
* Current: `publish()` re-registered every live session's tools on each connect.
* Verified limitation: O(n²) registrations per boot + repeated "hot-registered"
  log lines (repo's own audit item *P2-F*; reproduced by reading the real
  connect→publish path).
* Decision: **improved in place** (session-aware idempotency + reconnect rebind)
  rather than replacing the registry or introducing a new one. Regression test
  added. Deferred sub-item: unregistering a *stopped* server's tools (would need
  a `PluginRegistry.unregister` API — **DEFER**, low severity, honest failure
  sentence today).

### 2.2 Document builders (`documents/builders.py`) — **IMPROVE (done)**
Validate-before-probe ordering; no library change. Document libs remain optional
and probed. **KEEP** python-docx / openpyxl / python-pptx / pypdf / reportlab
(the code already degrades to a minimal PDF writer without reportlab).

### 2.3 Vision backends — **KEEP (ultralytics BLOCKED by environment)**
* Current: `ensure_backend` chain ultralytics → mediapipe → opencv → ai, each
  guarded, each reporting which engine ran.
* Verified limitation: `import torch` fails on this machine (WinError 1114,
  c10.dll). This is an **environment** failure, not a defect in the backend
  selection. The chain already falls through safely.
* Decision: **KEEP / BLOCKED**. Replacing ultralytics is not justified by an
  environment-specific DLL failure. Revisit only if the owner wants a torch-free
  detector (e.g. ONNX Runtime) — a REPLACE proposal would need accuracy/latency
  comparisons and a licensing check first.

### 2.4 Windows automation (`capabilities/windows_ext.py`, pywinauto) — **KEEP / BLOCKED**
`pywinauto` cannot import here (`win32ui`/MFC). The controller is probed and the
router names a missing engine rather than substituting. **KEEP**; the fix is an
environment one (install the MSVC/MFC runtime), not a code change.

### 2.5 Log rotation — **KEEP (documentation correction)**
* Current: `observability/logger.py::_prune_old_logs` prunes dated
  `jarvis-*.jsonl` older than `JARVIS_LOG_RETENTION_DAYS` (default 14) on
  first open; `core/maintenance.run_maintenance()` also rotates (14-day + size
  cap).
* Verified limitation: **none** — rotation exists. `docs/cleanup_report.md` §7
  ("log rotation is not implemented") and `JARVIS_ARCHITECTURE.md` §14/§18
  disagree; the architecture doc matches the code.
* Decision: **KEEP**; the stale note in `cleanup_report.md` is superseded by
  this audit (not edited, to avoid rewriting an historical report).

### 2.6 `core/*` one-release alias shims — **KEEP (DEFER removal)**
The `modernize-jarvis-architecture` change left 19 transparent aliases so old
import paths keep working ("move, don't break"). Removing them is a breaking
change for any external caller and is not justified by a defect. **DEFER** until
the project confirms no caller relies on `core.<module>`.

### 2.7 Bare `except Exception: pass` (~960 in project code) — **KEEP**
Sampled in `docs/cleanup_report.md` §7: nearly all are deliberate tolerance on
audio/vision/teardown paths; a wholesale rewrite is churn with real regression
risk. **KEEP**; only the one that hid a dead feature (wake detector) was changed
in the prior pass.

### 2.8 `documents` / `pytest` / dev tooling — **KEEP**
No change to `requirements.txt`, `package.json` or lockfiles.

### 2.9 Memory-growth telemetry (`memory/growth.py`) — **IMPROVE (done)**
Tests were accumulating growth events (incl. the test-only `"fake"` backend) in
the user's real `data/memory/growth.json`. Added an explicit, production-safe
`set_growth_dir()` sandbox hook that `tests/conftest.py` points at a temp dir;
the real file is now untouched by the suite. **KEEP** the growth feature itself
(it is a real, deterministic counter); only test isolation changed.

## 3. Dependency evaluation (summary)

| Candidate action | Decision | Why |
| --- | --- | --- |
| Add LiteLLM / a router library | **DEFER / reject** | Existing router passes tests; no demonstrated need |
| Add a second agent orchestrator | **reject** | LangGraph + Python fallback already exist |
| Replace Gemini Live voice | **reject** | No test shows a fixable-by-replacement defect |
| Replace SQLite/local retrieval | **reject** | Reliable baseline; optional backends already present |
| Add/replace vision detector | **DEFER** | Environment (torch DLL), not a code defect |
| Upgrade/flatten dependencies | **reject** | No verified need; avoid lockfile churn |

## 4. Rollback

All code changes in this pass are small and localised (`mcp_host/bridge.py`,
`documents/builders.py`, `ui.py`) plus test/doc updates. Reverting any file to
the committed revision restores prior behaviour; no data migration, schema
change or config-format change was made.
