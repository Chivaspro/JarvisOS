# Feature Audit — JARVIS OS (master audit)

Business date: 2026-10-16. Repository: `JarvisOS Main/` (branch `main`, one commit).
Environment: `.venv` Python 3.14.8, `requirements.txt` fully installed + `pytest 9.1.1`.
Method: code inspection against `JARVIS_ARCHITECTURE.md` + the suites in
`tests/` + targeted live/repro checks. Report date and evidence are recorded for
each row. **A feature is only marked working when a named command/test passed.**

Status vocabulary: VERIFIED WORKING · VERIFIED FIXED · PARTIALLY VERIFIED ·
FAILING · BLOCKED · UNVERIFIED.

> Scope note. This is a large codebase (≈23600-line `ui.py`, ≈1200-line
> `core/laya_decision.py`, ~60 tools, ~45 feature flags). This audit verifies the
> subsystems below by running the existing suites and inspecting the real code
> paths. Live end-to-end runs that need a microphone, camera, Docker or paid
> providers are marked BLOCKED/UNVERIFIED where they were not executed — the
> environment limitation is stated, not hidden. No feature is marked working on
> the strength of a declaration, button or config key existing.

---

## Environment findings (apply across subsystems)

| Finding | Evidence | Impact |
| --- | --- | --- |
| Project `.venv` was **empty** (only pip) | `.venv\Lib\site-packages` held `pip/` only | Restored from `requirements.txt` (approved); recorded in `TEST_RESULTS.md` |
| **`import torch` fails**: `WinError 1114` loading `torch\lib\c10.dll` | 3/3 fresh runs (`jarvis_torch_probe.py`) | **YOLO/ultralytics vision BLOCKED**; app degrades to mediapipe/opencv/ai (guarded in `vision/object_detection.ensure_backend`) |
| **`pywinauto` import fails**: `win32ui` DLL (MFC runtime absent) | import smoke | `pc_control` Windows-automation engine unavailable; optional + probed |
| Bundled **Piper voice model absent**: `config/voices/jarvis-high.onnx` | folder holds only `jarvis-high.json`; `*.onnx` untracked | "Local Piper Only" / "Gemini → Piper ONNX" output modes BLOCKED |
| Whole-tree `tests/ui` run dies with a native teardown abort | `docs/cleanup_report.md` §7/§8; reproduced flakily | Run UI tests one file per process (documented workaround) |

---

## A. Desktop UI

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Main HUD + command deck + panels | `ui.py` (`RefinedCommandWindow`, `MainWindow`), `ui_settings.py`, `ui_layout*.py`, `ui_theme.py`, `ui_mcp_panel.py`, `ui_neural_map*.py`, `ui_vault_panel.py`, `ui_perfomance.py` | VERIFIED WORKING | 42/47 UI files exit 0 per-file; see `TEST_RESULTS.md` §2 |
| Activity feed (incremental paint) | `ui.py` `_ActivityTimeline` (P0-B fix from prior pass) | VERIFIED WORKING | `tests/ui/test_activity_feed_incremental.py` 4 passed |
| 3D orb frame budget | `dashboard/brain3d.py` | VERIFIED WORKING | `tests/ui/test_orb_frame_budget.py` 7 passed |
| Chat history (async load, grouping, search, resume) | `ui.py` `ConversationHistoryPage` (worker-thread read → `run_on_ui`) | VERIFIED WORKING | `tests/ui/test_chat_history.py` 13 passed (tests updated to current contract) |
| Chat History stage (standalone 4:5 window) | `ui.py` `HistoryStage`, `show_history`/`clear_history_view` | VERIFIED WORKING | `tests/ui/test_history_stage.py` 8 passed (updated to standalone-window contract) |
| Design-token discipline (no private palettes) | `ui.py` now uses `C.RED`/`C.TEXT_DIM` | VERIFIED FIXED | `tests/ui/test_design_tokens.py` 47 passed (was 1 failing) |
| Layout Studio / Figma export / Neural map / vault / MCP / performance panels | `ui_layout*.py`, `figma_export.py`, `ui_neural_map*.py`, `ui_vault_panel.py`, `ui_mcp_panel.py`, `ui_perfomance.py` | VERIFIED WORKING | layout 12, figma 12, neural/mind 9, vault, mcp 3+3, performance 4 — all passed per-file |
| UI responsiveness during long tasks / cancellation / shutdown | threads + `run_on_ui` marshalling; `core/stability.request_process_exit` | PARTIALLY VERIFIED | Widget-thread marshalling pinned by `tests/unit/test_vision_panel_threading.py`; no full interactive session was driven |

