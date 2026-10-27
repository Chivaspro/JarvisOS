"""Word boundaries survive the whole reply pipeline (the welded-words bug).

Reported on screen:

    "I have displayeda comprehensiveF2L algorithmcheat sheet for you, sir."

The transcription arrives in events that carry the word boundary as a LEADING
SPACE ("I have displayed", " a comprehensive", " F2L", ...). ``_clean_transcript``
stripped every event, so the streamed text welded the words together. The
accumulated whole-reply path never showed it because it joins the trimmed
fragments with a single space - which is exactly why the bug only appeared once
the reply started being streamed to the screen.

These tests pin:
  * the two views of one fragment (edge-preserving for the stream, trimmed for
    the accumulated reply),
  * the reported examples, fed as the SDK really sends them,
  * that normal messages, punctuation, newlines, Markdown and code blocks are
    left alone (no invented spaces, no collapsed runs),
  * that the wiring keeps using the edge-preserving form.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main():
    return pytest.importorskip("main")


# ── the fragment cleaner ─────────────────────────────────────────────────────

def test_a_fragment_keeps_its_word_boundary_when_asked(main):
    assert main._clean_transcript(" a comprehensive", preserve_edges=True) == \
        " a comprehensive"
    assert main._clean_transcript(" a comprehensive") == "a comprehensive"


def test_control_markers_are_still_removed_in_both_views(main):
    assert main._clean_transcript("<ctrl46>Hello there", preserve_edges=True) == \
        "Hello there"
    assert main._clean_transcript("<ctrl46>Hello there") == "Hello there"
    # ...including stray control characters from the service.
    assert main._clean_transcript("Hi\x07 there", preserve_edges=True) == "Hi there"
    assert main._clean_transcript("\x00\x0bSir.\x1f") == "Sir."


def test_newlines_and_tabs_are_not_eaten_by_the_cleaner(main):
    """Line breaks are payload (Markdown lists, code blocks), not noise."""
    block = "```python\n    x = 1\n```"
    assert main._clean_transcript(block, preserve_edges=True) == block
    assert main._clean_transcript("\n- item one\n", preserve_edges=True) == \
        "\n- item one\n"


def test_empty_and_none_inputs_are_safe(main):
    assert main._clean_transcript("") == ""
    assert main._clean_transcript(None, preserve_edges=True) == ""


# ── the reported examples, fed the way the SDK sends them ────────────────────

def _stream(fragments, main):
    """Drive the real streaming helpers with raw transcription events."""
    host = main.JarvisLive.__new__(main.JarvisLive)

    class _UI:
        def __init__(self):
            self.parts: list[str] = []

        def stream_assistant_text(self, chunk):
            self.parts.append(str(chunk))

        def flush_assistant_stream_text(self):
            pass

        def stream_shown(self):
            return True

        def stream_available(self):
            return True

        def finish_assistant_stream(self):
            pass

    class _Sink:
        def __init__(self):
            self.fed: list[str] = []

        def feed(self, text):
            self.fed.append(str(text))

        def flush(self):
            pass

    host.ui = _UI()
    host._speech_stream = _Sink()
    host._speech_sink = lambda: host._speech_stream
    host.voice_mode = lambda: "local"
    host._stream_gate = ""
    host._stream_buffer = ""
    host._live_out_fed = ""
    host._startup_greeting_text = None
    host._reply_stream_enabled = lambda: True

    for fragment in fragments:
        host._stream_reply_text(fragment)
    shown = "".join(host.ui.parts)
    spoken = "".join(host._speech_stream.fed)
    return shown, spoken


def test_the_first_reported_example_is_not_welded(main):
    """'I have displayeda comprehensiveF2L algorithmcheat sheet for you, sir.'"""
    fragments = ["I have displayed", " a comprehensive", " F2L", " algorithm",
                 " cheat sheet for you, sir."]

    shown, spoken = _stream(fragments, main)

    expected = "I have displayed a comprehensive F2L algorithm cheat sheet for you, sir."
    assert shown == expected, shown
    assert spoken == expected, spoken


def test_the_second_reported_example_is_not_welded(main):
    """'Good afternoon, sir. I'mready to continueour work on the Snakegame'"""
    fragments = ["Good afternoon, sir. I'm", " ready to continue", " our work on the",
                 " Snake", " game whenever you are."]

    shown, spoken = _stream(fragments, main)

    expected = ("Good afternoon, sir. I'm ready to continue our work on the "
                "Snake game whenever you are.")
    assert shown == expected, shown
    assert spoken == expected, spoken


