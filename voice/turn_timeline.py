"""Per-turn latency timeline for the reply pipeline.

Why this exists: the boot is measured (``core/startup_state.py`` →
``data/logs/startup.log``) but a *conversation turn* was not. Every latency
claim about JARVIS - "the Fish voice is slow", "it waits for the whole answer" -
was an impression. This module owns the measurement: exactly one record per
reply, written as one parseable line:

    TURN  kind=voice provider=gemini model=gemini-3.8-live engine=fish_audio
      seen_ms=812.4 submit_ms=829.1 first_audio_ms=1210.0 audible_ms=1213.5
      generation_ms=3402.2 total_ms=4104.9 chars=402 note=fallback=synchronized

Every timestamp is milliseconds measured from that turn's own ``send`` mark
(the moment the user's turn was received), on ``time.monotonic()``, so a wall
clock adjustment mid-turn cannot invent or hide latency.

Design rules (mirroring ``core/startup_state.py``):

* Standard library only and Qt-free - main.py writes, the CLI reads.
* ``mark`` is FIRST-WINS per tag: a fragment boundary, a retry or a superseded
  turn can never move the moment something *first* happened.
* Never raises. A broken log path, a foreign tag or a caller on a dying thread
  must not change the turn it is observing.
* Note values are scrubbed with the observability scrubber before they are
  written (a note carries engine errors, and those can quote an API response).

Read it back:

    python -m voice.turn_timeline --report
    python -m voice.turn_timeline --report --last 20 --raw
"""
from __future__ import annotations

import argparse
import statistics
import threading
import time
from pathlib import Path

__all__ = [
    "TAGS", "INTERVALS", "configure", "begin_turn", "mark", "note", "char_count",
    "end_turn", "snapshot", "recent", "load_records", "report",
    "reset_for_tests", "log_path",
]

#: The marks a turn can carry, in the order they normally happen.
TAGS = (
    "send",          # the user's turn was received (set by begin_turn)
    "first_text",    # first reply text became visible to the user
    "tts_submit",    # first text was handed to the speech engine
    "first_audio",   # first audio bytes arrived from the provider
    "playback",      # audio really started coming out of the speaker
    "stream_end",    # the model finished generating the reply text
    "turn_end",      # the turn settled (speech finished / work done)
)

#: ``(field, from_tag, to_tag, human label)`` - the intervals the report shows.
INTERVALS = (
    ("seen_ms", "send", "first_text", "Send -> first visible text"),
    ("submit_ms", "send", "tts_submit", "Send -> first text given to the voice engine"),
    ("first_audio_ms", "send", "first_audio", "Send -> first audio bytes from the provider"),
    ("audible_ms", "send", "playback", "Send -> audio AUDIBLE"),
    ("generation_ms", "send", "stream_end", "Send -> model finished the text"),
    ("total_ms", "send", "turn_end", "Send -> speech finished"),
    ("tts_ttfb_ms", "tts_submit", "first_audio", "voice submit -> first audio bytes"),
    ("tail_ms", "stream_end", "turn_end", "model done -> speech done"),
)

_BASE_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_LOG = _BASE_DIR / "data" / "logs" / "turn_timeline.log"

_lock = threading.RLock()
_log_lock = threading.Lock()

_log_path: Path | None = None
_log_configured: bool = False
_turn: dict | None = None
_recent: list[dict] = []
_RECENT_MAX = 200


# ── configuration ────────────────────────────────────────────────────────────

def configure(log_path: str | Path | None = None) -> None:
    """Point the timeline at ``log_path``; ``None`` disables file logging.

    Called once by main.py. Until it is, the default project log is used, so a
    test or a script that forgets to configure still records (and a test that
    wants silence calls ``configure(None)``).
    """
    global _log_path, _log_configured
    with _lock:
        _log_configured = True
        if log_path is None:
            _log_path = None
            return
        try:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _log_path = path
        except Exception:
            _log_path = None


def log_path() -> Path | None:
    """The file records are appended to (``None`` when logging is disabled)."""
    with _lock:
        if not _log_configured:
            return _DEFAULT_LOG
        return _log_path


# ── turn lifecycle ───────────────────────────────────────────────────────────

