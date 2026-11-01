"""Which controller serves this request? One place that decides.

Every control tool J.A.R.V.I.S. owns is good at one thing, and the model has to
pick between them for a spoken request. Left to itself it guesses — that is how
"play music" ended up in an internal web page on a machine with Spotify
installed, and how "open YouTube" can end up in a raw browser when the HUD has a
perfectly good panel for it.

This module is the decision, made once and testable:

* :func:`classify` reads a request and says what KIND of work it is;
* :func:`match_rule` matches a **deterministic rule** — a fixed sentence pattern
  that maps to one exact tool and action (``minimize all windows`` →
  ``window_control.minimize_all``). Rules exist because a local classifier can
  and does misroute commands it has never seen ("set chrome priority high" →
  CODE), and a spoken command with one obvious mechanical meaning must never be
  at the mercy of a 322M-parameter guess;
* :func:`plan` turns an intent into an ordered list of concrete controllers to
  try, grounded in what is really installed, really running and really switched
  on;
* :func:`quick` is the rules-only fast path (no probes, no model);
* :func:`smart` is the fusion: deterministic rule first, then the local Laya
  classifier, then the built-in vocabulary — and it always says which of the
  three decided;
* :func:`report` renders a plan as text for the model.

It executes **nothing** and imports no controller at module level: it asks the
existing probes (``capabilities/media.py``, ``capabilities/router.py``) for the
facts, and answers with reasons. The model stays the one that acts, and every
handler that follows a plan is following an order it can also explain.

Facts are injected for tests (``plan(..., facts={...})``), which is what makes
the policy measurable without a Spotify install, a Playwright browser, or a UI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

#: The kinds of work a request can be. ``chat`` means no controller at all.
INTENTS = ("media", "app", "web", "screen", "research",
           "window", "system", "file", "clipboard", "document", "chat")

# ── classification vocabulary (the fallback when no rule matches) ────────────

_MEDIA_WORDS = (
    "music", "song", "songs", "track", "tracklist", "playlist", "album", "artist",
    "spotify", "vlc", "itunes", "media player", "pause", "resume", "unpause",
    "skip", "next song", "previous song", "volume", "mute", "unmute", "audio",
)
_WEB_WORDS = (
    "website", "web page", "webpage", "url", "browser", "in chrome", "in edge",
    "in firefox", "http://", "https://", "www.", ".com", ".org", ".net", ".io",
    "google for", "search the web", "search online", "in the browser",
)
_SCREEN_WORDS = (
    "click", "double click", "double-click", "right click", "right-click", "tap ",
    "type into", "type in", "press the", "press enter", "hotkey", "the button",
    "this button", "that icon", "on my screen", "on the screen", "drag ", "scroll",
    "screenshot", "screen capture", "what is on my screen",
)
_RESEARCH_WORDS = (
    "research", "look up", "look into", "find information", "find out about",
    "gather information", "compare prices", "find sources", "investigate",
)
_APP_WORDS = (
    "open ", "launch ", "start the ", "close ", "quit ", "focus ", "bring up ",
    "switch to ",
)

#: Nouns that make a "find/delete/copy …" request a FILESYSTEM request rather
#: than a conversation about files. The rule guard uses this list: "find the
#: invoice from March" is a file search, "find information about laptops" is
#: research, and only the noun can tell them apart.
_FILE_NOUNS = (
    "file", "files", "folder", "folders", "document", "documents", "directory",
    "pdf", "docx", "xlsx", "pptx", "txt", "csv", "zip", "archive", "invoice",
    "receipt", "photo", "picture", "screenshot", "download", "downloads",
    "desktop", "documents", "pictures", "one drive", "onedrive",
)

#: Site names a spoken "open X in the browser" resolves to without a lookup.
#: Only first-party destinations J.A.R.V.I.S. already talks about; anything
#: else is left to the model, because inventing a URL is a real mistake.
_SITE_URLS = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "maps": "https://www.google.com/maps",
    "google maps": "https://www.google.com/maps",
    "wikipedia": "https://www.wikipedia.org",
    "chatgpt": "https://chatgpt.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
}

_PRIORITY_LEVELS = {
    "realtime": "realtime", "real time": "realtime",
    "high": "high",
    "above normal": "above_normal", "above_normal": "above_normal",
    "normal": "normal",
    "below normal": "below_normal", "below_normal": "below_normal",
    "low": "low", "idle": "idle",
}


def _tokens(request: str) -> str:
    return " ".join(str(request or "").lower().split())


def _clean(value: str) -> str:
    """One captured fragment, without the punctuation speech adds."""
    text = str(value or "").strip().strip("\"'")
    return text.strip(" .,;:!?").strip()


def _score(text: str, words: tuple[str, ...], weight: int = 2) -> int:
    return sum(weight for word in words if word in text)


def _norm(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _fileish(text: str) -> bool:
    """True when the words name real files/paths rather than a topic."""
    low = _tokens(text)
    if any(noun in low for noun in _FILE_NOUNS):
        return True
    if any(marker in low for marker in ("\\", "/", "~")):
        return True
    return bool(re.search(r"\.[a-z0-9]{2,5}\b", low))


def _named_application(text: str) -> bool:
    """True when a known application name appears in the request."""
    try:
        from capabilities import desktop
        aliases = desktop.APP_ALIASES
    except Exception:
        return False
    return any(_norm(alias) and _norm(alias) in text for alias in aliases)


def _site_url(text: str) -> str:
    """The URL for a named first-party site in the request (or "")."""
    low = _tokens(text)
    for name in sorted(_SITE_URLS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return _SITE_URLS[name]
    return ""


def _first_url(request: str) -> str:
    for token in str(request or "").split():
        if token.startswith(("http://", "https://")):
            return token.strip(".,;)\"'")
    return ""


# ── the deterministic rule table ──────────────────────────────────────────────

def _no_args(_match, _request: str) -> dict:
    return {}


@dataclass(frozen=True)
class Rule:
    """One fixed sentence pattern → one exact tool action.

    A rule never executes anything and never extracts a path from prose: it
    names the intent, the tool and the action, and carries only the arguments it
    literally read (a quoted name, a percentage, a priority level). Everything
    else is left to the model that follows the plan.
    """

    name: str
    intent: str
    tool: str
    action: str
    pattern: re.Pattern
    why: str
    args: Callable = _no_args
    #: Extra check on the whole request, for rules whose pattern is not enough
    #: (e.g. "delete X" is only a file rule when X actually names a file).
    guard: Callable[[str, re.Match], bool] = field(
        default=lambda _text, _m: True)
    confidence: float = 0.95


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


#: Every rule, most specific first. Order is the policy: a request is served by
#: the FIRST rule it matches, so clipboard-before-file, file-open-before-app and
#: window-before-app are deliberate, not incidental.
_RULES: tuple[Rule, ...] = (
    # ── clipboard (must beat file copy/move: "copy X to the clipboard") ──────
    Rule("clipboard.write", "clipboard", "clipboard_sentry", "write",
         _rx(r"^(?:copy|put|write|add)\s+(?P<text>.+?)\s+(?:to|onto|into)\s+"
             r"(?:the\s+)?clipboard\s*$"),
         "the clipboard is the named destination",
         lambda m, r: {"action": "write", "text": _clean(m.group("text"))}),
    Rule("clipboard.read", "clipboard", "clipboard_sentry", "read",
         _rx(r"^(?:(?:read|show|open)\s+(?:the\s+)?clipboard|"
             r"what(?:'s| is)\s+(?:on|in)\s+(?:the\s+)?clipboard|"
             r"what\s+did\s+i\s+(?:just\s+)?copy)\s*\??$"),
         "the request asks what is on the clipboard",
         lambda m, r: {"action": "read"}),
    Rule("clipboard.clear", "clipboard", "clipboard_sentry", "clear",
         _rx(r"^(?:clear|empty|wipe)\s+(?:the\s+)?clipboard\s*$"),
         "clearing the clipboard is one exact action",
         lambda m, r: {"action": "clear"}),
    Rule("clipboard.translate", "clipboard", "clipboard_sentry", "translate",
         _rx(r"^translate\s+(?:the\s+)?clipboard(?:\s+(?:to|into)\s+"
             r"(?P<language>.+?))?\s*$"),
         "translating the clipboard is one exact action",
         lambda m, r: {"action": "translate",
                       **({"language": _clean(m.group("language"))}
                          if m.group("language") else {})}),
    Rule("clipboard.analyze", "clipboard", "clipboard_sentry", "analyze",
         _rx(r"^(?:analy[sz]e|summari[sz]e|explain)\s+(?:the\s+)?clipboard\s*$"),
         "the clipboard is the document being analysed",
         lambda m, r: {"action": "analyze"}),
    Rule("clipboard.fix", "clipboard", "clipboard_sentry", "fix",
         _rx(r"^(?:fix|proofread|correct)\s+(?:the\s+)?clipboard\s*$"),
         "the clipboard text is the thing to repair",
         lambda m, r: {"action": "fix"}),
    Rule("clipboard.format", "clipboard", "clipboard_sentry", "format",
         _rx(r"^format\s+(?:the\s+)?clipboard\s*$"),
         "the clipboard text is the thing to format",
         lambda m, r: {"action": "format"}),

    # ── documents (before file: "create a word document" is not a file search) ─
    Rule("document.docx", "document", "documents", "create_docx",
         _rx(r"^(?:create|make|write|generate|draft)\s+(?:me\s+)?(?:a\s+|an\s+)?"
             r"(?:new\s+)?(?:word\s+)?(?:document|docx|report|letter|resume|cv)\b"),
         "the request asks for a Word document deliverable",
         lambda m, r: {"action": "create_docx"}),
    Rule("document.xlsx", "document", "documents", "create_xlsx",
         _rx(r"^(?:create|make|write|generate)\s+(?:me\s+)?(?:a\s+|an\s+)?"
             r"(?:new\s+)?(?:excel\s+|spreadsheet|workbook|xlsx)\b"),
         "the request asks for an Excel workbook deliverable",
         lambda m, r: {"action": "create_xlsx"}),
    Rule("document.pptx", "document", "documents", "create_pptx",
         _rx(r"^(?:create|make|write|generate)\s+(?:me\s+)?(?:a\s+|an\s+)?"
             r"(?:new\s+)?(?:powerpoint|presentation|deck|slides|pptx)\b"),
         "the request asks for a PowerPoint deck deliverable",
         lambda m, r: {"action": "create_pptx"}),
    Rule("document.pdf_merge", "document", "documents", "pdf_merge",
         _rx(r"^(?:merge|combine|join)\s+(?:these\s+|the\s+)?pdfs?\b"),
         "merging PDFs is one exact action",
         lambda m, r: {"action": "pdf_merge"}),
    Rule("document.pdf_extract", "document", "documents", "pdf_extract",
         _rx(r"^extract\s+pages?\b.*\bpdf\b"),
         "extracting PDF pages is one exact action",
         lambda m, r: {"action": "pdf_extract"}),
    Rule("document.pdf", "document", "documents", "pdf_from_text",
         _rx(r"^(?:create|make|generate)\s+(?:me\s+)?(?:a\s+|an\s+)?"
             r"(?:new\s+)?pdf\b"),
         "the request asks for a PDF deliverable",
         lambda m, r: {"action": "pdf_from_text"}),
    Rule("document.list", "document", "documents", "list_documents",
         _rx(r"^(?:list|show)\s+(?:my\s+)?(?:documents|deliverables)\b"),
         "the request asks which documents J.A.R.V.I.S. has written",
         lambda m, r: {"action": "list_documents"}),

    # ── files (deterministic; paths are passed through, never invented) ──────
    Rule("file.open", "file", "file_control", "open",
         _rx(r"^open\s+(?P<path>.+?\.(?:pdf|docx?|xlsx?|pptx?|txt|csv|md|json|"
             r"png|jpe?g|gif|bmp|mp3|mp4|wav|zip))\s*$"),
         "the request opens a named document, not an application",
         lambda m, r: {"action": "open", "path": _clean(m.group("path"))}),
    Rule("file.open_folder", "file", "file_control", "open",
         _rx(r"^open\s+(?:the\s+)?(?P<path>.+?)\s+(?:folder|directory)\s*$"),
         "the request opens a folder in Explorer",
         lambda m, r: {"action": "open", "path": _clean(m.group("path"))}),
    Rule("file.delete", "file", "file_control", "delete",
         _rx(r"^(?:delete|remove|trash)\s+(?P<path>.+)$"),
         "the request deletes a named file or folder (Recycle Bin only)",
         lambda m, r: {"action": "delete", "path": _clean(m.group("path"))},
         guard=lambda text, m: _fileish(text) and "clipboard" not in text),
    Rule("file.copy", "file", "file_control", "copy",
         _rx(r"^copy\s+(?P<src>.+?)\s+(?:to|into)\s+(?P<dst>.+)$"),
         "the request copies a file or folder to a destination",
         lambda m, r: {"action": "copy", "path": _clean(m.group("src")),
                       "destination": _clean(m.group("dst"))},
         guard=lambda text, m: _fileish(text)),
    Rule("file.move", "file", "file_control", "move",
         _rx(r"^(?:move|cut)\s+(?P<src>.+?)\s+(?:to|into)\s+(?P<dst>.+)$"),
         "the request moves a file or folder to a destination",
         lambda m, r: {"action": "move", "path": _clean(m.group("src")),
                       "destination": _clean(m.group("dst"))},
         guard=lambda text, m: _fileish(text)),
    Rule("file.rename", "file", "file_control", "rename",
         _rx(r"^rename\s+(?P<src>.+?)\s+(?:to|as)\s+(?P<dst>.+)$"),
         "the request renames a named file or folder",
         lambda m, r: {"action": "rename", "path": _clean(m.group("src")),
                       "destination": _clean(m.group("dst"))}),
    Rule("file.zip", "file", "file_control", "zip",
         _rx(r"^(?:zip|compress|archive)\s+(?P<paths>.+)$"),
         "the request creates a zip archive",
         lambda m, r: {"action": "zip", "paths": _clean(m.group("paths"))},
         guard=lambda text, m: _fileish(text)),
    Rule("file.unzip", "file", "file_control", "unzip",
         _rx(r"^(?:unzip|extract)\s+(?P<path>.+)$"),
         "the request extracts a zip archive",
         lambda m, r: {"action": "unzip", "path": _clean(m.group("path"))},
         guard=lambda text, m: ("zip" in text or "archive" in text)),
    Rule("file.find", "file", "file_control", "find",
         _rx(r"^(?:find|locate|search\s+for|where\s+is|where\s+are)\s+"
             r"(?P<query>.+)$"),
         "the request looks for a named file or folder on this PC",
         lambda m, r: {"action": "find", "query": _clean(m.group("query"))},
         guard=lambda text, m: _fileish(text)),
    Rule("file.list", "file", "file_control", "list",
         _rx(r"^(?:list|show)\s+(?:the\s+)?(?:files|folders|contents|items)\b"
             r"(?:\s+(?:in|of)\s+(?P<path>.+))?\s*$"),
         "the request lists a folder's real contents",
         lambda m, r: {"action": "list",
                       **({"path": _clean(m.group("path"))}
                          if m.group("path") else {})}),
    Rule("file.list_whats_in", "file", "file_control", "list",
         _rx(r"^what(?:'s| is| are)\s+in\s+(?P<path>.+?)(?:\s+folder)?\s*\??$"),
         "the request asks what a folder contains",
         lambda m, r: {"action": "list", "path": _clean(m.group("path"))}),
    Rule("file.info", "file", "file_control", "info",
         _rx(r"^(?:file\s+)?info\s+(?:about|on|for)\s+(?P<path>.+)$"),
         "the request asks for one item's real metadata",
         lambda m, r: {"action": "info", "path": _clean(m.group("path"))}),

    # ── windows (deterministic arrangement, no screen reading needed) ────────
    Rule("window.minimize_all", "window", "window_control", "minimize_all",
         _rx(r"\bminimi[sz]e\s+(?:all|every)\s+(?:of\s+the\s+|the\s+)?windows?\b"),
         "'all windows' is a window-arrangement command, never a screen click",
         lambda m, r: {"action": "minimize_all"}),
    Rule("window.arrange_all", "window", "window_control", "arrange_all",
         _rx(r"^(?:tile|arrange)\s+(?:all|every)\s+(?:of\s+the\s+|the\s+)?"
             r"windows?\b"),
         "arranging every window is one exact Win32 action",
         lambda m, r: {"action": "arrange_all"}),
    Rule("window.list", "window", "window_control", "list_windows",
         _rx(r"^(?:list|show)\s+(?:the\s+)?(?:open\s+)?windows\b"),
         "the request asks which top-level windows are open",
         lambda m, r: {"action": "list_windows"}),
    Rule("window.monitors", "window", "window_control", "list_monitors",
         _rx(r"^(?:list|show)\s+(?:the\s+)?(?:displays|monitors)\b"),
         "the request asks about the connected displays",
         lambda m, r: {"action": "list_monitors"}),
    Rule("window.move_monitor", "window", "window_control", "move_to_monitor",
         _rx(r"^move\s+(?P<app>.+?)\s+to\s+(?:display|monitor)\s+"
             r"(?P<monitor>\d+)\s*$"),
         "moving a window to a numbered display is one exact action",
         lambda m, r: {"action": "move_to_monitor", "app": _clean(m.group("app")),
                       "monitor": int(m.group("monitor"))}),
    Rule("window.snap", "window", "window_control", "snap_grid",
         _rx(r"^snap\s+(?P<app>.+?)\s+(?:to\s+)?(?:the\s+)?"
             r"(?P<position>top[- ]left|top[- ]right|bottom[- ]left|"
             r"bottom[- ]right|left|right|top|bottom)"
             r"(?:\s+(?:corner|quadrant|half))?\s*$"),
         "snapping a window into a named position is one exact action",
         lambda m, r: {"action": "snap_grid", "app": _clean(m.group("app")),
                       "position": _clean(m.group("position")).replace(" ", "-")}),
    Rule("window.tile_left", "window", "window_control", "tile_left",
         _rx(r"^(?:tile|snap)\s+(?P<app>.+?)\s+(?:to\s+)?(?:the\s+)?left"
             r"(?:\s+(?:half|side))?\s*$"),
         "tiling a window left is one exact Win32 action",
         lambda m, r: {"action": "tile_left", "app": _clean(m.group("app"))}),
    Rule("window.tile_right", "window", "window_control", "tile_right",
         _rx(r"^(?:tile|snap)\s+(?P<app>.+?)\s+(?:to\s+)?(?:the\s+)?right"
             r"(?:\s+(?:half|side))?\s*$"),
         "tiling a window right is one exact Win32 action",
         lambda m, r: {"action": "tile_right", "app": _clean(m.group("app"))}),
    Rule("window.minimize", "window", "window_control", "minimize",
         _rx(r"^minimi[sz]e\s+(?P<app>.+)$"),
         "minimising a named window is one exact Win32 action",
         lambda m, r: {"action": "minimize", "app": _clean(m.group("app"))}),
    Rule("window.maximize", "window", "window_control", "maximize",
         _rx(r"^maximi[sz]e\s+(?P<app>.+)$"),
         "maximising a named window is one exact Win32 action",
         lambda m, r: {"action": "maximize", "app": _clean(m.group("app"))}),
    Rule("window.restore", "window", "window_control", "restore",
         _rx(r"^(?:restore|unminimi[sz]e)\s+(?P<app>.+)$"),
         "restoring a named window is one exact Win32 action",
         lambda m, r: {"action": "restore", "app": _clean(m.group("app"))}),

    # ── screen reading (real OCR / UI Automation, before visual guessing) ────
    Rule("screen.screenshot", "screen", "pc_control", "screen_screenshot",
         _rx(r"^(?:take|capture|grab)\s+(?:a\s+)?(?:screenshot|screen\s*capture|"
             r"screen\s*shot)\b"),
         "a screenshot is one exact capture, not a visual-control task",
         lambda m, r: {"action": "screen_screenshot"}),
    Rule("screen.read", "screen", "pc_control", "read_screen",
         _rx(r"^(?:(?:read|scan)\s+(?:the\s+|my\s+)?screen|"
             r"what(?:'s| is)\s+(?:on|on)\s+(?:my|the)\s+screen)\s*\??$"),
         "reading the screen is real OCR, not a description by a model",
         lambda m, r: {"action": "read_screen"}),
    Rule("screen.find", "screen", "pc_control", "find_text",
         _rx(r"^find\s+(?P<query>.+?)\s+on\s+(?:the\s+|my\s+)?screen$"),
         "finding a word on screen returns its exact real position",
         lambda m, r: {"action": "find_text", "query": _clean(m.group("query"))}),
    Rule("screen.click_text", "screen", "pc_control", "click_text",
         _rx(r"^click\s+(?P<query>.+?)\s+on\s+(?:the\s+|my\s+)?screen$"),
         "clicking recognised screen text uses its exact real position",
         lambda m, r: {"action": "click_text", "query": _clean(m.group("query"))}),
    Rule("screen.wait", "screen", "pc_control", "wait_for_text",
         _rx(r"^wait\s+(?:for|until)\s+(?P<query>.+?)(?:\s+to\s+"
             r"(?:appear|show\s+up))?\s+on\s+(?:the\s+|my\s+)?screen$"),
         "waiting for screen text is a real OCR poll, not a guess",
         lambda m, r: {"action": "wait_for_text", "query": _clean(m.group("query"))}),
    Rule("screen.ui_tree", "screen", "pc_control", "ui_tree",
         _rx(r"^(?:list|show)\s+(?:the\s+)?(?:ui\s+)?controls?\s+(?:of|in)\s+"
             r"(?P<title>.+)$"),
         "listing a window's controls reads the real UI Automation tree",
         lambda m, r: {"action": "ui_tree", "title": _clean(m.group("title"))}),
    Rule("screen.ui_read", "screen", "pc_control", "ui_read",
         _rx(r"^read\s+(?:the\s+)?(?:control|field)\s+(?P<control>.+)$"),
         "reading a named control goes through UI Automation",
         lambda m, r: {"action": "ui_read", "control": _clean(m.group("control"))}),
    Rule("screen.ui_set", "screen", "pc_control", "ui_set_text",
         _rx(r"^(?:set|fill)\s+(?:the\s+)?(?P<control>.+?)\s+"
             r"(?:field|box|input)\s+(?:to|with)\s+(?P<text>.+)$"),
         "filling a named field goes through UI Automation and verifies the "
         "read-back",
         lambda m, r: {"action": "ui_set_text",
                       "control": _clean(m.group("control")),
                       "text": _clean(m.group("text"))}),

    # ── web (before app: a URL or "in the browser" is not an app launch) ─────
    Rule("web.url", "web", "browser_action", "browser_open",
         _rx(r"\bhttps?://\S+"),
         "the request names a URL, so the structured browser owns it",
         lambda m, r: {"action": "browser_open",
                       "url": _first_url(r) or _clean(m.group(0))}),
    Rule("web.browser", "web", "browser_action", "browser_open",
         _rx(r"\b(?:in|with|using|on)\s+(?:the\s+)?(?:chrome|edge|firefox|brave|"
             r"browser)\b"),
         "the request names a browser, so the structured browser owns it",
         lambda m, r: {"action": "browser_open",
                       "url": _first_url(r) or _site_url(r)}),

    # ── media (before system kill/resume: "stop the music" is playback) ──────
    Rule("media.play", "media", "play_music", "play",
         _rx(r"^play\b(?P<query>.*)$"),
         "'play …' is a request to start something playing",
         lambda m, r: {"query": _clean(m.group("query"))}),
    Rule("media.pause", "media", "play_music", "pause",
         _rx(r"^(?:pause|resume|unpause|stop)\s+(?:the\s+)?"
             r"(?:music|song|track|playback|audio)\s*$"),
         "the request controls what is playing right now",
         lambda m, r: {"action": "pause"}),
    Rule("media.next", "media", "play_music", "next",
         _rx(r"^(?:next|skip|previous)\s+(?:the\s+)?(?:song|track|one)\s*$"),
         "the request skips the current track",
         lambda m, r: {"action": "next"}),
    Rule("media.volume", "media", "power_control", "volume_up",
         _rx(r"^(?:turn\s+(?:the\s+)?)?volume\s+(?P<direction>up|down)\s*$"),
         "changing the system volume is a power-control action",
         lambda m, r: {"action": ("volume_up"
                                  if m.group("direction").lower() == "up"
                                  else "volume_down")}),

    # ── applications (only when a real application name is present) ──────────
    Rule("app.open", "app", "pc_control", "open_application",
         _rx(r"^(?:open|launch|start|run|bring\s+up|switch\s+to|focus)\s+"
             r"(?:the\s+)?(?:app(?:lication)?\s+)?(?P<app>.+)$"),
         "the request names an application to open or focus",
         lambda m, r: {"action": "open_application",
                       "app": _clean(m.group("app"))},
         guard=lambda text, m: _named_application(text)),
    Rule("app.close", "app", "pc_control", "close_application",
         _rx(r"^(?:close|quit|exit)\s+(?:the\s+)?(?:app(?:lication)?\s+)?"
             r"(?P<app>.+)$"),
         "the request names an application to close",
         lambda m, r: {"action": "close_application",
                       "app": _clean(m.group("app"))},
         guard=lambda text, m: _named_application(text)),

    # ── system control (processes, wifi, volume, brightness, metrics) ────────
    Rule("system.priority", "system", "system_control", "set_process_priority",
         _rx(r"^(?:set|change|put)\s+(?P<name>.+?)\s+(?:process\s+)?priority\s+"
             r"(?:to\s+)?(?P<level>realtime|real\s+time|high|above\s+normal|"
             r"normal|below\s+normal|low|idle)\s*$"),
         "a named process priority is one exact process-control action",
         lambda m, r: {"action": "set_process_priority",
                       "name": _clean(m.group("name")),
                       "level": _PRIORITY_LEVELS.get(
                           _norm(m.group("level")), _norm(m.group("level")))}),
    Rule("system.list_processes", "system", "system_control", "list_processes",
         _rx(r"^(?:list|show)\s+(?:the\s+)?(?:running\s+)?processes\b"),
         "the request asks which processes are running",
         lambda m, r: {"action": "list_processes"}),
    Rule("system.kill", "system", "system_control", "kill_process",
         _rx(r"^(?:kill|end|terminate|stop)\s+(?:the\s+)?(?:process\s+)?"
             r"(?P<name>.+)$"),
         "killing a named process is one exact process-control action "
         "(it still needs the on-screen confirmation)",
         lambda m, r: {"action": "kill_process", "name": _clean(m.group("name"))},
         guard=lambda text, m: (_named_application(text) or "process" in text)),
    Rule("system.suspend", "system", "system_control", "suspend_process",
         _rx(r"^suspend\s+(?:the\s+)?(?:process\s+)?(?P<name>.+)$"),
         "suspending a named process is one exact process-control action",
         lambda m, r: {"action": "suspend_process", "name": _clean(m.group("name"))},
         guard=lambda text, m: (_named_application(text) or "process" in text)),
    Rule("system.resume", "system", "system_control", "resume_process",
         _rx(r"^(?:resume|unsuspend)\s+(?:the\s+)?(?:process\s+)?(?P<name>.+)$"),
         "resuming a named process is one exact process-control action",
         lambda m, r: {"action": "resume_process", "name": _clean(m.group("name"))},
         guard=lambda text, m: (_named_application(text) or "process" in text)),
    Rule("system.wifi_status", "system", "system_control", "wifi_status",
         _rx(r"^(?:(?:wifi|wi-fi)\s+status|is\s+(?:the\s+)?(?:wifi|wi-fi)\s+"
             r"(?:on|connected))\s*\??$"),
         "the request asks for the live Wi-Fi state",
         lambda m, r: {"action": "wifi_status"}),
    Rule("system.wifi", "system", "system_control", "wifi_on",
         _rx(r"^(?:(?:turn\s+(?:the\s+)?)?(?:wifi|wi-fi)\s+(?P<state>on|off)|"
             r"(?P<verb>enable|disable)\s+(?:the\s+)?(?:wifi|wi-fi))\s*$"),
         "switching Wi-Fi is one exact system action",
         lambda m, r: {"action": ("wifi_on"
                                  if (m.group("state") or "").lower() == "on"
                                  or (m.group("verb") or "").lower() == "enable"
                                  else "wifi_off")}),
    Rule("system.volume", "system", "system_control", "set_volume",
         _rx(r"^set\s+(?:the\s+)?volume\s+(?:to\s+)?(?P<percent>\d{1,3})\s*%?$"),
         "setting an exact volume percentage is one exact system action",
         lambda m, r: {"action": "set_volume",
                       "percent": int(m.group("percent"))}),
    Rule("system.brightness", "system", "system_control", "set_brightness",
         _rx(r"^set\s+(?:the\s+)?brightness\s+(?:to\s+)?(?P<percent>\d{1,3})"
             r"\s*%?$"),
         "setting an exact brightness percentage is one exact system action",
         lambda m, r: {"action": "set_brightness",
                       "percent": int(m.group("percent"))}),
    Rule("system.theme", "system", "system_control", "set_theme",
         _rx(r"^(?:switch|set|change)\s+(?:to\s+)?(?:the\s+)?"
             r"(?P<theme>dark|light)\s+theme\s*$"),
         "switching the Windows theme is one exact system action",
         lambda m, r: {"action": "set_theme", "theme": _clean(m.group("theme"))}),
    Rule("system.metrics", "system", "system_control", "system_metrics",
         _rx(r"^(?:(?:system|pc)\s+(?:metrics|status|usage)|"
             r"what(?:'s| is)\s+using\s+(?:my\s+)?(?:cpu|memory|ram))\s*\??$"),
         "the request asks for live CPU/memory figures",
         lambda m, r: {"action": "system_metrics"}),

    # ── research (the internet, not the machine) ─────────────────────────────
    Rule("research", "research", "web_research", "",
         _rx(r"^(?:research|look\s+up|look\s+into|find\s+out\s+about|investigate|"
             r"compare\s+prices|gather\s+information)\b(?P<query>.*)$"),
         "the request asks for real-world information from the internet",
         lambda m, r: {"query": _clean(m.group("query")) or _clean(r)}),
)


def match_rule(request: str) -> tuple[Rule | None, re.Match | None]:
    """The first deterministic rule this request matches (or ``(None, None)``).

    Matching is done on a normalised copy of the request, so spoken casing and
    extra whitespace never change the route. The guard (when a rule has one)
    sees the same normalised text.
    """
    text = _tokens(request)
    if not text:
        return None, None
    for rule in _RULES:
        match = rule.pattern.search(text)
        if match is None:
            continue
        try:
            if not rule.guard(text, match):
                continue
        except Exception:
            continue
        return rule, match
    return None, None


def rule_names() -> tuple[str, ...]:
    """Every deterministic rule id, in policy order (for diagnostics/tests)."""
    return tuple(rule.name for rule in _RULES)


# ── classification ────────────────────────────────────────────────────────────

def classify(request: str) -> tuple[str, str]:
    """The intent behind one request, with the wording that decided it.

    Deterministic rules win first: a sentence with one mechanical meaning must
    not depend on a classifier's guess. Everything else falls through to the
    conversational vocabulary, which is deliberately conservative — anything
    that does not clearly name work returns ``chat``, because inventing
    automation for a conversational sentence is the expensive mistake.
    """
    text = _tokens(request)
    if not text:
        return ("chat", "there is no request to route")
    rule, _match = match_rule(request)
    if rule is not None:
        return (rule.intent, rule.why)
    # ── the vocabulary fallback (no rule matched) ─────────────────────────────
    scores = {
        "media": _score(text, _MEDIA_WORDS),
        "web": _score(text, _WEB_WORDS),
        "screen": _score(text, _SCREEN_WORDS, 1),
        "research": _score(text, _RESEARCH_WORDS, 2),
        "app": _score(text, _APP_WORDS, 1),
    }
    best = max(scores.values())
    if best <= 0:
        return ("chat", "nothing in the wording asks for the computer to be driven")
    # A named application is the strongest signal there is: "open Spotify" is an
    # app request even though it also contains a media word.
    if scores["app"] and _named_application(text):
        return ("app", "the request names an application to open or close")
    # Ties resolve towards the most specific controller, never towards a guess
    # that would type or click for a request that only mentioned a topic.
    for intent in ("screen", "media", "app", "web", "research"):
        if scores[intent] == best:
            return (intent, f"the wording matches the {intent} vocabulary")
    return ("chat", "no controller vocabulary matched")


# ── live facts ────────────────────────────────────────────────────────────────

#: Settings switches this layer reads. Each gates the matching candidate.
_SWITCHES = ("native_music_apps", "webview_engine", "webview_music",
             "desktop_control", "windows_automation", "physical_input",
             "playwright_browser", "uitars_visual", "power_control", "web_task",
             "webview", "screen_understanding", "window_control",
             "process_control", "system_settings", "clipboard_sentry",
             "file_control", "document_architect")


def _switch(name: str, default: bool = True) -> bool:
    try:
        from config.settings import get_feature
        return bool(get_feature(name, default))
    except Exception:
        return bool(default)


def facts() -> dict:
    """The real state of every controller this layer routes between.

    All probes are the controllers' own (they cache and never raise), so this
    is cheap enough to call before answering one question.
    """
    out: dict = {"switches": {name: _switch(name) for name in _SWITCHES}}
    try:
        from capabilities import media
        out["media"] = media.status()
    except Exception as exc:
        out["media"] = {"available": False, "detail": f"probe failed: {exc}"}
    try:
        from capabilities import router
        out["controllers"] = router.local_status()
    except Exception as exc:
        out["controllers"] = {"_error": f"probe failed: {exc}"}
    controllers = out.setdefault("controllers", {})
    for name, module_name in (("window_control", "windows_ext"),
                              ("file_control", "files"),
                              ("ocr", "ocr"),
                              ("uia", "uia"),
                              ("clipboard", "clipboard")):
        if name in controllers:
            continue
        try:
            import importlib
            module = importlib.import_module(f"capabilities.{module_name}")
            status = dict(module.status())
            status.setdefault("usable", status.get("available"))
            controllers[name] = status
        except Exception as exc:
            controllers[name] = {"usable": False,
                                 "detail": f"probe failed: {exc}"}
    try:
        from capabilities import agent_tars
        out["agent_tars"] = agent_tars.status()
    except Exception as exc:
        out["agent_tars"] = {"usable": False, "detail": f"probe failed: {exc}"}
    try:
        from capabilities import agent_reach
        out["agent_reach"] = agent_reach.status()
    except Exception as exc:
        out["agent_reach"] = {"available": False, "detail": f"probe failed: {exc}"}
    return out


#: Live facts for :func:`plan` — named so a caller-supplied ``facts`` argument
#: can never shadow the probe itself.
_live_facts = facts


def _usable(f: dict, name: str) -> bool:
    info = (f.get("controllers") or {}).get(name) or {}
    return bool(info.get("usable"))


def _controller_detail(f: dict, name: str) -> str:
    info = (f.get("controllers") or {}).get(name) or {}
    return str(info.get("detail") or "")


def _candidate(controller: str, tool: str, reason: str, ready: bool,
               args: dict | None = None) -> dict:
    return {"controller": controller, "tool": tool, "reason": reason,
            "ready": bool(ready), "args": dict(args or {})}


def _switch_reason(switches: dict, name: str, what: str) -> str:
    """One honest sentence for a switched-off controller."""
    return f"{what} is switched off in Settings"


# ── the chains ────────────────────────────────────────────────────────────────

def _media_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    media = f.get("media") or {}
    query = _play_query(request)
    chain: list[dict] = []
    # A bare volume command is not playback: "turn the volume up" must change
    # the system volume, not start a song.
    text = _tokens(request)
    volume = re.search(r"\bvolume\b|\bmute\b|\bunmute\b", text)
    if volume and not any(word in text for word in ("music", "song", "track",
                                                    "playback", "audio")):
        power_ready = (_usable(f, "power_control")
                       and switches.get("power_control", True)
                       and switches.get("desktop_control", True))
        if "up" in text:
            action, verb = "volume_up", "turning the volume up"
        elif "down" in text:
            action, verb = "volume_down", "turning the volume down"
        else:
            action, verb = "mute", "muting the system sound"
        chain.append(_candidate(
            "power", "power_control",
            (f"the power controller can run '{action}' directly"
             if power_ready else
             _controller_detail(f, "power_control")
             or _switch_reason(switches, "power_control", "Power control")),
            power_ready, {"action": action}))
    installed = media.get("installed") or {}
    if switches.get("native_music_apps", True) and media.get("available"):
        label = ", ".join(str(k).title() for k in installed) or "a media application"
        chain.append(_candidate(
            "media", "play_music",
            f"an installed player was found ({label}) and native music apps are on",
            True, {"query": query}))
    elif not switches.get("native_music_apps", True):
        chain.append(_candidate(
            "media", "play_music",
            "native music apps are switched off in Settings, so the installed "
            "player is not used", False, {"query": query}))
    else:
        chain.append(_candidate(
            "media", "play_music",
            "no native music player (Spotify, VLC) is installed on this machine",
            False, {"query": query}))
    if switches.get("webview_engine", True) and switches.get("webview_music", True):
        chain.append(_candidate(
            "webview", "play_music",
            "the internal WebView player is enabled in Settings", True,
            {"query": query}))
    else:
        chain.append(_candidate(
            "webview", "play_music",
            "the internal WebView player is switched off in Settings", False,
            {"query": query}))
    chain.append(_candidate(
        "youtube", "youtube_video",
        "the YouTube action can play a song without any local player", True,
        {"query": query}))
    return chain


def _app_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    target = _app_target(request)
    text = _tokens(request)
    if rule is not None and rule.intent == "app" and rule.action:
        action = rule.action
    elif re.match(r"^(?:close|quit|exit)\b", text):
        action = "close_application"
    elif re.match(r"^(?:focus|switch\s+to)\b", text):
        action = "focus_window"
    else:
        action = "open_application"
    chain: list[dict] = []
    if not switches.get("desktop_control", True):
        ready, reason = False, "desktop control is switched off in Settings"
    elif not switches.get("windows_automation", True):
        ready, reason = False, "Windows application automation is switched off in Settings"
    elif not _usable(f, "desktop"):
        ready, reason = False, (_controller_detail(f, "desktop")
                                or "the desktop controller is not available")
    else:
        ready, reason = True, "pywinauto can launch and focus the application"
    chain.append(_candidate("desktop", "pc_control", reason, ready,
                            {"action": action, "app": target}))
    tars = f.get("agent_tars") or {}
    chain.append(_candidate(
        "agent_tars", "computer_control",
        ("Agent TARS can run the whole desktop job"
         if tars.get("usable") else
         f"Agent TARS is not ready ({tars.get('detail') or 'unavailable'})"),
        bool(tars.get("usable")), {"task": request}))
    return chain


def _web_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    url = _first_url(request) or _site_url(request)
    chain = [
        _candidate("playwright", "browser_action",
                   ("Playwright is installed and can drive the page element by "
                    "element" if _usable(f, "playwright")
                    else _controller_detail(f, "playwright")
                    or "Playwright is not available"),
                   _usable(f, "playwright") and switches.get("playwright_browser", True),
                   {"action": "browser_open", "url": url}),
    ]
    tars = f.get("agent_tars") or {}
    chain.append(_candidate(
        "agent_tars", "browser_control",
        ("Agent TARS can run the whole browser workflow" if tars.get("usable")
         else f"Agent TARS is not ready ({tars.get('detail') or 'unavailable'})"),
        bool(tars.get("usable")) and switches.get("web_task", True),
        {"task": request, "url": url}))
    webview = switches.get("webview", True)
    chain.append(_candidate(
        "webview", "jarvis_window",
        ("the internal HUD panel can show the page" if webview
         else "the internal WebView is switched off in Settings"),
        webview, {"window": "webview", "action": "open", "query": url}))
    return chain


#: Screen actions served by the Windows OCR engine (deterministic, no model).
_OCR_ACTIONS = {"read_screen", "find_text", "click_text", "wait_for_text"}
#: Screen actions served by UI Automation (the control tree, not pixels).
_UI_ACTIONS = {"ui_tree", "ui_read", "ui_click", "ui_set_text"}


def _screen_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    master = switches.get("desktop_control", True)
    chain: list[dict] = []
    # A deterministic screen action (screenshot, OCR, UI Automation) must be
    # served by the local controller that actually implements it — asking a
    # vision model to "take a screenshot" is the misroute this rule prevents.
    if rule is not None and rule.intent == "screen" and rule.action:
        action = rule.action
        if action in _UI_ACTIONS:
            needed, controller = "windows_automation", "uia"
        elif action == "click_text":
            needed, controller = "physical_input", "windows-ocr+pyautogui"
        elif action in _OCR_ACTIONS:
            needed, controller = "screen_understanding", "windows-ocr"
        else:
            needed, controller = "screen_understanding", "pyautogui"
        ready = master and switches.get(needed, True)
        if ready:
            reason = f"{controller} implements '{action}' directly"
        elif not master:
            reason = "desktop control is switched off in Settings"
        else:
            reason = _switch_reason(switches, needed,
                                    "Screen reading" if needed == "screen_understanding"
                                    else "Windows automation")
        chain.append(_candidate(controller, "pc_control", reason, ready,
                                {"action": action}))
    visual = (_usable(f, "uitars_visual") and switches.get("uitars_visual", True)
              and master)
    physical = (_usable(f, "desktop") and switches.get("physical_input", True)
                and master)
    chain.append(_candidate("uitars", "visual_control",
                            ("UI-TARS can find the target on screen and act on it"
                             if visual else
                             _controller_detail(f, "uitars_visual")
                             or "visual control is switched off in Settings"),
                            visual, {"instruction": request}))
    chain.append(_candidate("pyautogui", "pc_control",
                            ("PyAutoGUI can type or click at a known position"
                             if physical
                             else "physical input is switched off in Settings"),
                            physical, {"instruction": request}))
    return chain


def _window_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    action = rule.action if rule is not None else ""
    ready = (switches.get("desktop_control", True)
             and switches.get("window_control", True)
             and _usable(f, "window_control"))
    if ready:
        reason = ("Win32 can arrange the real windows without stealing what "
                  "the user is doing")
    elif not switches.get("desktop_control", True):
        reason = "desktop control is switched off in Settings"
    elif not switches.get("window_control", True):
        reason = "window control is switched off in Settings"
    else:
        reason = (_controller_detail(f, "window_control")
                  or "the window controller is not available on this platform")
    return [_candidate("win32", "window_control", reason, ready,
                       {"action": action})]


def _system_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    action = rule.action if rule is not None else ""
    process_action = action in ("list_processes", "kill_process",
                                "suspend_process", "resume_process",
                                "set_process_priority")
    needed = "process_control" if process_action else "system_settings"
    what = "Process control" if process_action else "System settings"
    ready = (switches.get("desktop_control", True)
             and switches.get(needed, True)
             and _usable(f, "desktop"))
    if ready:
        reason = (f"the Windows system controller implements '{action}' directly"
                  if action else
                  "the Windows system controller is available")
    elif not switches.get("desktop_control", True):
        reason = "desktop control is switched off in Settings"
    elif not switches.get(needed, True):
        reason = _switch_reason(switches, needed, what)
    else:
        reason = (_controller_detail(f, "desktop")
                  or "the Windows system controller is not available")
    return [_candidate("windows", "system_control", reason, ready,
                       {"action": action})]


#: File actions that only read. They work even when the file switch is off.
_FILE_READ_ACTIONS = {"list", "find", "info"}


def _file_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    action = rule.action if rule is not None else ""
    available = _usable(f, "file_control")
    read_only = action in _FILE_READ_ACTIONS
    if read_only:
        ready = available
        reason = ("the filesystem controller can read folders and names without "
                  "any change to the disk" if ready
                  else _controller_detail(f, "file_control")
                  or "the filesystem controller is not available")
    elif not switches.get("file_control", True):
        ready, reason = False, "file control is switched off in Settings"
    else:
        ready = available
        reason = ("the filesystem controller is ready; deletes go to the "
                  "Recycle Bin and nothing overwrites silently" if ready
                  else _controller_detail(f, "file_control")
                  or "the filesystem controller is not available")
    return [_candidate("filesystem", "file_control", reason, ready,
                       {"action": action})]


def _clipboard_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    action = rule.action if rule is not None else ""
    ready = (switches.get("desktop_control", True)
             and switches.get("clipboard_sentry", True)
             and _usable(f, "clipboard"))
    if ready:
        reason = "the clipboard controller can read and write it directly"
    elif not switches.get("clipboard_sentry", True):
        reason = "the clipboard sentry is switched off in Settings"
    else:
        reason = (_controller_detail(f, "clipboard")
                  or "the clipboard controller is not available")
    return [_candidate("clipboard", "clipboard_sentry", reason, ready,
                       {"action": action})]


def _document_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    switches = f.get("switches") or {}
    action = rule.action if rule is not None else ""
    ready = (switches.get("desktop_control", True)
             and switches.get("document_architect", True))
    if ready:
        reason = ("the document architect can write the real file and name it "
                  "in the result")
    elif not switches.get("document_architect", True):
        reason = "the document architect is switched off in Settings"
    else:
        reason = "desktop control is switched off in Settings"
    return [_candidate("documents", "documents", reason, ready,
                       {"action": action})]


def _research_chain(f: dict, request: str, rule: Rule | None = None) -> list[dict]:
    reach = f.get("agent_reach") or {}
    chain = [
        _candidate("agent_reach", "web_research",
                   ("Agent Reach can research this across its live channels"
                    if reach.get("available")
                    else f"Agent Reach is not available ({reach.get('detail') or 'unavailable'})"),
                   bool(reach.get("available")), {"query": request}),
    ]
    chain.extend(_web_chain(f, request, rule))
    return chain


_CHAINS = {"media": _media_chain, "app": _app_chain, "web": _web_chain,
           "screen": _screen_chain, "research": _research_chain,
           "window": _window_chain, "system": _system_chain,
           "file": _file_chain, "clipboard": _clipboard_chain,
           "document": _document_chain}


# ── request parsing helpers ───────────────────────────────────────────────────

def _play_query(request: str) -> str:
    text = " ".join(str(request or "").split()).strip()
    low = text.lower()
    for prefix in ("play music", "play some music", "play a song", "play song",
                   "play me some", "play me", "play"):
        if low.startswith(prefix):
            return text[len(prefix):].strip(" :-–—,") or ""
    return ""


def _app_target(request: str) -> str:
    """The application named in the request, as the controller expects it."""
    text = _tokens(request)
    try:
        from capabilities import desktop
        aliases = desktop.APP_ALIASES
    except Exception:
        return ""
    for alias in sorted(aliases, key=len, reverse=True):
        if _norm(alias) and _norm(alias) in text:
            return _norm(alias)
    return ""


# ── the public entry points ───────────────────────────────────────────────────

def _chat_plan(why: str, source: str, confidence: float,
               state: dict) -> dict:
    return {"intent": "chat", "controller": "", "tool": "", "args": {},
            "reason": why, "candidates": [], "ready": False,
            "source": source, "confidence": confidence, "rule": "",
            "facts": _fact_summary(state)}


def plan(request: str = "", *, intent: str = "", facts: dict | None = None) -> dict:
    """Recommend a controller for one request, with reasons and live evidence.

    ``intent`` overrides classification when the caller already knows what the
    request is. Facts are probed when not supplied (tests inject them). A
    deterministic rule sets the intent AND the exact action/arguments, and those
    are merged into the chosen candidate only when that candidate's tool is the
    one the rule named — a rule can never misdirect a different controller.
    """
    state = facts if facts is not None else _live_facts()
    wanted = str(intent or "").strip().lower()
    rule: Rule | None = None
    match: re.Match | None = None
    if wanted in INTENTS:
        kind, why = wanted, "the caller named the intent"
        source, confidence = "caller", 1.0
    else:
        rule, match = match_rule(request)
        if rule is not None:
            kind, why = rule.intent, rule.why
            source, confidence = "rule", rule.confidence
        else:
            kind, why = classify(request)
            source, confidence = "classifier", 0.55

    if kind == "chat":
        return _chat_plan(why, source, confidence, state)

    chain = _CHAINS[kind](state, request, rule)
    chosen = next((c for c in chain if c["ready"]), None)
    picked = chosen or chain[0]
    args = dict(picked["args"])
    if rule is not None and rule.tool == picked["tool"]:
        try:
            args.update(rule.args(match, request))
        except Exception:
            pass
    return {
        "intent": kind,
        "reason": why,
        "controller": picked["controller"],
        "tool": picked["tool"],
        "args": args,
        "ready": chosen is not None,
        "candidates": chain,
        "source": source,
        "confidence": confidence,
        "rule": rule.name if rule is not None else "",
        "facts": _fact_summary(state),
    }


def quick(request: str = "") -> dict | None:
    """The deterministic route for a request, or None when no rule matches.

    No probes, no model, no I/O: this is the fast path a caller can use on every
    turn to answer "is this one of the commands with exactly one meaning?".
    """
    rule, match = match_rule(request)
    if rule is None:
        return None
    try:
        args = dict(rule.args(match, request))
    except Exception:
        args = {}
    return {"intent": rule.intent, "tool": rule.tool, "action": rule.action,
            "args": args, "rule": rule.name, "confidence": rule.confidence,
            "reason": rule.why, "source": "rule"}


#: How a Laya category maps onto this layer's intents. ``auto`` means "ask the
#: vocabulary again" — AUTOMATE is broad enough that the words should refine it.
_LAYA_INTENT = {
    "SEARCH": "research",
    "CODE": "chat",
    "REVIEW": "chat",
    "VIEW": "screen",
    "FIND": "file",
    "GENERATE": "chat",
    "ANALYZE": "chat",
    "AUTOMATE": "auto",
    "CHAT": "chat",
}


def _same_request(left: str, right: str) -> bool:
    """True when a cached classification was decided on these same words."""
    a, b = _tokens(left), _tokens(right)
    return bool(a) and a == b


def smart(request: str = "", *, facts: dict | None = None,
          allow_laya: bool = True) -> dict:
    """The fused decision: rules first, then Laya, then the vocabulary.

    * a deterministic rule wins outright — a mechanical command must not be
      re-decided by a model that has never seen it;
    * otherwise the local Laya classifier is consulted (a fresh classification
      only when it is genuinely ready; otherwise its most recent decision, when
      that decision was made on the same words);
    * when neither exists, the built-in vocabulary decides.

    The result always carries ``source`` (``rule`` | ``laya`` | ``classifier``)
    and ``confidence``, so no surface can present a guess as a certainty.
    """
    rule, _match = match_rule(request)
    if rule is not None:
        result = plan(request, facts=facts)
        result["source"] = "rule"
        result["rule"] = rule.name
        result["confidence"] = rule.confidence
        return result

    laya = None
    if allow_laya:
        try:
            from core import laya_decision
            last = laya_decision.last_classification()
            if last and _same_request(str(last.get("request") or ""), request):
                laya = dict(last)
            elif laya_decision.get_state() == laya_decision.STATE_READY:
                laya = laya_decision.classify_task(request, record=False)
        except Exception:
            laya = None

    if laya:
        category = str(laya.get("category") or "").strip().upper()
        try:
            confidence = float(laya.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        mapped = _LAYA_INTENT.get(category, "chat")
        if mapped == "auto":
            mapped = classify(request)[0]
        if category == "VIEW" and (_first_url(request) or _site_url(request)
                                   or re.search(r"\bbrowser\b", _tokens(request))):
            mapped = "web"
        result = plan(request, intent=mapped, facts=facts)
        result["source"] = "laya"
        result["rule"] = ""
        result["confidence"] = confidence
        result["laya"] = {"category": category, "confidence": confidence,
                          "cached": bool(laya.get("cached")),
                          "target": str(laya.get("target") or "")}
        result["reason"] = (f"Laya classified this as {category} "
                            f"at {confidence:.2f} confidence")
        return result

    result = plan(request, facts=facts)
    result["source"] = "classifier"
    return result


def _fact_summary(state: dict) -> dict:
    """The few facts that decided the route, kept for the model to see."""
    media = state.get("media") or {}
    controllers = state.get("controllers") or {}
    return {
        "media_installed": sorted((media.get("installed") or {}).keys()),
        "media_preferred": str(media.get("preferred") or ""),
        "desktop": bool(_usable(state, "desktop")),
        "playwright": bool(_usable(state, "playwright")),
        "uitars": bool(_usable(state, "uitars_visual")),
        "power": bool(_usable(state, "power_control")),
        "window": bool(_usable(state, "window_control")),
        "files": bool(_usable(state, "file_control")),
        "ocr": bool(_usable(state, "ocr")),
        "uia": bool(_usable(state, "uia")),
        "clipboard": bool(_usable(state, "clipboard")),
        "agent_tars": bool((state.get("agent_tars") or {}).get("usable")),
        "agent_reach": bool((state.get("agent_reach") or {}).get("available")),
        "switches_off": sorted(k for k, v in (state.get("switches") or {}).items()
                               if not v),
    }


def _args_line(args: dict) -> str:
    try:
        return json.dumps(args, ensure_ascii=False)
    except Exception:
        return str(args)


def render(result: dict) -> str:
    """One plan dict as the text block the model reads.

    Separate from :func:`report` so a caller that already made the decision
    (``smart()``) can render exactly that decision instead of re-deciding it.
    """
    if result["intent"] == "chat":
        return (f"No controller is needed for this: {result['reason']}. Answer it "
                "as a conversation.")
    lines = [f"CONTROL PLAN — intent: {result['intent']}"]
    source = str(result.get("source") or "")
    if source:
        decided = f"decided by: {source}"
        if result.get("rule"):
            decided += f" (rule {result['rule']})"
        if result.get("laya"):
            decided += (f" (Laya: {result['laya'].get('category')} · "
                        f"{result['laya'].get('confidence', 0.0):.2f})")
        lines.append(decided)
    if result["ready"]:
        lines.append(f"use: {result['controller']} via {result['tool']}")
    else:
        lines.append("use: nothing is ready — tell the user what is missing "
                     "instead of substituting another engine")
    if result.get("args"):
        lines.append(f"call with: {_args_line(result['args'])}")
    for index, candidate in enumerate(result["candidates"]):
        state = "ready" if candidate["ready"] else "NOT ready"
        mark = "→" if index == 0 else " "
        lines.append(f"  {mark} {index + 1}. {candidate['controller']} "
                     f"({candidate['tool']}) [{state}] — {candidate['reason']}")
    facts_now = result["facts"]
    lines.append("facts: " + ", ".join(f"{k}={v}" for k, v in facts_now.items()))
    if result["ready"]:
        lines.append("Call that tool with the arguments shown; read its result "
                     "before claiming anything was done.")
    return "\n".join(lines)


def report(request: str = "", *, intent: str = "", mode: str = "plan") -> str:
    """One text block for Gemini: the route, the reasons, the fallbacks.

    ``mode`` selects which decision produced the plan:

    * ``plan``  — deterministic rules + the built-in vocabulary (no model);
    * ``quick`` — rendered identically (use :func:`quick` for the yes/no);
    * ``smart`` — rules first, then the local Laya classifier.
    """
    chosen_mode = str(mode or "plan").strip().lower()
    if chosen_mode == "smart":
        result = smart(request)
    else:
        # "plan" and "quick" render the same route: both are deterministic
        # rules first, then the vocabulary. ``quick()`` itself is the API for
        # a caller that needs the yes/no ("did a rule match?") without a plan.
        result = plan(request, intent=intent)
    return render(result)


__all__ = ["INTENTS", "Rule", "classify", "facts", "match_rule", "plan",
           "quick", "render", "report", "rule_names", "smart"]
