# Realtime replies: speak and show the answer while it is written

**Status:** implemented and tested; wired into the app. Supersedes the previous
"render the reply once it is complete" behaviour in local voice mode.

---

## 1. What was wrong (traced, not guessed)

In **local voice mode** (the mode where Fish Audio and every other non-Gemini
engine renders the voice), the reply path was:

```text
mic / typed text
   -> Gemini Live (or a plain provider over HTTP)
   -> output_transcription fragments accumulate in out_buf   (main.py)
   -> turn_complete
        -> ui.write_log(full_text)            one block of text, at the end
        -> speak_local(full_text)             Fish TTS starts HERE
             -> voice/tts.py: split into sentences, one HTTP POST per sentence
```

Two waits stacked on top of each other:

1. **the whole answer had to exist** before the first sample was requested;
2. every sentence then paid its own request + time-to-first-byte.

And a third, smaller one: the WebSocket/HTTP session is opened *after* the first
text exists, so its handshake sits on the critical path.

The reply text also arrived on screen in one block, because nothing fed the
fragments to the UI even though `ui.py` has had a streaming API
(`begin_assistant_stream` / `stream_assistant_text` / `finish_assistant_stream`)
since before this change - with no producer.

## 2. What it does now

```text
mic / typed text
   -> Gemini Live / streaming provider HTTP
   -> each fragment, immediately:
        ui.stream_assistant_text(fragment)        text grows on screen
        sink.feed(fragment)                       voice starts on the first phrase
   -> turn_complete
        sink.flush()                              speaks the tail, closes the session
        ui.finish_assistant_stream()               the stream row IS the record
```

Pieces (all in the repo):

| Piece | File | Role |
|---|---|---|
| Live speech sink | `voice/speech_stream.py` | bounded intake, punctuation chunking, one worker, epoch+cancel interruption; decides the backend per turn |
| Fish realtime backend | `voice/tts.py::FishAudioTTSEngine.ws_stream` | one WebSocket session per turn (`/v1/tts/live`), incremental TEXT in, raw 24 kHz PCM out, written to one open device stream |
| Engine backend | `voice/tts.py::TTSPlayer` | every other engine, sentence by sentence (the pre-existing path, unchanged) |
| Provider streaming | `app/ai_providers.py::chat_stream` | OpenAI-compatible SSE + Anthropic event stream; same validation as `chat()` |
| Turn wiring | `main.py` | streams fragments to UI+voice, closes the turn once, prepares the session early, cancels on interrupt |
| UI coalescing | `ui.py` | bounded update rate (25 Hz) so a token storm cannot peg the GUI thread; legacy surfaces kept for the no-panel case |
| Measurement | `voice/turn_timeline.py` | per-turn timeline → `data/logs/turn_timeline.log` |

### The Fish realtime session, concretely

* SDK: the installed `fish-audio-sdk` `client.tts.stream_websocket(...)` →
  WebSocket `/v1/tts/live`, events `start` / `text` / `stop`; audio bytes are
  yielded as they arrive while a separate sender thread consumes the text.
* Format: `pcm`, mono, **24 kHz** - raw PCM is written straight to the speaker,
  no container and no decoder in the path.
* `chunk_length=100` (fastest the API accepts), `min_chunk_length=12`,
  `latency` from the user's stored setting (`balanced` in this install).
* One session per turn, opened when the turn starts (`prepare()`), so the
  handshake overlaps generation. A turn that never produces text closes the
  idle session by itself.

## 3. Measured (real account, 2026-10-26, `s2.1-pro-free`, same text, no audio device)

Time to the **first PCM chunk**, two sentences fed 0.8 s apart:

| `min_chunk_length` | first audio |
|---|---|
| 50 (SDK default) | 2602 ms |
| 20 | 1849 ms |
| **12 (shipped)** | **1063 ms** |
| 5 | 1124 ms |

A very short single phrase (`"Yes, sir."`) at `min_chunk_length=5`: 1364 ms.

Why 12: it lets the **first sentence** start generation on its own. At 50 the
server held audio back until the next sentence arrived, which was most of the
difference. 5 bought nothing further and would synthesise two-word fragments.

Realtime vs the previous whole-reply path, same voice and reply
(`tests/unit/test_fish_realtime.py::test_live_after_vs_before_the_same_reply`):