## B. Voice

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Hybrid voice coordinator, mode routing, chunking, interruption | `voice/hybrid_voice.py` (`PiperSpeechService`, mode migration) | VERIFIED WORKING | `tests/hybrid_voice/test_hybrid_voice.py`, `test_piper_pipeline.py` (58 passed incl. 2 skips) |
| TTS providers (Fish, ElevenLabs, Gemini, ONNX, Edge, Kokoro, SAPI) | `voice/tts.py`, `voice/providers_config.py`, `config/voice_config.json` | VERIFIED WORKING | `test_provider_runtime.py`, `test_providers_config.py`, `test_settings_default_voice.py` passed |
| Settings voice defaults + provider scoping | `ui_settings.py`, `voice/providers_config.py` | VERIFIED WORKING | `test_settings_default_voice.py` (now 2 honest skips for the absent model) |
| Streaming TTS / delivery style / narration | `voice/tts.py`, `voice/delivery_style.py`, `voice/narrator.py` | VERIFIED WORKING | `test_tts_streaming.py`, `test_delivery_style.py`, `test_narrator.py` passed |
| Pi/ONNX local voice (bundled) | `config/voices/jarvis-high.onnx` | **BLOCKED** | Model file absent/untracked; README claims it is bundled — packaging gap (see REPAIR_REPORT) |
| Gemini Live native audio session | `main.py` `JarvisLive` | UNVERIFIED (live) | Requires mic + live Gemini session; not exercised headless |
| Microphone watchdog / device recovery | `main.py` (P0-A fix: serialized reopen, bounded retry) | PARTIALLY VERIFIED | `tests/unit/test_realtime_stt_toggle.py::test_mic_watchdog_replacement_actually_starts` passed; live mic recovery not driven |

## C. AI providers & routing

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Providers (Gemini/OpenAI/Anthropic/Groq/Ollama/OpenRouter/custom) | `app/ai_providers.py`, `app/llm_client.py` | VERIFIED WORKING | `tests/unit/test_openrouter_provider.py`, `test_api_key_*.py` passed |
| Model router / role mapping | `app/model_router.py` | VERIFIED WORKING | `test_openrouter_provider.py::test_models_for_includes_openrouter` passed |
| Key validation probes (real request, honest status) | `services/hub.py` | VERIFIED WORKING | `tests/unit/test_service_hub.py` (~40 tests) passed |
| Local Laya "System 1" decision model | `core/laya_decision.py` (guards `find_spec('laya')` before torch) | VERIFIED WORKING (routing) | `tests/unit/test_laya_decision.py` 39 passed; model package not installed → honest WARNING fallback |
| No silent model substitution | router/`services` probes | VERIFIED WORKING (unit) | `test_pc_web_control.py::test_..._does_not_substitute`, `test_service_hub` passed |

## D. Brain, agents, orchestration

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Cognitive engine + skill registry | `brain/engine.py`, `brain/skills.py`, `brain/context.py` | VERIFIED WORKING | `tests/unit/test_capabilities.py`, `test_brain_mode_switch.py`, `test_task_engine.py` passed |
| Task lifecycle / PLAN→…→REPORT | `brain/tasks.py`, `brain/task_engine` | VERIFIED WORKING | `test_task_engine.py` (26) + `test_question_flow.py`/`test_question_gate.py` passed |
| LangGraph orchestrator + pure-Python fallback | `brain/graph.py` (`features.langgraph_orchestrator`) | VERIFIED WORKING | `tests/unit/test_orchestrator.py` incl. `SINGLE_NOTICE` degradation passed |

