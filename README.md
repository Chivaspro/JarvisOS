# J.A.R.V.I.S. OS

A local-first, voice-driven desktop assistant for Windows: a PyQt6 HUD over a
Python backend that runs a real-time Gemini Live voice session, executes
permissioned tools (files, screen, browser, PC automation, coding agents), and
keeps its memory on your machine.

> **Status: active development.** The desktop application is **Windows-first**
> and that is the only platform verified here. macOS/Linux are *not* supported
> by the desktop app and are **unverified** — a few helpers (e.g. the dashboard
> server) contain Linux-oriented code paths, but the UI, audio and automation
> layers target Windows. Treat every non-Windows claim as untested.

> **Before you run anything:** some features send data to external services.
> See [Security, privacy and data flow](#security-privacy-and-data-flow).

---

## What it does

Capabilities that exist in the source today (not aspirational):

| Area | What exists | Where |
| --- | --- | --- |
| Voice | Real-time Gemini Live audio session, wake word, barge-in/interrupt, hybrid TTS | `main.py`, `voice/` |
| Vision | Screen understanding, camera capture, gesture/object detection | `actions/screen_processor.py`, `capabilities/` |
| Files | Read/search/organise files, documents (Excel/PDF/Word) | `actions/`, `documents/` |
| Web research | Grounded Gemini search with a DuckDuckGo fallback and honest failure reporting | `actions/web_search.py` |
| Browser / PC control | Playwright browser actions and Windows UI automation (opt-in) | `capabilities/`, `coding/` |
| Reverse engineering | Capture a site you own or are authorized to examine (DOM, styles, screenshot, HAR), list the API surface its traffic revealed, synthesise a clean verified remake, or copy an app folder's sources into a readable tree — always behind the user's explicit authorization and an audit log | `capabilities/reverse_engineer.py` |
| Coding agents | Generated projects built in an isolated folder + venv; code helpers | `actions/dev_agent.py`, `actions/code_helper.py` |
| Automation engines | Agent TARS (Node) and MCP servers over stdio | `capabilities/`, `mcp_host/` |
| Connectors | One route to external apps via Composio sessions | `connectors/` |
| Knowledge library | Local document library: MarkItDown or stdlib conversion + SQLite FTS5 search with title/heading citations; source files are never modified | `knowledge/library.py` |
| Meeting notes | Local system-audio + microphone capture, local faster-whisper transcript, Markdown notes and summaries | `capabilities/meeting_notes.py` |
| Home Assistant | Native control of your own instance — lights, switches, fans, booleans, scenes and confirmation-gated scripts | `capabilities/home_assistant.py` |
| GitHub (read-only) | Notifications, issues, PRs, checks and search over GET-only REST; every GitHub write stays with Composio | `capabilities/github.py` |
| Memory | Local memory + an optional Obsidian vault | `memory/`, `Jarvis Obsidian Vault/` |
| Tasks | Task engine with approval questions and a progress panel | `brain/task_engine.py`, `ui_task_panel.py` |
| Diagnostics | Health/feature probes and a redacted diagnostic export | `observability/`, `capabilities/health.py` |

Everything destructive or account-affecting is gated behind a permission switch
**and** a confirmation prompt.

Optional extras stay optional: `markitdown[pdf,docx,xlsx,pptx]` for PDF/Office
conversion in the library, `PyAudioWPatch` (system audio) and `faster-whisper`
(local transcription) for meeting notes. They are offered from Settings ▸
Dependencies; when one is missing the feature names the package and the exact
`pip install` command instead of failing silently.

---

## Project structure

```
JarvisOS Main/
├── main.py                  # backend: Live voice session, tool dispatch, permissions
├── ui.py                    # PyQt6 main window / HUD (backend-agnostic proxy)
├── ui_settings.py           # Settings window (AI, Permissions, Engines …)
├── ui_task_panel.py         # Task progress panel
├── start_jarvis.py          # launcher (venv bootstrap + start)
├── setup_jarvis.py          # setup/repair wizard (STANDALONE, stdlib only)
├── Closed_Jarvis.py         # recovery: clear an orphaned background instance
├── app/                     # service layer (installer, providers, config guard)
├── actions/                 # tool handlers (web search, dev agent, documents …)
├── brain/                   # personality, skills, task engine, orchestration
├── capabilities/            # OS/desktop capabilities + health probes
├── coding/                  # coding agent adapters (Aider, OpenHands, router)
├── config/                  # settings.py, api_keys.json (private), voices/
├── connect/                 # pairing gateway
├── connectors/              # Composio-based external app connectors
├── core/                    # shared infrastructure (process_launch, model_router …)
├── dashboard/               # local status server
├── data/                    # runtime data: logs, memory, reports (local)
├── docs/                    # audit/repair ledgers and subsystem docs
├── documents/               # document builders/readers
├── evolution/               # self-improvement ("Crucible")
├── mcp_host/                # MCP server host (stdio/HTTP), REA server
├── memory/                  # memory manager and growth
├── observability/           # structured logging, diagnostics, redaction
├── security/                # permissions, coding approval, malware scanner
├── services/                # provider hub (health probes, keys)
├── tests/                   # unit, hybrid_voice, ui suites
├── voice/                   # TTS/STT engines and delivery style
└── requirements.txt         # Python dependencies
```

---

## Prerequisites

* **Windows 10/11** (the desktop app's only supported target).
* **Python 3.13+** recommended — the setup wizard installs 3.13 and says so;
  the code floor is 3.11 (`MIN_PYTHON` in `setup_jarvis.py`). Newer/older
  interpreters are **unverified**.
* **Node.js 22.15.0 or newer** — required only for Agent TARS / MCP servers.
  This floor is enforced identically by `package.json`, the setup wizard and
  the installer probe (`tests/unit/test_node_version_gate.py` pins that).
* **Git** and (for browser automation) **Playwright Chromium** — the wizard
  installs it.
* A **Google Gemini API key** for voice/vision. Optional extra keys are listed
  in `.env.example`.

`ffmpeg` and a microphone/speaker are needed for audio features; PyAudio/
`sounddevice` devices are probed at runtime.

---

## Setup (Windows-first)

```powershell
# 1. From the project folder, run the wizard with the interpreter you intend to use
python setup_jarvis.py
```

The wizard creates `.venv`, installs Python dependencies, optionally installs
Node + Playwright, writes keys, and can launch the app. Useful non-interactive
flags (verified against `python setup_jarvis.py --help`):

```powershell
python setup_jarvis.py --check            # report only, change nothing
python setup_jarvis.py --install          # Python dependencies
python setup_jarvis.py --node             # Node dependencies (Agent TARS)
python setup_jarvis.py --browser          # Playwright chromium
python setup_jarvis.py --keys --yes       # write keys from .env, no prompts
python setup_jarvis.py --test             # test stored keys live
python setup_jarvis.py --diagnose         # report providers and engines
python setup_jarvis.py --auto-heal        # diagnose & repair every known failure
python setup_jarvis.py --install-missing  # install everything still absent
python setup_jarvis.py --all --yes        # everything, no prompts
python setup_jarvis.py --launch           # start J.A.R.V.I.S.
```

Rerunning the wizard is safe: it reuses a good venv and never overwrites your
`config/api_keys.json` with blanks.

### Launch

```powershell
python start_jarvis.py        # rebuilds/uses the venv, then starts the app
```

If a background instance is left behind and the app cannot start, run the
recovery tool:

```powershell
python Closed_Jarvis.py --status   # report only
python Closed_Jarvis.py            # terminate an orphaned instance
```

---

## API keys and configuration

Keys live in **local, private** files that are **never committed**:

* `.env` — copy from `.env.example` and fill in (placeholders only in the repo).
* `config/api_keys.json` — written by the Settings UI / wizard.

`.gitignore` covers `.env`, `config/api_keys.json` and the other runtime state.
Never paste a real key into an issue, a log, or a test.

Selection order: environment variable → `config/api_keys.json`. Diagnostics and
logs redact every credential-shaped field (`config.settings.redact_secrets`).

Optional first-party links read a few more fields from the same file:
`github_token` (a fine-grained, read-only token) and `github_api_url`;
`home_assistant_url`, `home_assistant_token` and `home_assistant_verify_tls`.
The library and meeting notes need no account at all — the local transcription
model is chosen with `meeting_notes_model` (`base` by default). Every one of
these appears in Settings ▸ API with its GET API KEY page and TEST button.

---

## Feature switches

Permissions are enforced in `security/permissions.py`. A switch that cannot be
read is treated as **revoked** (fail closed) — a broken lookup can never
authorise a desktop, terminal, file, camera or account action.

* Toggle them in **Settings ▸ Permissions**; defaults live in `FEATURE_DEFAULTS`
  in `config/settings.py`.
* The four first-party additions each have their own switch: **Knowledge
  library**, **Home Assistant** and **GitHub (read-only)** default **on** (each
  is inert until pointed at a file/folder, a URL/token or a token), and
  **Meeting notes** defaults **off** — every start confirms on the HUD, and
  `stop`/`status` keep answering even if the switch is turned off mid-meeting.
  Library `ingest` additionally requires **File control**, because it reads
  files.
* High-impact actions additionally show a confirmation card (e.g. coding tasks,
  file writes, posts/messages, process termination, meeting start/delete and
  Home Assistant scripts).
* Coding/agent work is separate: **Settings ▸ AI** picks models, and generated
  projects are written to `%USERPROFILE%\Desktop\JarvisProjects\<name>` in their
  own virtual environment.

---

## Security, privacy and data flow

* **Local-first storage.** Memory, logs, config and the optional Obsidian vault
  live under `data/`, `config/`, `memory/` and `Jarvis Obsidian Vault/`.
* **Meeting audio and the document library never leave the machine.** Meeting
  recordings are written to `data/meetings/<id>` and transcribed locally; only
  when you ask for a summary does the transcript *text* go to the active AI
  model — the audio does not. The library indexes files under `data/library/`
  and only the passages retrieved enter the conversation; source files are never
  renamed, rewritten or deleted.
* **The GitHub link is GET-only.** It cannot comment, merge, close, label or
  push; the model is told so, and those operations stay with the Composio
  connector.
* **Home Assistant control is a fixed allowlist.** Lights, switches, fans, input
  booleans, scenes and scripts only; locks, alarms, cameras and covers are
  refused by name, there is no arbitrary service call, every command is read
  back from the instance before success is claimed, and `run_script` confirms on
  the HUD first.
* **What leaves the machine (only when the matching feature is on):** live audio
  and, for vision features, screen frames/images are sent to Google Gemini; web
  research queries go to Gemini/DuckDuckGo; connector actions go through
  Composio. Turn those features off to keep them local.
* **Permissions fail closed.** See above.
* **Generated code is not run on the host by default.** The dev agent runs a
  project's own entry point with an allow-listed interpreter (`python`/`node`),
  an argv list and **no shell**, inside a per-project venv. Non-zero exit codes,
  timeouts and launch failures are reported as failures — never as success.
* **Child processes** are launched without a console window via
  `core/process_launch.py`; the app-wide default preserves any caller
  `startupinfo` and can be bypassed per call for a deliberately visible window.
* **No secret logging.** Credentials are redacted at the logging boundary; a
  diagnostic export is redacted too.

---

## Tests and development

The suite runs headless (offscreen Qt), needs **no** real API key, microphone,
camera, signed-in browser or destructive action, and skips (not fails) when an
optional engine is absent.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q         # core suite
.\.venv\Scripts\python.exe -m pytest tests/hybrid_voice -q # audio pipeline
```

UI tests must be run **one file per process** (a whole-tree run aborts inside a
native Qt/WebEngine call):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_chat_history.py -q
```

Static checks:

```powershell
.\.venv\Scripts\python.exe -m compileall -q actions app brain capabilities coding config connect connectors core dashboard documents evolution mcp_host memory observability security services voice
.\.venv\Scripts\python.exe -m pip check
```

CI (`.github/workflows/ci.yml`) byte-compiles everything and collects the full
suite on Linux, then runs `tests/unit` + `tests/hybrid_voice` on Windows. It
needs no credentials and carries **no badge until it has actually run green**.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError` at launch | Run `python setup_jarvis.py --install`, then launch via `start_jarvis.py`. |
| Wrong Python picked up | Run the wizard with the interpreter you want, or set it explicitly; the wizard refuses below its floor. |
| Node errors / Agent TARS won't start | Install **Node 22.15.0+**; re-run `python setup_jarvis.py --node`. |
| Playwright "browser not found" | `python setup_jarvis.py --browser`. |
| Voice silent / Gemini error | Check the key in **Settings ▸ AI** and run `python setup_jarvis.py --test`. The voice model is `gemini-3.8-live`; a retired model in your config is migrated automatically and reported. |
| Microphone/speaker issues | Pick devices in **Settings ▸ Audio**; the app probes devices at startup. |
| App won't start after a crash | `python setup_jarvis.py --inspect-crash`, or `python Closed_Jarvis.py`. |
| Several things look broken at once | `python setup_jarvis.py --auto-heal` — repairs the runtime, packages, config/keys, Gemini, SQLite stores, stale locks and caches in one pass, then lists anything it could not fix. |

**Where the logs are:** `data/logs/` (rotated structured JSONL) and the startup
error file `jarvis_startup_error.log`. Diagnostics never include secret values.

---

## Documentation

* `docs/CODEBASE_AUDIT.md` — the audit ledger: every finding with severity,
  evidence, fix status and test name.
* `docs/ARCHITECTURE.md` — startup flow, module boundaries, model routing,
  voice lifecycle, tool execution, permissions and integration boundaries.
* `docs/FEATURE_AUDIT.md`, `docs/REPAIR_REPORT.md`, `docs/TEST_RESULTS.md`,
  `docs/coding_agent_hardening.md` — per-pass ledgers.

---

## Contributing

Small, focused, test-backed changes are preferred. Add a regression test for any
bug fix, keep the suite deterministic and credential-free, and run
`tests/unit` before opening a change. Do not weaken or delete a test to make the
build green — document why if an assertion is genuinely wrong.

---

## Roadmap (not shipped)

Larger ideas that are explicitly **not** in this build: adopting async
(NON_BLOCKING) Live tool calling, a workflow-automation builder, per-server MCP
governance, a user-facing memory browser with retention controls, a
local-only/cloud-disabled mode, and an opt-in redacted telemetry pipeline.

---

## License

**CC BY-NC 4.0** — Creative Commons Attribution-NonCommercial 4.0 International.
Copyright (c) 2026 FatihMakes. **Commercial use is not permitted.** See
[`LICENSE`](LICENSE) for the full terms.
