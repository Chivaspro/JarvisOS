# Chat Scroll, Voice Submission & Settings Repair — Completion Report

Date: 2026-10-17
Scope: the reported chat-log auto-scroll failures, the voice-input submission
failure ("the spoken command never becomes a submitted user message"), a
settings functionality audit, a small set of verified defect fixes, and a
bounded feature gap (chat message copy).

Everything below was **executed**; nothing is claimed from inspection alone.
Test commands and their results are in §6. The one thing that cannot run
headless — a live Gemini-Live voice session — is called out in §7 with a
user-run smoke procedure.

---

## 1. Root causes found

### 1a. Chat auto-scroll

1. **No follow-state existed.** `_RoundedChatLog._scroll_bottom` animated to the
   bottom on *every* append. A reader who scrolled up into older messages was
   yanked back down by any incoming J.A.R.V.I.S. message.
2. **The entrance animation defeated the single coalesced scroll.** A new row
   reveals its height over 220 ms (`play_entrance_animation`), but the one
   deferred scroll tick fired while the row was still ~1 px tall, read a stale
   `bar.maximum()`, and nothing re-scrolled when the reveal finished — the newest
   message settled partly below the fold ("stops halfway"). Streaming rows only
   survived because each chunk re-requested.
3. **History restore used arbitrary fixed delays.** `load_conversation` scrolled
   at 0/120/400 ms and *animated* the jump, instead of positioning immediately
   and settling with the layout.
4. **Resize never re-pinned** a reader who was parked at the bottom.

### 1b. Voice input → message submission

1. **Both the user turn and the reply were gated on `_command_window.isVisible()`.**
   With the chat panel tucked away (but alive), the finalized utterance reached
   only the activity feed and an in-memory buffer: never the chat panel, never
   the conversation store. Re-showing did not replay it. This is the primary
   root cause of "the recognized command does not appear as a submitted user
   message".
2. **Two different message pipelines.** Typed messages display + record through
   `append_msg`; voice messages rode a string transport (`write_log("You: …")`
   with prefix parsing and `raw[4:]` slicing) — exactly the "separate,
   inconsistent behavior" the requirements forbid.
3. **Stale composer echo.** The composer echo was cleared only when
   `box.text() == last echo`, and any non-empty focused box was guessed to be a
   *user draft* (`box.hasFocus() and current != echoed`). A leftover echo from a
   previous turn was therefore misread as a draft: the next utterance's live
   transcript was suppressed and the stale command stayed in the input box
   forever — the reported symptom.

### 1c. Other verified defects

1. **`_MicIconButton` repaint storm:** its 16 ms timer started in `__init__` and
   never stopped — an idle button repainted ~60×/second.
2. **The mic button had no state** (always the same glyph), while the status rail
   already knew MUTED/LISTENING/THINKING/SPEAKING.
3. **First-open chat replay re-recorded** buffered turns on a window
   rebuild (`_chat_history` replay ran through `append_msg` → `record_message`).
4. **Dead settings (saved, read by nothing):** `chat_filter` (the "Show in chat"
   preset), `panel_always_on_top` ("Keep HUD windows always on top"),
   `memory_autosave` ("Auto-save remembered facts").
5. **No copy action on chat rows** (painted widgets, no native selection).

---

## 2. Files modified