```text
[live] first audible: BEFORE=5864 ms  AFTER=4572 ms  (delta +1292 ms)
```

**Read that number with its caveats, which the run printed:** the Fish account's
**API credit is exhausted** (`402 Insufficient API credit` on the plain HTTP
endpoint), so the BEFORE leg fell back to the SDK whole-reply fetch and both
legs were running against a throttled account. It is a real end-to-end
measurement, not a clean one; the `min_chunk_length` table above was taken
while credit was healthy and is the trustworthy latency figure.

## 4. What the user experiences

* text appears progressively (bounded to 25 updates/second),
* speech starts on the first phrase (measured ~1.0 s from the first text with
  healthy credit, plus whatever the model takes to produce that text),
* the spoken words are the displayed words - both come from the same fragments,
* an interruption (ESC / the ✋ control / wake word) cancels the live session,
  drops queued speech and closes the WebSocket; nothing from the interrupted
  reply resumes,
* a voice failure costs the voice, never the conversation: the sink falls back
  to the engine path, reports it in the activity log and the turn timeline, and
  the text stays on screen.

## 5. Settings

| Setting | Default | Effect |
|---|---|---|
| `features.tts_realtime` | ON | the reply is spoken **and shown** while it is written (this document) |
| `features.tts_streaming` | ON | cloud voices render sentence by sentence |
| both OFF | - | synchronized fallback: the complete reply is published at the same moment as its audio |

Both are in Settings ▸ Automation with tooltips explaining the audible change.

## 6. How to measure your own turns

Every turn writes one line to `data/logs/turn_timeline.log`:

```text
TURN kind=voice provider=gemini model=gemini-3.8-live engine=fishAudio
     first_text=812.4 tts_submit=829.1 first_audio=1210.0 playback=1213.5
     stream_end=3402.2 turn_end=4104.9 chars=402
```

```powershell
.\.venv\Scripts\python.exe -m voice.turn_timeline --report          # median / min / max / p95
.\.venv\Scripts\python.exe -m voice.turn_timeline --report --last 20 --raw
```

The headline interval is `Send -> audio AUDIBLE`. `p95` is only printed once
the sample size justifies it (5+ turns); a missing mark skips its interval
rather than reporting a zero.

## 7. Tests

| File | Covers |
|---|---|
| `tests/unit/test_speech_stream.py` | chunking, delta vs cumulative intake, ordering, bounded queue, interrupt, renderer failure, Fish session fallback, prepare/idle-close, no-leak diagnostics |
| `tests/unit/test_fish_realtime.py` | the realtime session against a fake SDK: incremental in, audio before the text stream ends, PCM order/rate, one device stream per turn, cancellation, both failure phases, opt-in live tests |
| `tests/unit/test_provider_streaming.py` | SSE parsers (both wire formats), keep-alives/malformed frames, cancellation, validation parity, and the turn wiring incl. the synchronized fallback |
| `tests/unit/test_realtime_reply_stream.py` | fragment order, exactly-once close, greeting gate, no-sink fallback, prepare, interrupt/narration/priority guards |
| `tests/ui/test_streamed_reply_ui.py` | coalescing (120 tokens → bounded updates, complete text), recorded once, no-panel visibility |
| `tests/unit/test_turn_timeline.py` | first-wins marks, parse round-trip, scrubbing, honest report |

Live (opt-in, spends credits):

```powershell
$env:JARVIS_LIVE_FISH="1"
.\.venv\Scripts\python.exe -m pytest tests/unit/test_fish_realtime.py -q -k live -s
```

## 8. Limits, honestly

* The WebSocket handshake (~0.6-0.9 s measured) is per **turn**, not per
  sentence - but it is still a handshake per reply, unless `prepare()` ran.
* `pcm`/24 kHz is what was verified against this account and model. A provider
  change (format rejected) raises before the first audio and the turn renders
  through the engine path instead - degraded, reported, not silent.
* Model-credit exhaustion (402) is handled as a runtime failure: the sink
  switches to the engine path and the app reports it; no reply is lost.
* The Gemini-native modes (`gemini`, `gemini_piper`) were already realtime
  (speech-to-speech / per-fragment Piper) and are untouched by this work.
* The numbers in §3 are single-run timings from one machine and one account;
  they are evidence of the direction and the magnitude, not a benchmark suite.
