"""Markdown → speakable text for the VOICE path only.

The chat keeps rendering the full markdown; the voice must speak prose.
This module sits between the reply text and the speech sink
(``voice/speech_stream.py``) so headings, bold markers, code fences, tables
and URLs are shown on screen but never read aloud::

    "## Example\\n**bold** and `code`"   →   "Example"
                                              "bold and code"

Three collaborators:

* ``MarkdownSpeechFilter`` — streaming transformer. Reply text arrives in
  arbitrary deltas; the filter holds back at most a few characters of
  partially-typed syntax (a half-arrived fence, a trailing ``**``) so broken
  markdown can never leak into speech. ``feed()`` returns speakable text,
  ``flush()`` releases the tail at the end of a turn, ``reset()`` starts
  fresh.
* ``filter_markdown_for_speech(text)`` — one-shot entry for callers holding a
  whole line already (direct player calls). **Idempotent**: filtering
  filtered text changes nothing, so a second gate can never corrupt speech.
* ``SpeechCostModel`` — the typewriter's pacing oracle (``ui_typewriter.py``).
  The same line/fence state machine answers "is this word spoken, and how
  many spoken letters does it have", so the word-by-word reveal follows the
  voice even when the reply contains code blocks that are never spoken.

Decisions (Settings ▸ behaviour, ``features.speech_markdown_filter``):

* Fenced code blocks and table rows are **skipped silently** — what is on
  screen stays on screen.
* Inline code keeps its content: "use ``pip install``" → "use pip install".
* Links keep their anchor text; images and bare URLs are dropped.
* The filter is line-oriented with a bounded holdback, so it delays emission
  by characters, never by a sentence.

stdlib only, no I/O, safe to import from anywhere.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# ── inline transforms ───────────────────────────────────────────────────────
# Deliberately character-class based (not "match pairs"): a delta boundary can
# split `**bold**` anywhere, and stripping each half independently is what
# makes the streaming output identical to the one-shot output.

_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~|><])")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^>\s]+)>")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_BARE_URL = re.compile(r"(?i)(?:https?://|www\.)\S+")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_ENTITIES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
             ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "))
_STAR_RUN = re.compile(r"\*+")
_TILDE_RUN = re.compile(r"~+")
_DASH_RUN = re.compile(r"-{2,}")           # thematic "---" only; "well-known" survives
_BOUNDARY_UNDER = re.compile(r"(?<![0-9A-Za-z])_+|_+(?![0-9A-Za-z])")
_SPACES = re.compile(r"[ \t]{2,}")

# ── block patterns ──────────────────────────────────────────────────────────

_FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_FENCE_PARTIAL = re.compile(r"^\s{0,3}[`~]{1,2}\s*$")
_HR = re.compile(r"^\s{0,3}(?:[-*_][ \t]*){3,}$")
#: Line prefixes that are still undecided ("##", "-", "12)") — held until the
#: line resolves, so a marker split across deltas can never leak as text.
_HEADING_PARTIAL = re.compile(r"^\s{0,3}#{1,6}\s*$")
_LIST_PARTIAL = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s*$")
_DIGIT_PARTIAL = re.compile(r"^\s*\d{1,3}$")
_QUOTE = re.compile(r"^\s{0,3}(?:>\s?)+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_HEADING_TAIL = re.compile(r"\s*#+\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
#: A contiguous run of could-still-combine characters at the very end of the
#: current partial line — held back (max 8) so `**bol|d**` never emits stray
#: markers and a partial table/fence resolves as a whole.
_RISKY_RUN = re.compile(r"[`*_~\[(\]<\\|]+$")

#: A bare URL still growing at the end of the buffer (ends at whitespace).
_URL_TAIL = re.compile(r"(?i)(?:https?://|www\.)\S*$")

_HOLD_MAX = 8
#: Upper bound for holding an inline construct (`[text](url)`, autolink)
#: until it completes — beyond this the raw text is emitted instead of
#: stalling speech on a runaway bracket.
_SPAN_HOLD_MAX = 512


def _inline_span_hold(t: str) -> int:
    """How many trailing chars to hold for a still-arriving inline construct.

    A link / autolink / bare URL split across deltas must be filtered as a
    whole — `[text](url)` is not a contiguous run of risky characters, so the
    plain risky-run hold cannot see it.
    """
    idx = -1
    lb = t.rfind("[")
    # "]" at the very end still counts as unclosed: the next delta may turn
    # the bracket pair into a link (`[text` + `](url)`).
    if lb != -1 and (t.rfind("]") < lb or t.endswith("]")):
        idx = max(idx, lb)
    lt = t.rfind("<")
    if lt != -1 and t.rfind(">") < lt:
        idx = max(idx, lt)
    rb = t.rfind("](")
    if rb != -1 and t.rfind(")", rb) == -1:
        # hold from the "[" of the same link, not just the "]" — everything
        # of `[text](` must be filtered as one unit when the ")" arrives
        lb2 = t.rfind("[", 0, rb)
        idx = max(idx, lb2 if lb2 != -1 else rb)
    um = _URL_TAIL.search(t)
    # A *bare* URL (prose text) is whitespace-preceded; a URL inside a
    # completed `[text](url)` or `<url>` is handled (or already fully
    # filtered) by the branches above / the inline pass.
    if um and (um.start() == 0 or t[um.start() - 1].isspace()):
        idx = max(idx, um.start())
    if idx < 0:
        return 0
    # Extend left across immediately adjacent bracket groups ("[ref][1]"):
    # the pair before the pending one is part of the same construct.
    while idx > 0 and t.rfind("]", 0, idx) == idx - 1:
        lb3 = t.rfind("[", 0, idx - 1)
        if lb3 == -1:
            break
        idx = lb3
    hold = len(t) - idx
    return hold if hold <= _SPAN_HOLD_MAX else 0


#: Marks word-internal underscore runs while the seam context is patched in
#: (a fragment edge can't see the character on the other side otherwise).
_UNDER_SENTINEL = "\x00"


def _inline_ctx(text: str, prev_ch: str = "") -> str:
    """``_inline`` with one character of left context.

    A fragment that starts with a run of underscores needs the previous
    emitted character to know whether the run is word-internal (`x_y_z` must
    keep its underscores, `` __bold__ `` must lose them). The run is protected
    with a sentinel, the regular inline pass runs, and the sentinel restores.
    """
    if text.startswith("_") and prev_ch and prev_ch.isalnum():
        lead = len(text) - len(text.lstrip("_"))
        if 0 < lead < len(text):
            text = _UNDER_SENTINEL * lead + text[lead:]
            return _inline(text).replace(_UNDER_SENTINEL, "_")
    return _inline(text)


def _inline(text: str) -> str:
    """Strip markdown syntax from one fragment, keeping the spoken words."""
    t = _ESCAPE.sub(r"\1", text)
    t = _IMAGE.sub("", t)
    t = _AUTOLINK.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _REF_LINK.sub(r"\1", t)
    # Leftover brackets (unmatched pairs, reference clutter) are never spoken.
    # Making them a fixed point keeps one-shot and streamed output identical
    # even when a `[ref][1]` pair is split across deltas.
    t = t.replace("[", "").replace("]", "")
    t = _BARE_URL.sub("", t)
    t = _HTML_TAG.sub("", t)
    for src, dst in _ENTITIES:
        t = t.replace(src, dst)
    t = t.replace("`", "")
    t = _STAR_RUN.sub("", t)
    t = _TILDE_RUN.sub("", t)
    t = _DASH_RUN.sub("", t)
    t = _BOUNDARY_UNDER.sub("", t)
    t = _SPACES.sub(" ", t)
    return t


def _starts_table(line: str) -> bool:
    """True for GFM table rows: `| a | b |` (the emitted form in practice)."""
    return line.lstrip().startswith("|") and line.count("|") >= 2


class MarkdownSpeechFilter:
    """Streaming markdown → speech transformer (see module docstring)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buf = ""              # unconsumed text (partial line + held run)
        self._in_fence = False
        self._fence_char = ""
        self._fence_len = 0
        self._skip_line = False     # the current line is dropped (table row…)
        self._line_started = False  # part of the current line was already emitted
        self._prev_out = "\n"       # last character put into the output

    # ── public API ──────────────────────────────────────────────────────

    def feed(self, text) -> str:
        """Consume a reply delta; return the speakable text it completed."""
        try:
            self._buf += str(text or "")
        except Exception:
            return ""
        out: List[str] = []
        self._drain(final=False, out=out)
        return "".join(out)

    def flush(self) -> str:
        """End of turn: release the held tail, then start a fresh state."""
        out: List[str] = []
        try:
            self._drain(final=True, out=out)
        except Exception:
            pass
        self.reset()
        return "".join(out)

    # ── draining ────────────────────────────────────────────────────────

    def _drain(self, final: bool, out: List[str]) -> None:
        # 1) Complete lines first — they resolve every block decision.
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            if self._skip_line:
                self._skip_line = False
                out.append("\n")
                continue
            if self._in_fence:
                self._consume_fence_line(line)
                out.append("\n")
                continue
            spoken = self._full_line(line)
            out.append(spoken)
            out.append("\n")
            self._line_started = False
            self._prev_out = "\n"

        # 2) The partial tail.
        t = self._buf
        if self._skip_line:
            self._buf = ""
            return
        if self._in_fence:
            if final:
                self._buf = ""       # a truncated code block is still code
            return
        if not t:
            return
        if _FENCE_LINE.match(t):
            if final:
                self._enter_fence(t)  # opener with no content before EOF
                self._buf = ""
            return                    # otherwise wait for the newline
        if _FENCE_PARTIAL.match(t):
            return
        if _HEADING_PARTIAL.match(t) or _LIST_PARTIAL.match(t) or _DIGIT_PARTIAL.match(t):
            return
        if t.lstrip().startswith("|"):
            # A line that starts with a pipe is (almost certainly) a table
            # row. Decide only when the line is complete; nothing may leak
            # from a row that turns out to be skipped. (A stray pipe that is
            # NOT a table resolves at the newline and speaks in full.)
            if final and not _starts_table(t):
                pre, self._buf = t, ""
                emit = self._partial_line(pre)
                if emit:
                    out.append(emit)
                    self._line_started = True
                    self._prev_out = emit[-1]
            elif final:
                self._buf = ""
            return
        if _HR.match(t):
            if final:
                self._buf = ""
            return                    # "---" could still become "--- text"
        if final:
            pre, self._buf = t, ""
        else:
            m = _RISKY_RUN.search(t)
            hold = min(len(m.group(0)) if m else 0, _HOLD_MAX)
            hold = max(hold, _inline_span_hold(t))
            cut = len(t) - hold
            if cut <= 0:
                return
            pre, self._buf = t[:cut], t[cut:]
        emit = self._partial_line(pre)
        if emit:
            out.append(emit)
            self._line_started = True
            self._prev_out = emit[-1]

    # ── line machinery ──────────────────────────────────────────────────

    def _full_line(self, line: str) -> str:
        """A complete (newline-terminated) source line → spoken text."""
        if _FENCE_LINE.match(line):
            self._enter_fence(line)
            return ""
        if _starts_table(line):
            return ""
        if not self._line_started:
            if _HR.match(line):
                return ""
            line = _QUOTE.sub("", line)
            m = _HEADING.match(line)
            if m:
                line = _HEADING_TAIL.sub("", m.group(1))
            else:
                m = _BULLET.match(line)
                if m:
                    line = line[m.end():]
        return _inline_ctx(line, self._prev_out)

    def _partial_line(self, pre: str) -> str:
        """A partial line prefix: line-start rules once, inline always."""
        if self._line_started:
            return _inline_ctx(pre, self._prev_out)
        line = _QUOTE.sub("", pre)
        m = _HEADING.match(line)
        if m:
            line = m.group(1)
            if not self._buf:
                # the fragment IS the whole line so far: trailing #s are the
                # markdown closing markers, exactly as in a complete line
                line = _HEADING_TAIL.sub("", line)
        else:
            m = _BULLET.match(line)
            if m:
                line = line[m.end():]
        return _inline_ctx(line, self._prev_out)

    def _enter_fence(self, line: str) -> None:
        m = _FENCE_LINE.match(line)
        if not m:
            return
        self._in_fence = True
        self._fence_char = m.group(1)[0]
        self._fence_len = len(m.group(1))

    def _consume_fence_line(self, line: str) -> None:
        m = _FENCE_LINE.match(line)
        if m and m.group(1)[0] == self._fence_char and len(m.group(1)) >= self._fence_len:
            self._in_fence = False
            self._fence_char = ""
            self._fence_len = 0