def _scrub(value) -> str:
    text = ""
    try:
        text = str(value or "")
    except Exception:
        return ""
    try:
        from observability.logger import scrub_text
        text = scrub_text(text)
    except Exception:
        pass
    return text[:160]


def begin_turn(kind: str = "voice", provider: str = "", model: str = "",
               engine: str = "", request: str = "") -> dict:
    """Start a new turn. Any turn still open is closed first, as superseded.

    Returns the live record (a dict) so a caller can read it, but the module
    keeps its own reference: all marks go through :func:`mark`.
    """
    global _turn
    try:
        with _lock:
            if _turn is not None:
                try:
                    _turn.setdefault("note", {})["superseded"] = "1"
                except Exception:
                    pass
                _close_locked()
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": _scrub(kind) or "unknown",
                "provider": _scrub(provider),
                "model": _scrub(model),
                "engine": _scrub(engine),
                "request": _scrub(request),
                "chars": 0,
                "marks": {},
                "note": {},
                "_t0": time.monotonic(),
            }
            record["marks"]["send"] = 0.0
            _turn = record
            return record
    except Exception:
        return {}


def mark(tag: str, value_ms: float | None = None) -> None:
    """Record ``tag`` on the active turn. First win; unknown tags are ignored.

    ``value_ms`` is only for a caller that measured the interval itself; it
    defaults to the elapsed monotonic time since this turn's ``send``.
    """
    try:
        name = str(tag or "").strip()
        if name not in TAGS:
            return
        with _lock:
            turn = _turn
            if turn is None:
                return
            marks = turn["marks"]
            if name in marks and name != "send":
                return                       # first win: never move a moment
            if value_ms is None:
                value_ms = (time.monotonic() - turn["_t0"]) * 1000.0
            marks[name] = max(0.0, float(value_ms))
    except Exception:
        pass


def note(key: str, value) -> None:
    """Attach one short diagnostic field (scrubbed). Later calls overwrite."""
    try:
        with _lock:
            if _turn is None:
                return
            _turn["note"][_scrub(key)] = _scrub(value)
    except Exception:
        pass


def char_count(value) -> None:
    """Record how many characters the reply carries (fragment-safe: max wins)."""
    try:
        n = len(str(value or ""))
        with _lock:
            if _turn is not None:
                _turn["chars"] = max(int(_turn.get("chars") or 0), n)
    except Exception:
        pass


def end_turn() -> dict:
    """Close the active turn, write its line and return the record."""
    with _lock:
        return _close_locked()


def _close_locked() -> dict:
    global _turn
    turn = _turn
    _turn = None
    if turn is None:
        return {}
    try:
        turn["marks"].setdefault("turn_end",
                                 (time.monotonic() - turn["_t0"]) * 1000.0)
        _recent.append(turn)
        del _recent[:-_RECENT_MAX]
        _write_line(turn)
    except Exception:
        pass
    return turn


# ── persistence ──────────────────────────────────────────────────────────────

def _interval(record: dict, interval) -> float | None:
    _, a, b, _ = interval
    try:
        marks = record.get("marks") or {}
        if a not in marks or b not in marks:
            return None
        value = float(marks[b]) - float(marks[a])
        return value if value >= 0 else None
    except Exception:
        return None


