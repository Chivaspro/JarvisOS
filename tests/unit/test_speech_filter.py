"""Markdown → speech filter: rules, streaming safety, pacing, integration.

The user-facing contract these tests pin:

* the chat still shows full markdown (untouched — this module never runs on
  display text);
* the VOICE speaks prose only — no ``#``, no ``**``, no backticks, no table
  pipes, and **code blocks are skipped silently**;
* a reply streamed in arbitrary deltas produces byte-identical speech to the
  same reply filtered in one shot (the filter can never leak a broken marker);
* the typewriter's pacing counts spoken letters, so code blocks reveal at a
  visible trickle instead of dragging the voice-synced reveal behind.
"""
from __future__ import annotations

import random

import pytest

from voice.speech_filter import (
    MarkdownSpeechFilter,
    SpeechCostModel,
    filter_markdown_for_speech,
)


def spoken_lines(text: str):
    """The spoken content as clean lines (newline counts are noise here)."""
    out = filter_markdown_for_speech(text)
    return [line.strip() for line in out.split("\n") if line.strip()]


# ── the exact scenario from the user ────────────────────────────────────────


CASE_MARKDOWN_DEMO = (
    "Certainly, sir. Here is an example of Markdown formatting for your review:\n"
    "\n"
    "# This is a heading\n"
    "\n"
    "**Bold text**, *italic text*, and `inline code`.\n"
    "\n"
    "```markdown\n"
    "# Heading\n"
    "**bold**\n"
    "```\n"
    "\n"
    "| Column A | Column B |\n"
    "| -------- | -------- |\n"
    "| 1        | 2        |\n"
    "\n"
    "And a [link](https://example.com) to finish.\n"
)


def test_markdown_demo_speaks_only_prose():
    lines = spoken_lines(CASE_MARKDOWN_DEMO)
    assert lines[0] == ("Certainly, sir. Here is an example of Markdown "
                        "formatting for your review:")
    joined = "\n".join(lines)
    # syntax can never be spoken
    assert "#" not in joined
    assert "**" not in joined
    assert "`" not in joined
    assert "|" not in joined
    assert "https://" not in joined
    # code-block content stays on screen
    assert "Heading" not in joined.replace("This is a heading", "")
    assert "bold" not in joined.lower().replace("bold text", "")
    # readable things stay readable
    assert "This is a heading" in joined
    assert "Bold text, italic text, and inline code." in joined
    assert "And a link to finish." in joined


def test_code_block_is_skipped_silently():
    text = "Here is the code:\n```python\nprint('boom')\nx = 1\n```\nDone."
    assert spoken_lines(text) == ["Here is the code:", "Done."]


def test_table_is_skipped_silently():
    text = "Prices follow.\n| Item | Cost |\n| a | b |\nThat is all."
    assert spoken_lines(text) == ["Prices follow.", "That is all."]


# ── rule coverage ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("src,expected", [
    ("## Title ##", "Title"),
    ("#### Deep heading", "Deep heading"),
    ("**bold** words", "bold words"),
    ("__bold__ words", "bold words"),
    ("*italic* words", "italic words"),
    ("_italic_ words", "italic words"),
    ("~~struck~~ words", "struck words"),
    ("text with `code span` inside", "text with code span inside"),
    ("> quoted wisdom", "quoted wisdom"),
    (">> deep quote", "deep quote"),
    ("- list item", "list item"),
    ("* star item", "star item"),
    ("1. numbered item", "numbered item"),
    ("12) paren item", "paren item"),
    ("***", ""),
    ("---", ""),
    ("[anchor text](https://x.example/y)", "anchor text"),
    ("[ref][1]", "ref"),
    ("![diagram](img.png) caption", "caption"),
    ("see <https://x.example> now", "see now"),
    ("visit https://x.example/path for more", "visit for more"),
    ("keep snake_case_name intact", "keep snake_case_name intact"),
    ("well-known problem", "well-known problem"),
    ("a &amp; b", "a & b"),
    ("plain sentence.", "plain sentence."),
])
def test_inline_rules(src, expected):
    assert filter_markdown_for_speech(src).strip() == expected