| File | Change |
|---|---|
| `ui.py` | `_RoundedChatLog`: follow-bottom state machine (wheel/key input), single coalesced scroll with force-on-user-message, `scroll_bottom_immediate`, bounded self-terminating restore settle loop, `rangeChanged` re-pin, resize re-pin, reduced-motion path via `smooth_animations`; `_ChatMessageRow.play_entrance_animation(on_finished=…)` + reveal re-pin hook; `_ChatMessageRow` context menu (Copy message / Copy whole conversation); `_MicIconButton` state glyph + hover-only timer; `_CommandWindow.set_live_transcript` (textEdited draft tracking + persistent echo log + robust clear) and `_bind_speech_composer`; `_send` clears the draft flag; `RefinedCommandWindow.set_status` forwards mic state; `load_conversation` restore uses `begin_settle_follow`; `MainWindow`: `_live_command_window` (existence-based), `_apply_chat_turn` / `submit_chat_turn` + `_chat_turn_sig`, existence gates in `_log_from_backend` and the stream methods, first-open replay records once; `_panel_always_on_top` init + apply + live-apply; `_UIProxy.submit_chat_turn` |
| `main.py` | Final voice-turn handling submits through `ui.submit_chat_turn("user", …)` (presentation only, never re-dispatched) with a duplicate-final guard |
| `ui_settings.py` | `_apply_chat_filter_preset`: the "Show in chat" preset now drives the real per-category filters |
| `memory/manager.py` | `BrainMemory.autosave_enabled()` + gate: "Auto-save remembered facts" OFF stops automatic capture only; explicit remember commands still honoured |
| `tests/ui/test_chat_autoscroll.py` | Rewritten to the new contract (11 tests) |
| `tests/ui/test_live_transcript_clear.py` | Extended: leftover echo, late fragment, empty final, mid-turn edit (15 tests) |
| `tests/ui/test_voice_turn_submission.py` | New (6 tests) |
| `tests/ui/test_mic_button_state.py` | New (4 tests) |
| `tests/ui/test_chat_row_copy.py` | New (3 tests) |
| `tests/ui/test_settings_functional.py` | New (6 tests) |
| `tests/ui/test_boot_smoke_voice_chat.py` | New — real `MainWindow` end-to-end (4 tests) |
| `tests/unit/test_voice_submit_contract.py` | New — runtime wiring contract (7 tests) |
| `docs/CHAT_VOICE_REPAIR_REPORT.md` | This report |

The owner's in-flight working tree (MCP/REA, task engine, vault, other ledgers)
was left untouched.

---

## 3. Bugs fixed

- **Auto-scroll:** new user messages always follow to the newest line; incoming
  replies never yank a reader who is up in history; returning within ~12 % of the
  viewport (min 48 px) resumes following; opening/restoring a conversation jumps
  immediately to the newest message and re-settles through layout changes;
  resize keeps a bottom-parked reader pinned; entrance-reveal completion re-pins.
  Reduced-motion users get instant positioning (Smooth animations OFF).
- **Voice submission:** a finalized utterance is displayed *and* recorded in the
  same conversation as the reply, whether the panel is visible, hidden, or not
  yet created; the command is never re-dispatched (one execution per utterance);
  a duplicate final event is suppressed; the composer clears on turn end without
  touching a user's typed draft, and a stale echo can no longer suppress the next
  utterance's transcript.
- **Replies/streams** now display and record while the panel is hidden.
- **First-open replay** records buffered turns exactly once.
- **Mic button** reflects MUTED / LISTENING / PROCESSING / SPEAKING and no longer
  repaints while idle.
- **Settings:** the "Show in chat" preset now sets the real category filters;
  "Keep HUD windows always on top" controls the panel window flag (live);
  "Auto-save remembered facts" gates automatic memory capture.
- **Chat rows** can be copied (message / whole conversation).

---

## 4. Settings audit

Method: every key written by the settings save path was cross-referenced against
its consumers across the codebase (quoted-key search excluding tests and the
settings module itself). Result:

| Setting | Consumer | Status |
|---|---|---|
| `ai_provider`, `ai_model`, custom provider fields | ai_providers / forge / laya_decision / main | verified wired |
| `camera_index`, `web_homepage` | camera / diagnostics / ui | verified wired |
| `features` map (`smooth_animations`, `realtime_stt`, `autostart`, `mic_access`, `drag_reactor`, `multi_window`, …) | brain_bridge / main / ui / config | verified wired; `smooth_animations` now also gates the chat glide |
| `chat_font_scale`, `hide_chat_lines`, `chat_filter_categories` | ui | verified wired |
| **`chat_filter`** | — (none) | **was dead — fixed** (now drives the category filters) |
| **`panel_always_on_top`** | — (none) | **was dead — fixed** (panel window flag, live) |
| **`memory_autosave`** | — (none) | **was dead — fixed** (gates automatic capture) |
| `model_roles` | model_router / config_guard | verified wired |

Completeness: the audit covered the saved-key surface programmatically; the
interview-style controls on tabs whose effects are provider-scoped (voice
speed/pitch/volume, onnx voice, fish/elevenlabs) were previously verified in
`docs/FEATURE_AUDIT.md` and were not re-derived here. `remember_belief` (Mind
beliefs) intentionally remains outside `memory_autosave`: it is a derived
reflection, not a "remembered fact". Settings are applied live where a consumer
exists and persisted through the existing `config/api_keys.json` writer; no new
storage was introduced and no secrets are stored client-side.