def _write_line(record: dict) -> None:
    path = log_path()
    if path is None:
        return
    try:
        parts = [
            "TURN",
            f"kind={record.get('kind') or '?'}",
            f"provider={record.get('provider') or '-'}",
            f"model={record.get('model') or '-'}",
            f"engine={record.get('engine') or '-'}",
        ]
        marks = record.get("marks") or {}
        for tag in TAGS:
            if tag in marks and tag != "send":
                parts.append(f"{tag}={float(marks[tag]):.1f}")
        parts.append(f"chars={int(record.get('chars') or 0)}")
        if record.get("request"):
            parts.append(f"request=\"{record['request']}\"")
        for key, value in (record.get("note") or {}).items():
            if value:
                parts.append(f"note={key}:{value}")
        line = f"{record.get('ts') or ''}  " + "  ".join(parts)
        with _log_lock:
            with open(path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(line + "\n")
    except Exception:
        pass


# ── reporting ────────────────────────────────────────────────────────────────

def snapshot() -> dict:
    """The live turn record (empty when no turn is open)."""
    with _lock:
        return dict(_turn) if _turn is not None else {}


def recent() -> list[dict]:
    """The last turns of THIS process, oldest first (for tests and tooling)."""
    with _lock:
        return [dict(r) for r in _recent]


def load_records(path: str | Path | None = None, last: int | None = None) -> list[dict]:
    """Parse records back out of the timeline log. Never raises."""
    target = Path(path) if path is not None else log_path()
    records: list[dict] = []
    if target is None:
        return records
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return records
    for line in lines:
        record = _parse_line(line)
        if record:
            records.append(record)
    if last is not None and last > 0:
        records = records[-int(last):]
    return records


def _parse_line(line: str) -> dict:
    """One ``TURN ...`` line → ``{"marks": {...}, ...}`` ({} when it is not one)."""
    try:
        text = str(line or "").strip()
        if "TURN" not in text:
            return {}
        head, _, rest = text.partition("TURN")
        record: dict = {"ts": head.strip(), "marks": {}, "note": {}}
        for token in rest.split():
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            if key in TAGS:
                try:
                    record["marks"][key] = float(value)
                except Exception:
                    continue
            elif key == "chars":
                try:
                    record["chars"] = int(value)
                except Exception:
                    pass
            elif key in ("kind", "provider", "model", "engine", "request"):
                record[key] = value
            elif key == "note":
                note_key, _, note_val = value.partition(":")
                record["note"][note_key] = note_val
        if not record["marks"]:
            return {}              # not a turn line (or an unreadable one)
        # The line omits ``send`` because it is always 0.0 by definition: every
        # other mark is measured FROM it, so parsing implies it back.
        record["marks"].setdefault("send", 0.0)
        return record
    except Exception:
        return {}


def _stats(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    out = {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 1),
        "min": round(ordered[0], 1),
        "max": round(ordered[-1], 1),
    }
    if len(ordered) >= 5:
        # p95 with the nearest-rank method; honest about needing a sample.
        idx = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
        out["p95"] = round(ordered[idx], 1)
    return out


def report(records: list[dict] | None = None) -> str:
    """A human-readable table of median/p95 per interval."""
    rows = list(records) if records is not None else load_records()
    lines = [f"turn timeline: {len(rows)} turn(s)"]
    if not rows:
        return lines[0] + " (nothing recorded yet)"
    kinds: dict[str, int] = {}
    for record in rows:
        kinds[str(record.get("kind") or "?")] = kinds.get(str(record.get("kind") or "?"), 0) + 1
    lines.append("kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    for interval in INTERVALS:
        values = [_interval(r, interval) for r in rows]
        st = _stats([v for v in values if v is not None])
        if not st.get("n"):
            continue
        p95 = f"  p95={st['p95']:>8.1f} ms" if "p95" in st else ""
        lines.append(
            f"  {interval[3]:<44} n={st['n']:<3} median={st['median']:>8.1f} ms"
            f"  min={st['min']:>8.1f}  max={st['max']:>8.1f}{p95}"
        )
    return "\n".join(lines)


def reset_for_tests() -> None:
    """Drop the live turn, the in-process history and the configured path."""
    global _turn, _log_path, _log_configured
    with _lock:
        _turn = None
        _recent.clear()
        _log_path = None
        _log_configured = False


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m voice.turn_timeline",
        description="Report the reply-latency timeline from data/logs/turn_timeline.log")
    parser.add_argument("--report", action="store_true",
                        help="print the median/p95 table (default action)")
    parser.add_argument("--file", default=None, help="timeline log to read")
    parser.add_argument("--last", type=int, default=None, help="only the last N turns")
    parser.add_argument("--raw", action="store_true", help="also echo the raw lines")
    args = parser.parse_args(argv)

    records = load_records(args.file, last=args.last)
    print(report(records))
    if args.raw:
        target = Path(args.file) if args.file else log_path()
        try:
            print()
            print(f"raw: {target}")
            for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
                if "TURN" in line:
                    print(line)
        except Exception as exc:
            print(f"(could not read {target}: {exc})")
    return 0


if __name__ == "__main__":       # pragma: no cover - exercised via _main in tests
    raise SystemExit(_main())