def filter_markdown_for_speech(text) -> str:
    """One-shot markdown → speech. Idempotent (a second pass is a no-op)."""
    f = MarkdownSpeechFilter()
    try:
        return f.feed(text) + f.flush()
    except Exception:
        return str(text or "")


def markdown_speech_enabled() -> bool:
    """``features.speech_markdown_filter`` — default ON.

    Reads :mod:`config.settings` (mtime-cached) so a Settings toggle applies
    to the next turn without restarting J.A.R.V.I.S.
    """
    try:
        from config.settings import get_feature
        return bool(get_feature("speech_markdown_filter", True))
    except Exception:
        return True


# ── typewriter pacing oracle ────────────────────────────────────────────────

class SpeechCostModel:
    """Word-level cost model for ``ui_typewriter.WordPacer``.

    ``split_token`` has the same contract as ``WordPacer._split``: given one
    whitespace-wrapped token it returns ``(spoken_letters, pause_seconds)``.
    The identical fence/table state machine marks code lines as *unspoken*:
    they reveal at a quick visible trickle instead of holding the voice-paced
    reveal back, and the learned speech rate counts spoken letters only.
    """

    #: Unspoken content (code, tables) reveals at this many letters per
    #: "letter" of cost — visible, but far faster than speech.
    TRICKLE_DIVISOR = 8

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._in_fence = False
        self._at_line_start = True

    def split_token(self, token: str) -> Tuple[int, float]:
        try:
            token = str(token or "")
            had_nl = "\n" in token
            # A token can carry the newline that STARTS its own line (when a
            # word follows it) or the one that ENDS the previous line.
            lead_nl = token.lstrip(" \t").startswith("\n")
            at_start = self._at_line_start or lead_nl
            self._at_line_start = had_nl
            word = token.strip()
            if not word:
                return 0, 0.0
            if at_start and re.match(r"[`~]{3,}", word):
                self._in_fence = not self._in_fence
                return 0, 0.0
            if self._in_fence or (at_start and _starts_table(word)):
                return max(1, len(word) // self.TRICKLE_DIVISOR), (0.10 if had_nl else 0.0)
            if at_start:
                # Line markers are never spoken: strip a heading/list/quote
                # marker before costing the first token of the line.
                word = re.sub(r"^(?:#{1,6}\s+|(?:[-*+]|\d{1,3}[.)])\s+|>\s*)",
                              "", word, count=1)
            spoken = _inline(word)
            letters = len(spoken)
            pause = 0.0
            if spoken.endswith((".", "!", "?", "\u2026")):
                pause += 0.36
            elif spoken.endswith((",", ";", ":", ")", "\u2014", "\u2013")):
                pause += 0.17
            if had_nl:
                pause += 0.30
            return letters, pause
        except Exception:
            return len(str(token or "").strip()), 0.0