def test_a_cumulative_transcript_keeps_the_boundary_spaces_too(main):
    """Some SDK versions resend the growing string: the new SUFFIX is what is
    streamed, and its leading space must survive."""
    fragments = ["I have displayed",
                 "I have displayed a comprehensive",
                 "I have displayed a comprehensive F2L algorithm cheat sheet "
                 "for you, sir."]

    shown, _ = _stream(fragments, main)

    assert shown == ("I have displayed a comprehensive F2L algorithm cheat "
                     "sheet for you, sir.")


def test_delta_fragments_without_leading_spaces_are_not_given_extra_ones(main):
    """The mirror requirement: never INVENT a space. A provider that streams
    tokens without boundary spaces must render exactly as it sent them."""
    fragments = ["Reproducing", " the ", "layout", " now."]

    shown, _ = _stream(fragments, main)

    assert shown == "Reproducing the layout now."


def test_provider_style_deltas_keep_their_spacing(main):
    """Typed turns stream provider deltas, which also carry their own boundary
    spaces ("Good", " evening", ",", " sir."). Same requirement, other source."""
    fragments = ["Good", " evening", ",", " sir.", " The F2L", " sheet", " is open."]

    shown, spoken = _stream(fragments, main)

    assert shown == "Good evening, sir. The F2L sheet is open."
    assert spoken == shown


def test_punctuation_and_markdown_are_preserved(main):
    fragments = ["Here is the plan:", "\n\n1. **Open** the file\n",
                 "2. `run()` it\n\n", "```python\nprint('hi')\n```"]

    shown, _ = _stream(fragments, main)

    assert shown == ("Here is the plan:\n\n1. **Open** the file\n"
                     "2. `run()` it\n\n```python\nprint('hi')\n```")
    assert "  " not in shown            # nothing doubled, nothing collapsed
    assert "run()" in shown and "**Open**" in shown


def test_urls_and_contractions_survive_untouched(main):
    fragments = ["I've saved it to https://example.com/a_b?x=1&y=2",
                 " - it's ready."]

    shown, _ = _stream(fragments, main)

    assert shown == ("I've saved it to https://example.com/a_b?x=1&y=2 - it's ready.")


# ── the accumulated whole-reply text is unchanged ────────────────────────────

def test_the_whole_reply_join_still_produces_single_spaces(main):
    """The path that NEVER had the bug must stay byte-for-byte as it was: the
    trimmed fragments joined with one space (greeting match, session ledger,
    memory write all depend on it)."""
    fragments = ["I have displayed", " a comprehensive", " F2L", " algorithm",
                 " cheat sheet for you, sir."]
    out_buf = [main._clean_transcript(f).strip() for f in fragments]

    assert " ".join(out_buf).strip() == (
        "I have displayed a comprehensive F2L algorithm cheat sheet for you, sir.")


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_stream_gets_the_edge_preserving_fragment():
    """A future edit must not quietly go back to streaming the trimmed text."""
    assert "fragment = _clean_transcript(" in MAIN_SRC
    assert "preserve_edges=True" in MAIN_SRC
    assert "self._stream_reply_text(fragment)" in MAIN_SRC
    assert "self._stream_reply_text(txt)" not in MAIN_SRC


def test_both_transcription_kinds_use_the_same_cleaner():
    """Input and output transcripts go through the one cleaner (so a fix in one
    place cannot miss the other)."""
    assert MAIN_SRC.count("_clean_transcript(") >= 2