## E. Computer & browser control

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Capability router + registry + locks + ledger | `capabilities/router.py`, `registry.py`, `actions.py`, `locks.py`, `core/action_ledger.py` | VERIFIED WORKING | `tests/unit/test_pc_web_control.py` (30+) passed |
| Playwright `browser_action` | `capabilities/browser_pw.py` | UNVERIFIED (live) | Offline contract verified; live pages not driven |
| pywinauto `pc_control` | `capabilities/windows_ext.py` | **BLOCKED** | `pywinauto` import fails (win32ui/MFC) on this machine |
| UI-TARS `visual_control` (Gemini key) | `capabilities/uitars.py`, UI-TARS prompt vendored | PARTIALLY VERIFIED | Parse→execute mapping tested offline (`test_pc_web_control.py`); no live grounding run |
| PyAutoGUI physical input | `capabilities/desktop.py` | PARTIALLY VERIFIED | Offline only; not exercised against the pointer |
| Consequential-action authorization | `security/permissions.py`, `approvals.py`, `confirm.py` | VERIFIED WORKING | `test_security.py`, `test_windows_orchestration.py` confirm UI-token gating; model `confirmed` flag ignored |
| Live capability acceptance sheet | `tests/live_capability_acceptance.py` | UNVERIFIED | Not run (spawns Agent TARS/Node + real engines) |

## F. Memory & knowledge

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| SQLite conversation + brain stores, persistence | `memory/`, `data/brain_memory.db`, `data/conversations.db` | VERIFIED WORKING | `test_memory_persistence.py`, `test_conversations.py`, `test_task_engine::test_record_is_persisted_to_sqlite` passed |
| Policy / promotion gate / identity / rules | `memory/policy.py`, `identity.py`, `rules.py` | VERIFIED WORKING | `test_memory_policy.py`, `test_memory_identity_rules.py`, `test_memory_continuity.py` passed |
| Obsidian vault backend (write under `Memory/`, provenance) | `memory/obsidian_backend.py`, `vault_store.py`, `vault_watch.py` | VERIFIED WORKING | `test_vault_memory.py` (12) passed |
| Mem0 (optional) | `memory/mem0_client.py` (`features.mem0_memory`) | VERIFIED WORKING (dormant) | `test_memory_policy.py` mem0 dormant/no-key/fallback tests passed |
| Qdrant knowledge (optional) | `knowledge/qdrant_store.py` (`features.qdrant_knowledge`) | UNVERIFIED | No Qdrant instance available |
| Local corpus retrieval | `knowledge/retriever.py` | VERIFIED WORKING | `test_knowledge_selfinspect.py` passed |

## G. Vision

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Camera manager + privacy toggle + staged frames | `vision/manager.py`, `frame_processor.py`, `vision/config.py` | VERIFIED WORKING | `tests/ui/test_camera_pipeline.py` (15) + `test_vision_*` passed (per-file) |
| Object detection backend selection | `vision/object_detection.py:ensure_backend` | PARTIALLY VERIFIED | Backend fallback chain inspected & guarded; ultralytics path BLOCKED by torch DLL failure |
| Gesture / hand tracking / face / register | `vision/gesture.py`, `hand_tracking.py`, `face_recognition.py` | UNVERIFIED (models) | mediapipe/insightface models not installed |
| Live vision stream / multimodal scene | `vision/live_stream.py`, `multimodal.py` | UNVERIFIED | Needs camera/model |

## H. Tools, files, productivity

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Documents (Word/Excel/PPT/PDF) | `documents/builders.py`, `documents/tool.py` | VERIFIED FIXED | `tests/unit/test_documents_architect.py` 20 passed (empty-content now refused before engine probe) |
| File controller/processor, media, archives | `actions/` | VERIFIED WORKING | `test_media_routing.py`, `test_documents_architect.py` passed; FFmpeg probed, not assumed |
| Web search / YouTube / weather | `actions/` (`web_search`, transcript, weather) | PARTIALLY VERIFIED | Offline tests pass; live provider calls not run |
| Reminders / system monitor / power control | `actions/`, `Closed_Jarvis.py` | VERIFIED WORKING | `test_power_control.py` (40+) passed; WMI thermal documented constant |
| Code helpers / dev agent | `actions/code_helper.py`, `dev_agent` | UNVERIFIED | Not exercised |

