"""The reply-latency timeline: measured per turn, never guessed.

These tests pin the properties a latency claim depends on:

* a mark records the FIRST time something happened (a fragment boundary or a
  superseded turn can never move it),
* every timestamp is measured from that turn's own ``send``,
* the written line parses back into the same numbers, and
* the report never invents a p95 out of a handful of samples, and never turns
  a MISSING mark into a zero-millisecond interval.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice import turn_timeline as tt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    tt.reset_for_tests()
    yield
    tt.reset_for_tests()


def _record(kind="voice", marks=None, **extra):
    record = {"ts": "2026-10-25 12:00:00", "kind": kind, "provider": "gemini",
              "model": "m", "engine": "e", "chars": 10, "marks": dict(marks or {}),
              "note": {}}
    record.update(extra)
    return record


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_begin_turn_starts_the_clock_at_send():
    tt.begin_turn(kind="voice", provider="gemini", engine="fish_audio")

    snap = tt.snapshot()
    assert snap["kind"] == "voice"
    assert snap["engine"] == "fish_audio"
    assert snap["marks"]["send"] == 0.0


def test_marks_are_first_wins_so_a_later_boundary_cannot_move_them():
    tt.begin_turn()
    tt.mark("first_text", 500.0)
    tt.mark("first_text", 900.0)      # a second fragment must not rewrite it

    assert tt.snapshot()["marks"]["first_text"] == 500.0


def test_unknown_tags_and_marks_without_a_turn_never_raise():
    tt.mark("first_text", 10.0)       # no turn open
    tt.note("engine_error", "boom")   # no turn open

    tt.begin_turn()
    tt.mark("not_a_tag", 10.0)
    tt.mark("first_text", "garbage")

    snap = tt.snapshot()
    assert "not_a_tag" not in snap["marks"]
    assert "first_text" not in snap["marks"], "a non-numeric mark is dropped, not zeroed"


def test_a_new_turn_supersedes_the_open_one_instead_of_losing_it(tmp_path):
    log = tmp_path / "turn_timeline.log"
    tt.configure(log)

    tt.begin_turn(kind="voice")
    tt.mark("first_text", 400.0)
    tt.begin_turn(kind="text")        # e.g. the user just typed the next turn

    records = tt.load_records(log)
    assert len(records) == 1
    assert records[0]["kind"] == "voice"
    assert records[0]["note"].get("superseded") == "1"
    assert tt.snapshot()["kind"] == "text"


# ── persistence ──────────────────────────────────────────────────────────────

def test_end_turn_writes_one_line_that_parses_back(tmp_path):
    log = tmp_path / "logs" / "turn_timeline.log"
    tt.configure(log)

    tt.begin_turn(kind="voice", provider="gemini", model="gemini-3.8-live",
                  engine="fish_audio", request="turn on the lights")
    tt.mark("first_text", 812.4)
    tt.mark("tts_submit", 829.1)
    tt.mark("first_audio", 1210.0)
    tt.mark("playback", 1213.5)
    tt.mark("stream_end", 3402.2)
    tt.char_count("Good evening, sir.")
    tt.note("fallback", "synchronized")
    tt.end_turn()

    records = tt.load_records(log)
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "voice"
    assert rec["engine"] == "fish_audio"
    assert rec["marks"]["first_text"] == pytest.approx(812.4)
    assert rec["marks"]["playback"] == pytest.approx(1213.5)
    assert rec["chars"] == len("Good evening, sir.")
    assert rec["note"]["fallback"] == "synchronized"


def test_end_turn_without_an_open_turn_is_a_no_op(tmp_path):
    tt.configure(tmp_path / "t.log")
    assert tt.end_turn() == {}
    assert tt.load_records(tmp_path / "t.log") == []


def test_char_count_keeps_the_longest_reply_seen():
    tt.begin_turn()
    tt.char_count("short")
    tt.char_count("a much longer reply than the first fragment")

    assert tt.snapshot()["chars"] == len("a much longer reply than the first fragment")


def test_note_values_are_scrubbed_before_they_are_written(tmp_path):
    log = tmp_path / "t.log"
    tt.configure(log)

    tt.begin_turn()
    # An exception string quoting a real credential shape must never persist.
    tt.note("engine_error", "401 from provider with key sk-fish-abcdefghijklmnopqrst")
    tt.end_turn()

    text = log.read_text(encoding="utf-8")
    assert "sk-fish-abcdefghijklmnopqrst" not in text
    assert "[REDACTED]" in text


def test_disabling_the_file_still_records_in_memory():
    tt.configure(None)

    tt.begin_turn(kind="text")
    tt.mark("first_text", 120.0)
    record = tt.end_turn()

    assert record["marks"]["first_text"] == 120.0
    assert len(tt.recent()) == 1


# ── reporting ────────────────────────────────────────────────────────────────

def test_missing_marks_skip_their_intervals_instead_of_reporting_zero():
    records = [
        _record(marks={"send": 0.0, "first_text": 300.0}),          # text only
        _record(marks={"send": 0.0, "first_text": 500.0, "playback": 1500.0}),
    ]

    text = tt.report(records)

    assert "Send -> first visible text" in text
    assert "n=2" in text
    assert "Send -> audio AUDIBLE" in text and "n=1" in text
    assert "1500.0" in text
    # No audio was ever reported for the first turn: the interval must not
    # appear as a zero.
    assert "median=0.0" not in text


def test_p95_is_only_reported_once_the_sample_size_justifies_it():
    four = [_record(marks={"send": 0.0, "first_text": float(i)}) for i in (100, 200, 300, 400)]
    five = four + [_record(marks={"send": 0.0, "first_text": 500.0})]

    assert "p95" not in tt.report(four)
    assert "p95" in tt.report(five)


def test_a_line_with_no_readable_marks_is_not_a_turn(tmp_path):
    log = tmp_path / "t.log"
    log.write_text("2026-10-25 12:00:00  TURN  kind=voice  chars=10\n", encoding="utf-8")

    assert tt.load_records(log) == []


def test_cli_report_prints_the_table_for_a_file(tmp_path, capsys):
    log = tmp_path / "turn_timeline.log"
    tt.configure(log)
    for value in (100.0, 200.0, 300.0, 400.0, 900.0):
        tt.begin_turn()
        tt.mark("first_text", value)
        tt.mark("playback", value + 50.0)
        tt.end_turn()

    assert tt._main(["--report", "--file", str(log)]) == 0

    out = capsys.readouterr().out
    assert "5 turn(s)" in out
    assert "Send -> audio AUDIBLE" in out
    assert "median=" in out and "p95=" in out


def test_the_report_is_honest_about_an_empty_log(tmp_path):
    assert "nothing recorded yet" in tt.report(tt.load_records(tmp_path / "missing.log"))


# ── main.py wiring (source-level guards) ─────────────────────────────────────
# The numbers only exist if the pipeline actually marks them; these guards pin
# the call sites the report depends on, in the style of test_text_turn_routing.

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def test_main_owns_one_timeline_and_configures_its_log():
    assert "from voice import turn_timeline as _timeline" in MAIN
    assert "turn_timeline.log" in MAIN


def test_every_voice_renderer_marks_the_same_timeline():
    # native Gemini audio
    assert '_timeline.mark("first_audio")' in MAIN
    assert '_timeline.mark("playback")' in MAIN
    # local engines (Fish/ElevenLabs/Edge/ONNX) and the Piper pipeline
    assert '_timeline.mark("tts_submit")' in MAIN
    assert '_timeline.mark("stream_end")' in MAIN
    assert '_timeline.mark("turn_end")' in MAIN
    assert "_timeline.end_turn()" in MAIN


def test_a_spoken_turn_starts_its_clock_when_the_user_stops_talking():
    """The measured interval must start at the real end of speech — the same
    instant the client VAD finalises the turn — not at some later marker."""
    marker = 'self._begin_turn_timeline("voice")'
    assert marker in MAIN
    assert MAIN.index(marker) > MAIN.index("self._last_utterance_end_t = time.monotonic()") - 500


def test_narration_and_the_greeting_never_claim_the_reply_s_audio():
    """A narration line ("opening the page") plays before the reply; counting
    it as the turn's first audio would make the numbers lie."""
    assert "mark_timeline=False" in MAIN
    narrate = MAIN.index("def _narrate_line")
    block = MAIN[narrate:narrate + 1600]
    assert "mark_timeline=False" in block