def test_indented_and_nested_list_items_speak():
    text = "- first\n  - nested\n1. second"
    assert spoken_lines(text) == ["first", "nested", "second"]


def test_unterminated_fence_swallows_rest_at_flush():
    text = "Intro.\n```python\nx = 1\n"
    assert spoken_lines(text) == ["Intro."]


# ── streaming safety ────────────────────────────────────────────────────────


CORPUS = [
    CASE_MARKDOWN_DEMO,
    "Intro.\n```python\nx = 1\n```\nOutro.",
    "## Head\n**bold** and `code`\n- item one\n- item two\n| a | b |\nDone.",
    "No markdown at all, just a sentence with commas, and one more.",
    "Math: 2 + 2 = 4, and a [link](u) plus > quote\n\n`x_y_z` end.",
    "Fence at the very end\n```\n",
    "See [ref][1] and [anchor](https://e.example) done.\n",
]


@pytest.mark.parametrize("text", CORPUS)
def test_streaming_matches_one_shot_for_many_splits(text):
    expected = filter_markdown_for_speech(text)
    rng = random.Random(20261031)
    for _ in range(60):
        f = MarkdownSpeechFilter()
        pieces = []
        i = 0
        while i < len(text):
            step = rng.randint(1, 7)
            pieces.append(f.feed(text[i:i + step]))
            i += step
        pieces.append(f.flush())
        got = "".join(pieces)
        assert got == expected, (text, got, expected)


def test_character_by_character_equals_one_shot():
    text = CASE_MARKDOWN_DEMO
    expected = filter_markdown_for_speech(text)
    f = MarkdownSpeechFilter()
    got = "".join(f.feed(ch) for ch in text) + f.flush()
    assert got == expected


def test_idempotent():
    for text in CORPUS:
        once = filter_markdown_for_speech(text)
        twice = filter_markdown_for_speech(once)
        assert twice == once


def test_reset_drops_state():
    f = MarkdownSpeechFilter()
    f.feed("```python\nsecret code")
    f.reset()
    assert f.feed("Recovered sentence.") .strip() == "Recovered sentence."


def test_heading_marker_split_across_deltas_never_leaks():
    f = MarkdownSpeechFilter()
    out = f.feed("##") + f.feed("# Real title\n") + f.flush()
    assert "#" not in out
    assert "Real title" in out


def test_bold_marker_split_across_deltas_never_leaks():
    f = MarkdownSpeechFilter()
    out = (f.feed("hello **bol") + f.feed("d** world") + f.feed("!") + f.flush())
    assert "*" not in out
    assert out.replace(" ", "") == "helloboldworld!"


# ── the pacing oracle ───────────────────────────────────────────────────────


def test_cost_model_prose_vs_code():
    m = SpeechCostModel()
    prose_letters = m.split_token("Hello\n")[0]
    assert prose_letters == 5
    # Tokens arrive exactly as ui_typewriter's splitter produces them
    # (trailing whitespace included) — the newline marks a line start.
    assert m.split_token("```python\n") == (0, 0.0)
    code_letters = m.split_token("print(x_here_12345)\n")[0]
    # closing fence back to prose
    assert m.split_token("```\n") == (0, 0.0)
    after = m.split_token("Back\n")[0]
    assert after == 4
    # code trickles: far fewer "letters" than its characters
    assert 0 < code_letters < 21 // 2


def test_cost_model_pauses():
    m = SpeechCostModel()
    assert m.split_token("done.")[1] >= 0.36
    assert m.split_token("waiting,")[1] >= 0.17
    assert m.split_token("plain")[1] == 0.0
    assert m.split_token("last\n")[1] >= 0.30


def test_cost_model_ignores_code_block_characters():
    """Code is shown but not spoken: its spoken-cost must stay tiny."""
    model = SpeechCostModel()
    letters = 0
    for token in ("```python\n", "x" * 64 + "\n", "```\n",
                  "Six spoken words here now.\n"):
        letters += model.split_token(token)[0]
    assert letters < 40, letters