---

## 5. New features implemented

- **Copy message text / copy whole conversation** from the chat row context menu.
- **Live microphone state glyph** (muted / listening / processing / speaking).
- Existing conversation management (search, rename, delete, new chat, restore,
  export/import), Stop/interrupt and the send path were **verified as already
  present** and covered by the existing `test_chat_history.py` (13 passed) and
  `test_send_stop_and_import.py` (9 passed) rather than rebuilt.

---

## 6. Tests performed

All with `JarvisOS Main\.venv\Scripts\python.exe`, from the project root.

| Command | Result |
|---|---|
| `pytest tests/unit -q` | **1614 passed, 5 skipped** (0 failures; documented baseline was 1330/6, so no regression and the tree already contained more tests) |
| `pytest tests/hybrid_voice -q` | **153 passed, 2 skipped** (matches baseline) |
| `pytest tests/ui/test_chat_autoscroll.py tests/ui/test_live_transcript_clear.py tests/ui/test_voice_turn_submission.py tests/ui/test_mic_button_state.py tests/ui/test_chat_row_copy.py tests/ui/test_settings_functional.py tests/ui/test_boot_smoke_voice_chat.py tests/ui/test_chat_history.py tests/unit/test_voice_submit_contract.py -q` | **69 passed** |
| `pytest tests/ui/test_confirm_banner.py test_chat_paragraphs.py test_send_stop_and_import.py test_typing_lifecycle.py test_typing_indicator_reuse.py test_window_interaction.py -q` | **54 passed** |
| `pytest tests/ui/test_mind_panel.py test_memory_core.py test_quick_setup.py test_activity_feed_incremental.py test_history_stage.py test_never_asleep_ui.py test_ui_control_open_verify.py -q` | **102 passed** |
| `pytest tests/ui/test_boot_smoke_voice_chat.py -q` | **4 passed** (real `MainWindow`: turn recorded + shown; hidden panel still records; opening/switching conversations lands on the newest message) |
| `python -m compileall ui.py main.py ui_settings.py memory/manager.py` | clean |

Scenarios covered by the new/updated tests: burst coalescing to one scroll;
assistant row while reading stays put; user row forces follow; near-bottom resumes
following; restore lands immediately and the settle loop self-terminates; late
height change re-pins; resize behaviour both ways; reveal completion re-pins;
smooth-vs-reduced-motion choice; user draft protection; leftover-echo recovery;
late duplicate fragment; empty final; mid-turn edit; voice turn visible/hidden,
with and without a panel; turn ordering; hidden replies; duplicate-final guard;
mic state + idle timer; settings preset and memory auto-save gate; copy actions.

`tests/ui` as a **whole tree in one process** is not usable in this environment
(native teardown abort / order-dependent flakiness); the project's own guidance
is one file per process, which is how the suites above were run. `tests/ui/
test_vision_rail.py` aborts natively on its own as well — already documented in
`docs/TEST_RESULTS.md` ("native teardown abort") and reproduced here inside the
microphone-waveform `paintEvent` (`ui.py` ~4853/4928), code this change set did
not touch. An unrelated order-dependent failure seen in one whole-tree attempt
does not reproduce when its file is run alone.

---

## 7. Remaining issues / not verified

- **Live Gemini-Live voice** (real microphone → server transcription → tool
  execution) cannot run headless. Every layer after the transcript is verified;
  the audio path itself needs a user run: speak "Jarvis, open YouTube" and
  confirm (a) interim text appears then clears from the composer, (b) the chat
  shows the "You" bubble above the reply in the same conversation, (c) Chat
  History shows both rows exactly once, (d) scrolling up mid-reply stays put and
  returns to following near the bottom.
- **Environment blockers (unchanged, not defects):** `torch` import
  access-violates (YOLO path), `pywinauto` DLL fails, the bundled
  `jarvis-high.onnx` Piper voice is absent — Piper/local-voice modes stay
  documented-unavailable as before.
- `panel_always_on_top` and a live mic-state change were verified at the source
  and unit level (window flag / state mapping); their on-screen appearance in a
  real desktop session was not re-verified visually.
- The report is a self-audit of this change set, not independent verification.