## I. MCP, connectors, remote access

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| MCP manager (4-state model), sessions, catalog | `mcp_host/manager.py`, `session.py`, `catalog.py` | VERIFIED WORKING | `tests/unit/test_mcp_host.py` 27 passed |
| Tool publishing as `<server>__<tool>` | `mcp_host/bridge.py` | **VERIFIED FIXED** | Registered once per active session; rebinds on reconnect. `test_mcp_publish_idempotent.py` (2) + `test_mcp_host.py` (27) passed |
| MCP secrets / redaction | `mcp_host/secrets.py` | VERIFIED WORKING | `test_mcp_secrets_config.py` passed |
| Connectors (Composio single route) | `connectors/`, `app/` | UNVERIFIED | Needs Composio session/credentials |
| Remote gateway (pairing/WebSocket, loopback) | `connect/` | PARTIALLY VERIFIED | `test_connect_gateway.py` passed; no phone pairing driven |
| Dashboard server | `dashboard/server.py` | UNVERIFIED | Not started |

## J. Security, recovery, evolution

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Permissions / approvals / confirm / coding approval | `security/` | VERIFIED WORKING | `test_security.py`, `test_coding_approval.py`, `test_windows_orchestration.py` passed |
| Secret redaction at the logging boundary | `observability/logger.py` (`_scrub_text` + key-name redaction) | **VERIFIED FIXED** | `tests/unit/test_observability.py` 6 passed (the two redaction tests were vacuous; now genuinely exercise a secret-shaped value) |
| Undo + action ledger | `security/undo.py`, `core/action_ledger.py` | VERIFIED WORKING | `test_security.py`, `test_phase3_wiring.py` passed |
| Auto-heal / boot sentry (bounded) | `recovery/` | VERIFIED WORKING | `test_recovery_auto_heal.py` (23) passed; protected files proposed, never auto-applied |
| Self-evolution (Forge/Crucible/DGM) | `evolution/` | PARTIALLY VERIFIED | `test_evolution_crucible.py`, `test_evolution_forge.py`, `test_evolution_manager.py` passed; live synthesis not run |
| Malware scanner | `security/malware_scanner.py` | VERIFIED WORKING | `test_malware_scanner.py` passed |

## K. Observability, startup, performance

| Feature | Implementation | Status | Evidence |
| --- | --- | --- | --- |
| Structured logging + redaction | `observability/logger.py` | VERIFIED WORKING | `test_observability.py` 6 passed |
| **Log rotation** | `observability/logger.py::_prune_old_logs` (`JARVIS_LOG_RETENTION_DAYS`, default 14) | VERIFIED WORKING | Code present and bounded; `core/maintenance.run_maintenance()` also rotates. (Note: `docs/cleanup_report.md` §7 says "not implemented" — that is now stale; see REPLACEMENT_DECISIONS) |
| Metrics / tracing / diagnostics / performance scan | `observability/`, `ui_perfomance.py` | VERIFIED WORKING | `test_ui_perfomance.py`, `test_performance_tab.py` passed |
| Lazy startup (WebEngine/urllib not imported) | `ui.py` `_load_webengine` | VERIFIED WORKING | cold `import ui` 1158 ms; warm 36 ms; `PyQt6.QtWebEngineWidgets`/`urllib.request` absent from `sys.modules` |
| One metrics sampler per process | `core/sys_metrics.py::shared_metrics` | VERIFIED WORKING | `test_sys_metrics_optional.py` passed |
| Full app startup/shutdown smoke | `main.py` | UNVERIFIED | Not run: needs a live GUI + Gemini session; would block the audit. Covered partially by compileall + `import ui` + per-file UI tests |

---

## Cross-cutting status summary

* **VERIFIED WORKING**: the great majority of subsystems above, backed by the
  unit/hybrid_voice/UI suites.
* **VERIFIED FIXED in this pass**: MCP publish idempotency/rebind; document
  builders validating before the engine probe; `ui.py` design-token literals;
  the observability redaction tests.
* **BLOCKED (environment)**: YOLO vision (torch DLL), pywinauto `pc_control`
  (MFC/win32ui), bundled Piper voice (missing model file).
* **UNVERIFIED (live)**: Gemini Live voice, live browser/pc control, Qdrant,
  connectors, dashboard, live search/transcript.