def test_cost_model_reset_forgets_an_open_fence():
    """A new turn must not inherit the previous reply's open fence."""
    model = SpeechCostModel()
    model.split_token("```python\n")
    assert model._in_fence
    model.reset()
    assert not model._in_fence
    assert model.split_token("Fresh prose.\n")[0] == len("Fresh prose.")


def test_cost_model_plain_text_is_counted_in_full():
    model = SpeechCostModel()
    assert model.split_token("Hello ")[0] == len("Hello")
    assert model.split_token("world.")[0] == len("world.")


# ── sink + player integration ───────────────────────────────────────────────


def test_sink_filters_before_the_worker():
    from voice.speech_stream import StreamingSpeechService
    svc = StreamingSpeechService(cfg={}, realtime=True)
    captured = []
    svc._submit = lambda chunks: (captured.extend(chunks), len(chunks))[1]
    svc.feed("## Title\n**bold** words\n```\nsecret()\n```\ntail sentence.")
    svc.flush()
    joined = " ".join(captured)
    assert "Title" in joined and "bold" in joined and "tail sentence." in joined
    assert "#" not in joined and "*" not in joined and "secret" not in joined


def test_sink_speak_releases_the_whole_line():
    """A whole-line call is complete text: its held tail must speak too."""
    from voice.speech_stream import StreamingSpeechService
    svc = StreamingSpeechService(cfg={}, realtime=True)
    captured = []
    svc._submit = lambda chunks: (captured.extend(chunks), len(chunks))[1]
    svc.speak("## Heading\nA sentence with **bold** tail")
    joined = " ".join(captured)
    assert "Heading" in joined and "bold" in joined and "tail" in joined
    assert "#" not in joined and "*" not in joined


def test_sink_flag_off_passes_markdown():
    from voice.speech_stream import StreamingSpeechService
    svc = StreamingSpeechService(cfg={}, realtime=True)
    svc._md_filter_enabled = lambda: False
    captured = []
    svc._submit = lambda chunks: (captured.extend(chunks), len(chunks))[1]
    svc.feed("## Title kept\n")
    svc.flush()
    assert "## Title kept" in " ".join(captured)


def test_sink_interrupt_resets_filter_state():
    from voice.speech_stream import StreamingSpeechService
    svc = StreamingSpeechService(cfg={}, realtime=True)
    captured = []
    svc._submit = lambda chunks: (captured.extend(chunks), len(chunks))[1]
    svc.feed("```\nunfinished code")
    svc.interrupt(log=False)
    captured.clear()
    svc.feed("Fresh prose.")
    svc.flush()
    assert "Fresh prose." in " ".join(captured)


def test_player_gate_filters_direct_calls():
    from voice.tts import TTSPlayer

    class _FakeEngine:
        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

    eng = _FakeEngine()
    player = TTSPlayer(engine=eng, name="fake")
    player.speak("# Title\n```\ncode block\n```\ntail prose.")
    assert eng.spoken, "the sentence must still be spoken"
    spoken = " ".join(eng.spoken)
    assert "Title" in spoken and "tail prose." in spoken
    assert "code block" not in spoken and "#" not in spoken


def test_player_gate_respects_the_off_switch(monkeypatch):
    """The Settings flag disables the gate everywhere, including direct calls."""
    monkeypatch.setenv("JARVIS_FEATURE_SPEECH_MARKDOWN_FILTER", "0")
    from voice.tts import TTSPlayer

    class _FakeEngine:
        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

    eng = _FakeEngine()
    TTSPlayer(engine=eng, name="fake").speak("# Raw title")
    assert eng.spoken == ["# Raw title"]


def test_player_gate_is_a_noop_on_plain_text():
    from voice.tts import TTSPlayer

    class _FakeEngine:
        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

    eng = _FakeEngine()
    TTSPlayer(engine=eng, name="fake").speak("Plain sentence, nothing else.")
    assert eng.spoken == ["Plain sentence, nothing else."]
