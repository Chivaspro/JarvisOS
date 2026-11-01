#!/usr/bin/env python3
"""J.A.R.V.I.S. Setup & Control Center — one file to go from a bare machine to
a running assistant.

What it does
------------
A compact TWO-LEVEL menu: the main screen shows the everyday actions and each
group opens a small submenu (B/Esc goes back, Q exits). Arrow keys / W-S move,
Enter runs, and every letter/number shortcut still works.

    [1] Quick setup          keys → packages → Gemini → Node → browser → test,
                             end to end in one press
    [H] Auto heal            NEW one pass over every known failure: runtime,
                             packages, config/keys, Gemini, databases, locks,
                             caches — and a plain report of anything it could
                             not repair
    [2] Install & update     ▸ Python 3.13 · packages · update · wipe .venv ·
                               Node runtime · Playwright · REA · Laya · FFmpeg ·
                               Git · Rust native (optional) · install ALL missing
    [3] Keys & providers     ▸ configure API keys (paste-friendly, verified the
                               moment they are typed: '-'=clear, 'test',
                               'open', 'help', 'env') · test connections ·
                               fix Gemini · authorize pages
    [4] Diagnose & fix       ▸ environment check · inspect run_jarvis crashes ·
                               diagnose & install what is missing · view logs ·
                               open the project folder
    [5] Speed doctor     NEW audit what slows replies down (voice renderer,
                               client VAD, provider/model, Laya, Gemini key)
                               and apply a one-key fast profile — backup first
    [6] Latency report   NEW the last session's measured timings from
                               data/logs/jarvis.log: boot/connect markers, the
                               per-turn "end of speech → first audio" latency,
                               the Laya load time and any VAD downgrade
    [7] Settings editor  NEW model · voice renderer · client VAD · wake word ·
                               sensitivity · proactive speaking · narrator ·
                               Laya — edited from here, without opening the app
    [8] Clean & maintain     ▸ optimize (logs/cache/bytecode · precompile ·
                               CPU priority) · CPU priority & threads ·
                               compress/shrink · reset data (backed up)
    [9] Build .exe           a Jarvis OS executable that carries its saved data
                             (config, data, memory, assets) — the project
                             folder itself is never moved or deleted
    [L] Launch J.A.R.V.I.S.  hands over to start_jarvis.py

The Node runtime install walks a repair ladder (npm ci -> npm install ->
legacy-peer-deps -> forced ci) and clears a partial node_modules and a corrupt
npm cache between attempts, so the "npm reported an error" dead end now ends
with a working Agent TARS runtime or an honest manual fix.

The wizard renders in the J.A.R.V.I.S. HUD colours (ui_theme.py's "hud" theme):
arc-reactor cyan, Stark repulsor-gold, the same three status colours; 24-bit on
a real terminal, plain text when piped or ``NO_COLOR`` is set.

Every answered menu item clears the screen first, so the previous output never
piles up on the next one (set ``NO_CLEAR=1`` to keep the scrollback).

Why it is STANDALONE (stdlib only)
----------------------------------
Like ``start_jarvis.py``, this must run on a fresh interpreter that has *no*
third-party packages, because installing them is its job. So it imports only
the standard library and shells out to pip / npm through the project's own
proven helpers. ``tests/unit/test_setup_wizard.py`` locks that invariant in.

Non-interactive use
-------------------
    python setup_jarvis.py --check          # report only
    python setup_jarvis.py --install        # Python dependencies
    python setup_jarvis.py --node           # Node dependencies (Agent TARS)
    python setup_jarvis.py --browser        # Playwright chromium
    python setup_jarvis.py --native         # build the OPTIONAL Rust extension (jarvis_native)
    python setup_jarvis.py --native-status  # native backend report (toolchain/build/import)
    python setup_jarvis.py --keys --yes     # write keys from .env, no prompts
    python setup_jarvis.py --test          # test stored keys live
    python setup_jarvis.py --fix-gemini    # repair Gemini (google-genai + key) and verify
    python setup_jarvis.py --install-missing  # install everything still absent
    python setup_jarvis.py --optimize       # housekeeping
    python setup_jarvis.py --shrink         # reduce folder size (builds, cache, DBs)
    python setup_jarvis.py --inspect-crash  # diagnose run_jarvis crashes & auto-fix
    python setup_jarvis.py --diagnose       # report providers and engines
    python setup_jarvis.py --all --yes      # everything, no prompts
    python setup_jarvis.py --speed-doctor   # audit what slows replies down
    python setup_jarvis.py --speed-doctor --yes   # …and apply the fast profile
    python setup_jarvis.py --latency-report # measured timings from the last session
    python setup_jarvis.py --settings       # edit model/voice/wake/Laya settings
    python setup_jarvis.py --launch         # start J.A.R.V.I.S.

Run it with the interpreter you intend to use::

    python setup_jarvis.py
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# Windows consoles default to a legacy codepage where box-drawing characters
# print as mojibake. Same guard the rest of the project uses — never fatal.
for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
START = ROOT / "start_jarvis.py"
ENTRY = ROOT / "main.py"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_FILE = ROOT / "config" / "api_keys.json"
PACKAGE_JSON = ROOT / "package.json"
VENV = ROOT / ".venv"  # local virtual environment — delete to wipe all packages

_IS_WIN = os.name == "nt"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WIN else {}

MIN_PYTHON = (3, 11)
# The Node runtime the packaged Agent TARS / MCP servers require. This is the
# SAME floor as package.json's "engines" field (>=22.15.0) and the installer's
# probe — one number, two readers: a wizard that accepted an older runtime only
# moved the failure to the first `npm ci`.
NODE_MIN = (22, 15, 0)
NODE_MIN_TEXT = "22.15.0"

#: REA is an npm MCP server, not a Python package. Its pinned release and the
#: two Node matrices live in ``mcp_host/rea.py``; these copies exist because the
#: wizard is stdlib-only and must keep working before the project imports. A
#: unit test pins them to the module so the two can never drift.
REA_PACKAGE = "rea-agents"
REA_VERSION = "6.7.0"
REA_NODE_GATE_TEXT = "Node.js 22.19+, 24.11+, or 26+"
REA_NODE_RECOMMENDED_TEXT = "Node.js 22.23.4+, 24.22.1+, or 26.12.1+"
REA_NODE_GATE = {22: (19, 0), 24: (11, 0)}
REA_NODE_RECOMMENDED = {22: (23, 4), 24: (22, 1), 26: (12, 1)}
REA_TIMEOUT_S = 240.0

# ── ANSI colours — the J.A.R.V.I.S. HUD palette ──────────────────────────────
# The wizard speaks the same colour language as the app itself (ui_theme.py's
# "hud" theme): an arc-reactor ice-cyan working colour, a single Stark
# repulsor-gold signature accent, and the same three status colours. 24-bit
# codes render on a real terminal and degrade to plain text when NO_COLOR is
# set or output is piped, so a logged run stays clean.
_COLOR = bool(sys.stdout and sys.stdout.isatty())
if os.environ.get("NO_COLOR"):
    _COLOR = False

HUD_ACCENT = "#00d4ff"   # arc-reactor cyan — the working colour
HUD_BRIGHT = "#8ceaff"   # lifted cyan — headings
HUD_MID = "#a6e9ff"      # secondary text
HUD_FAINT = "#547f92"    # hint / disabled — the small grey text
HUD_GOLD = "#f4b24a"     # Stark repulsor-gold — the live/active accent
HUD_GREEN = "#5df0b6"    # ready
HUD_AMBER = "#ffc773"    # attention
HUD_RED = "#ff6f8b"      # failed


def _hex_rgb(color: str) -> tuple[int, int, int]:
    s = str(color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except Exception:
        return 0, 0, 0


def _paint(color: str, text: str, heavy: bool = False) -> str:
    """Wrap *text* in a HUD colour. Plain text when colour is switched off."""
    if not _COLOR:
        return text
    r, g, b = _hex_rgb(color)
    lead = "\033[1m" if heavy else ""
    return f"{lead}\033[38;2;{r};{g};{b}m{text}\033[0m"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def cyan(t: str) -> str:   return _paint(HUD_ACCENT, t)
def accent(t: str) -> str: return _paint(HUD_ACCENT, t)
def bright(t: str) -> str: return _paint(HUD_BRIGHT, t, heavy=True)
def gold(t: str) -> str:   return _paint(HUD_GOLD, t)
def mid(t: str) -> str:    return _paint(HUD_MID, t)
def green(t: str) -> str:  return _paint(HUD_GREEN, t)
def yellow(t: str) -> str: return _paint(HUD_AMBER, t)
def red(t: str) -> str:    return _paint(HUD_RED, t)
def faint(t: str) -> str:  return _paint(HUD_FAINT, t)
def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _paint(HUD_FAINT, t)



# ── launcher look (same palette / glyphs as launch_ui.py's ConsoleUI) ────────
L_CYAN = (0, 229, 255)
L_DEEP = (0, 70, 130)
L_WHITE = (200, 250, 255)
L_GREEN = (57, 255, 182)
L_FAINT = (70, 120, 140)
L_TRACK = (18, 52, 70)
_GLYPHS = "01#$%&@?<>/\\|[]{}=+*ABCDEF0123456789"
_SPIN = "◢◣◤◥"
_BANNER = (
    "     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗",
    "     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝",
    "     ██║███████║██████╔╝██║   ██║██║███████╗",
    "██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║",
    "╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║",
    " ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝",
)


def _lerp(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _fg(rgb) -> str:
    return "\x1b[38;2;%d;%d;%dm" % tuple(rgb) if _COLOR else ""


def _rst() -> str:
    return "\x1b[0m" if _COLOR else ""


def _enable_vt() -> None:
    """Switch on ANSI processing for the Windows console (the launcher does too)."""
    if not (_IS_WIN and _COLOR):
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_enable_vt()


def decode(text: str, age: float, duration: float = 0.45) -> str:
    """'Hacking' reveal: unrevealed characters shimmer as random glyphs."""
    import random
    text = str(text)
    if age >= duration or not _COLOR:
        return text
    shown = int(len(text) * max(0.0, age) / duration)
    return "".join(ch if (i < shown or ch == " ") else random.choice(_GLYPHS)
                   for i, ch in enumerate(text))


# ── screen & style helpers ───────────────────────────────────────────────────

def _vlen(text: str) -> int:
    """Visible width of *text* — ANSI colour codes are not letters."""
    width = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\033" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and text[j] != "m":
                j += 1
            i = j + 1
            continue
        width += 1
        i += 1
    return width


_BOX_W = 76


def clear_screen() -> None:
    """Wipe the console so the next answer replaces the last.

    Only touches a real terminal, so a piped or logged run keeps every line.
    Set ``NO_CLEAR=1`` in the environment to keep the scrollback interactively.
    """
    if os.environ.get("NO_CLEAR"):
        return
    if not (sys.stdout and sys.stdout.isatty()):
        return
    try:
        os.system("cls" if _IS_WIN else "clear")
    except Exception:
        try:
            print("\033[2J\033[H", end="")
        except Exception:
            pass


def pause(message: str = "  Press Enter to continue…") -> None:
    """Block until the user acknowledges — never hangs a piped run."""
    if not (sys.stdin and sys.stdin.isatty()):
        return
    try:
        input(dim(message))
    except (EOFError, KeyboardInterrupt):
        print()


_PROGRESS_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)%")


def _progress_bar(percent: float, width: int = 28) -> str:
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100.0))
    return "█" * filled + "░" * (width - filled)


def _terminal_width(default: int = 96) -> int:
    try:
        return max(60, int(shutil.get_terminal_size((default, 20)).columns) - 1)
    except Exception:
        return default


_PROG_STATE = {"label": None, "since": 0.0}


def show_progress(label: str, percent: float, detail: str = "") -> None:
    """One live line in the launcher's style: spinner, decoding label, gradient bar."""
    pct = max(0.0, min(100.0, float(percent)))
    width = _terminal_width()
    now = time.monotonic()
    if _PROG_STATE["label"] != label:
        _PROG_STATE["label"], _PROG_STATE["since"] = label, now
    age = now - _PROG_STATE["since"]

    detail_plain = re.sub(r"\x1b\[[0-9;]*m", "", str(detail or ""))
    detail_plain = " ".join(detail_plain.replace("\r", " ").replace("\n", " ").split())
    bar_w = 24
    prefix = f"  ◆ {label[:26]:<26} [{_progress_bar(pct, bar_w)}] {pct:5.1f}%"
    detail_plain = detail_plain[:max(0, width - len(prefix) - 2)]

    if _COLOR and sys.stdout.isatty():
        spin = _SPIN[int(now * 12) % 4]
        filled = int(round(bar_w * pct / 100.0))
        cells = []
        for i in range(bar_w):
            if i < filled:
                head = (i == filled - 1) and pct < 100
                cells.append(_fg(L_WHITE if head else _lerp(L_DEEP, L_CYAN, i / max(1, bar_w - 1))) + "█")
            else:
                cells.append(_fg(L_TRACK) + "░")
        line = (" " + _fg(L_CYAN) + spin + " " +
                _fg(L_WHITE) + f"{decode(label[:26].upper(), age):<26}" +
                " " + _fg(L_FAINT) + "[" + "".join(cells) + _fg(L_FAINT) + "] " + _rst() +
                _fg(L_WHITE) + f"\x1b[1m{pct:5.1f}%" + _rst() +
                (("  " + _fg(L_FAINT) + detail_plain + _rst()) if detail_plain else ""))
        sys.stdout.write("\r\x1b[2K" + line)
    else:
        plain = prefix + (f"  {detail_plain}" if detail_plain else "")
        sys.stdout.write("\r" + plain.ljust(width)[:width])
    sys.stdout.flush()


def finish_progress(success: bool = True) -> None:
    """Remove the live progress bar before printing the final result."""
    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K\r")
    else:
        sys.stdout.write("\r" + (" " * _terminal_width()) + "\r")
    sys.stdout.flush()


def _run_with_progress(
    cmd: list[str], *, label: str, timeout: float | None = None,
    cwd: Path | None = None, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command while showing real percentages when the child emits them."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        process = subprocess.Popen(
            cmd, cwd=str(cwd or ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=False, bufsize=0, env=merged_env,
            **_NO_WINDOW,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))

    q: queue.Queue[str] = queue.Queue()
    chunks: list[str] = []

    def reader() -> None:
        try:
            assert process.stdout is not None
            while True:
                data = process.stdout.read(1024)
                if not data:
                    break
                q.put(data.decode("utf-8", "replace"))
        except Exception as exc:
            q.put(f"[reader error] {exc}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    pct = 0.0
    detail = "Starting…"
    started = time.monotonic()

    while process.poll() is None or not q.empty():
        try:
            chunk = q.get(timeout=0.12)
        except queue.Empty:
            show_progress(label, pct, detail)
            if timeout and time.monotonic() - started > timeout:
                process.kill()
                break
            continue
        chunks.append(chunk)
        vals = _PROGRESS_RE.findall(chunk)
        if vals:
            try:
                pct = max(pct, float(vals[-1]))
            except ValueError:
                pass
        clean = " ".join(x.strip() for x in re.split(r"[\r\n]+", chunk) if x.strip())
        if clean:
            detail = clean
        show_progress(label, pct, detail)
        if timeout and time.monotonic() - started > timeout:
            process.kill()
            break

    thread.join(timeout=1.0)
    rc = process.poll()
    if rc is None:
        rc = 1
    output = "".join(chunks)
    if rc == 0:
        pct = 100.0
        show_progress(label, pct, "Finished")
    finish_progress(rc == 0)
    return subprocess.CompletedProcess(cmd, rc, output, "")


def _read_menu_key() -> str:
    """Read one key: ↑/↓ or W/S move, Enter runs, Esc back, or a shortcut."""
    if _IS_WIN:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(ch2, "")
        if ch == "\x1b":
            return "esc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch.lower() == "w":
            return "up"
        if ch.lower() == "s":
            return "down"
        return ch.lower()
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                return {"[A": "up", "[B": "down"}.get(seq, "esc")
            if ch in ("\r", "\n"):
                return "enter"
            if ch.lower() == "w":
                return "up"
            if ch.lower() == "s":
                return "down"
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return ask("  select > ").strip().lower()


def rule() -> None:
    print(faint("  " + "─" * _BOX_W))


def section(title: str) -> None:
    """Launcher-style stage header:  ❯ TITLE  with a faint rule beneath."""
    print()
    if _COLOR:
        print(" " + _fg(L_GREEN) + "❯ " + _fg(L_WHITE) + "\x1b[1m" + title.upper() + _rst())
        print(" " + _fg(L_FAINT) + "  " + "─" * max(10, min(_BOX_W, len(title) + 6)) + _rst())
    else:
        print("> " + title.upper())


def banner() -> None:
    """The launch screen's gradient ASCII logo + spaced-out subtitle."""
    print()
    if not _COLOR:
        print("J.A.R.V.I.S :: setup / control center")
        print(f"  {ROOT}")
        print()
        return
    for i, row in enumerate(_BANNER):
        t = i / max(1, len(_BANNER) - 1)
        print(" " + _fg(_lerp(L_CYAN, L_DEEP, t * 0.85)) + row + _rst())
    print(" " + _fg(L_FAINT) + "  S Y S T E M   S E T U P   //   C O N T R O L   C E N T E R" + _rst())
    print(" " + _fg(L_FAINT) + f"  {ROOT}" + _rst())
    print()


def ok(msg: str) -> None:   print("  " + green("[ ok ] ") + msg)
def warn(msg: str) -> None: print("  " + yellow("[warn] ") + msg)
def bad(msg: str) -> None:  print("  " + red("[ X  ] ") + msg)
def info(msg: str) -> None: print("  " + accent("[ .. ] ") + msg)
# ``good`` is the historical name used by the maintenance paths; keep it as an
# alias so a successful repair never ends in a NameError.
def good(msg: str) -> None: ok(msg)


def _run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None,
         capture: bool = True) -> subprocess.CompletedProcess | None:
    """Run a command with a suppressed console window. Never raises."""
    try:
        return subprocess.run(
            cmd, cwd=str(cwd or ROOT),
            capture_output=True if capture else False,
            text=True, timeout=timeout, **_NO_WINDOW,
        )
    except Exception:
        return None


def _version_of(cmd: str, *args: str) -> str:
    r = _run([cmd, *args], timeout=10)
    if not r or r.returncode != 0:
        return ""
    text = (r.stdout or r.stderr or "").strip().splitlines()
    return text[0].strip() if text else ""


def _parse_node_version(text: str) -> tuple[int, int, int]:
    """(major, minor, patch) from a ``node --version`` string; (0,0,0) if junk.

    The old check read only the major integer, so v22.14.9 passed a floor of
    22.15.0. npm's own "engines" enforcement is minor-aware, so the wizard must
    be too — otherwise it certifies a runtime the very next ``npm ci`` rejects.
    """
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def ask(prompt: str, default: str = "") -> str:
    """input() that degrades to the default when stdin is not interactive."""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        value = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value if value != "" else default


def _confirm(prompt: str, default: bool = False) -> bool:
    """Yes/no question. A non-interactive run takes *default* and never blocks."""
    if not (sys.stdin and sys.stdin.isatty()):
        return default
    hint = " [Y/n] " if default else " [y/N] "
    answer = ask("  " + prompt + hint, "y" if default else "n").strip().lower()
    return answer in ("y", "yes")


def _no_console() -> bool:
    """True when there is no usable interactive console (pythonw / VBS)."""
    return not (sys.stdin and sys.stdin.isatty()
                and sys.stdout and sys.stdout.isatty())


def _gui_message(message: str, title: str = "J.A.R.V.I.S") -> None:
    """Native information box for a windowless launch; no-op in a terminal."""
    if not _IS_WIN or not _no_console():
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x40)
    except Exception:
        pass


def _launch_confirm(prompt: str, default: bool = True) -> bool:
    """Yes/no that works with OR without a console.

    ``_confirm`` deliberately answers its default when stdin is not a tty,
    which is right for scripted maintenance and wrong for a launch: a
    double-click would silently decline every repair. The VBS path gets a real
    message box instead.
    """
    if not _no_console():
        hint = " [Y/n] " if default else " [y/N] "
        answer = ask("  " + prompt + hint, "y" if default else "n").strip().lower()
        return answer in ("y", "yes")
    if not _IS_WIN:
        return default
    try:
        import ctypes
        flags = 0x04 | 0x20           # MB_YESNO | MB_ICONQUESTION
        if not default:
            flags |= 0x100            # MB_DEFBUTTON2
        return ctypes.windll.user32.MessageBoxW(
            None, str(prompt), "J.A.R.V.I.S", flags) == 6   # IDYES
    except Exception:
        return default


def _console_python() -> str:
    """A python.exe (never pythonw.exe) for the interactive key wizard."""
    exe = str(sys.executable or "")
    low = exe.lower()
    if low.endswith("pythonw.exe"):
        sibling = exe[:-len("pythonw.exe")] + "python.exe"
        if Path(sibling).is_file():
            return sibling
    if low.endswith("python.exe") and Path(exe).is_file():
        return exe
    return shutil.which("python") or shutil.which("py") or ""


def _open_key_setup() -> bool:
    """Run the interactive key wizard in its own console (pythonw has none)."""
    exe = _console_python()
    script = ROOT / "setup_jarvis.py"
    if not exe or not script.is_file():
        return False
    try:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if _IS_WIN else 0
        proc = subprocess.Popen([exe, str(script), "--keys"],
                                cwd=str(ROOT), creationflags=flags)
        proc.wait()
        return True
    except Exception:
        return False


# ── 1. environment check ─────────────────────────────────────────────────────

def check_python() -> bool:
    v = sys.version_info
    good = (v.major, v.minor) >= MIN_PYTHON
    label = f"Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})"
    (ok if good else bad)(label + ("" if good else
         f"  — need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"))
    if not good:
        warn(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required. "
             "Use menu option [p] or run: winget install Python.Python.3.13")
    return good


def check_pip() -> bool:
    r = _run([sys.executable, "-m", "pip", "--version"], timeout=30)
    good = bool(r and r.returncode == 0)
    (ok if good else bad)("pip " + (r.stdout.strip().split()[1] if good else "not available"))
    return good


def install_python_runtime() -> bool:
    """Install the latest Python 3 via winget (Windows only).

    After winget finishes, the user must reopen the terminal so the new
    ``python`` lands on PATH — the running process cannot update its own PATH.
    Returns ``True`` if winget reported success.
    """
    section("Install Python runtime")
    if not _IS_WIN:
        bad("Automatic Python install is only supported on Windows via winget.")
        info("Visit https://python.org/downloads and install Python 3.13+, then re-run.")
        return False
    if not shutil.which("winget"):
        bad("winget is not available on this machine.")
        info("Install Python 3.13+ manually from https://python.org/downloads")
        return False
    info("Installing latest Python 3.13 via winget — this may take a few minutes…")
    r = _run_with_progress(
        [
            "winget", "install", "--id", "Python.Python.3.13",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        label="Installing Python 3.13", timeout=1800,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("Python 3.13 installed successfully.")
        warn("IMPORTANT: Close and reopen this terminal window so the new "
             "python.exe lands on PATH, then run setup_jarvis.py again.")
    else:
        bad("winget could not install Python automatically.")
        info("Install manually from https://python.org/downloads  "
             "(tick 'Add Python to PATH' in the installer).")
    return good


def check_node() -> tuple[bool, str]:
    exe = shutil.which("node")
    if not exe:
        bad("Node.js not found — needed for Agent TARS and REA")
        return False, "node is not on PATH"
    ver = _version_of("node", "--version")
    good = _parse_node_version(ver) >= NODE_MIN
    (ok if good else warn)(f"Node.js {ver or '?'}" +
        ("" if good else f"  - Agent TARS requires v{NODE_MIN_TEXT} or newer"))
    return good, ver


def _rea_node_supported(version: str) -> bool:
    """npm's own ``engines`` gate for ``rea-agents`` (the hard install gate)."""
    raw = str(version or "").strip().lstrip("vV")
    if not raw or "-" in raw.split("+", 1)[0]:
        return False
    major, minor, _patch = _parse_node_version(raw)
    if major in (22, 24):
        return (minor, _patch) >= REA_NODE_GATE[major]
    return major >= 26 and major % 2 == 0


def _rea_node_recommended(version: str) -> bool:
    """REA's README matrix (a warning, never a hard failure)."""
    raw = str(version or "").strip().lstrip("vV")
    if not raw or "-" in raw.split("+", 1)[0]:
        return False
    major, minor, patch = _parse_node_version(raw)
    floor = REA_NODE_RECOMMENDED.get(major)
    if floor:
        return (minor, patch) >= floor
    return major > 26 and major % 2 == 0


def _rea_npx_root() -> Path:
    if _IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "npm-cache" / "_npx"
    return Path.home() / ".npm" / "_npx"


def _rea_cache_dirs(version: str | None = None) -> list[Path]:
    """Every npx cache entry that contains a real ``rea-agents`` package."""
    wanted = str(version or "").strip().lstrip("vV")
    found: list[Path] = []
    try:
        entries = list(_rea_npx_root().iterdir())
    except OSError:
        return found
    for entry in entries:
        package_json = entry / "node_modules" / REA_PACKAGE / "package.json"
        try:
            if not package_json.is_file():
                continue
            data = json.loads(package_json.read_text(encoding="utf-8"))
            installed = str(data.get("version") or "").strip().lstrip("vV")
        except Exception:
            continue
        if wanted and installed != wanted:
            continue
        found.append(entry)
    return found


def _rea_cached_versions() -> list[str]:
    versions: list[str] = []
    for entry in _rea_cache_dirs():
        try:
            data = json.loads((entry / "node_modules" / REA_PACKAGE /
                               "package.json").read_text(encoding="utf-8"))
            value = str(data.get("version") or "").strip().lstrip("vV")
        except Exception:
            continue
        if value and value not in versions:
            versions.append(value)
    return sorted(versions)


def check_rea() -> tuple[bool, str]:
    """Probe REA's real prerequisites without starting the MCP server."""
    section("REA (Reverse Engineer Anything)")
    node_exe = shutil.which("node")
    npx_exe = shutil.which("npx")
    if not node_exe:
        bad(f"Node.js not found — REA needs {REA_NODE_GATE_TEXT}.")
        return False, "node is not on PATH"
    version = _version_of("node", "--version")
    if not _rea_node_supported(version):
        bad(f"Node.js {version or '?'} is not supported by REA — "
            f"needs {REA_NODE_GATE_TEXT}.")
        return False, f"unsupported node {version}"
    if not npx_exe:
        bad("npx not found — it ships with npm/Node.js.")
        return False, "npx is not on PATH"
    if not _rea_node_recommended(version):
        warn(f"Node.js {version} installs REA but is below its tested matrix "
             f"({REA_NODE_RECOMMENDED_TEXT}).")
    cached = _rea_cache_dirs(REA_VERSION)
    if cached:
        ok(f"REA {REA_VERSION} is cached in npx ({len(cached)} entr"
           f"{'y' if len(cached) == 1 else 'ies'}).")
    else:
        info(f"REA {REA_VERSION} is not cached yet — the first start downloads it.")
    return True, version


def _rea_repair_cache() -> None:
    """Remove a broken npx entry for the pinned release.

    A partial ``_npx`` extraction (the measured failure was a missing
    ``@jridgewell/sourcemap-codec`` ESM file) makes Node exit before the MCP
    handshake. Deleting only that entry is safe: npx re-extracts it.
    """
    for entry in _rea_cache_dirs(REA_VERSION):
        info(f"Removing the partial npx entry {entry.name}…")
        shutil.rmtree(entry, ignore_errors=True)


def _rea_handshake() -> tuple[bool, str]:
    """Start the real server through the venv and prove the protocol works."""
    py = Path(_venv_python())
    if not py.is_file():
        return False, "the venv is missing — run [2] Python packages first"
    script = (
        "import json\n"
        "from mcp_host import rea\n"
        "from mcp_host.session import McpSession\n"
        "s = McpSession(rea.rea_server_spec())\n"
        "ok = s.start()\n"
        "if not ok:\n"
        "    print(json.dumps({'ok': False, 'error': s.error}))\n"
        "    raise SystemExit(1)\n"
        "names = [t.name for t in s.tools]\n"
        "r = s.call_tool('binary_session', {}, timeout=120)\n"
        "print(json.dumps({'ok': bool(r.ok), 'tools': len(names),\n"
        "                  'curated': len(rea.REA_DEFAULT_TOOLS),\n"
        "                  'missing': sorted(set(rea.REA_DEFAULT_TOOLS) - set(names)),\n"
        "                  'protocol': s.protocol_version, 'error': r.error}))\n"
        "s.close('setup probe done')\n"
    )
    result = _run([str(py), "-c", script], timeout=REA_TIMEOUT_S, capture=True)
    if not result:
        return False, "the handshake process could not be started"
    text = (result.stdout or "").strip().splitlines()
    payload = {}
    for line in reversed(text):
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if result.returncode != 0 or not payload.get("ok"):
        detail = payload.get("error") or (result.stderr or "").strip()
        return False, (detail.splitlines()[-1] if detail else "the server did not answer")
    missing = payload.get("missing") or []
    if missing:
        return False, f"the server is missing curated tools: {', '.join(missing[:5])}"
    return True, (f"{payload.get('tools', 0)} tools, "
                  f"{payload.get('curated', 0)} curated, "
                  f"protocol {payload.get('protocol', '?')}")


def _rea_prewarm() -> tuple[bool, str]:
    """Extract the pinned release into the npx cache and prove a handshake.

    ``npm cache add`` only fills npm's content-addressable cache; npx still has
    to extract the package. One real ``--version`` run does that extraction and
    is fast once warm, so the handshake afterwards measures the actual server
    rather than a cache that merely exists.
    """
    npx_exe = shutil.which("npx")
    if not npx_exe:
        return False, "npx is not on PATH (it ships with npm/Node.js)"

    info("Verifying the npm cache…")
    r = _run([_npm_exe(), "cache", "verify"], timeout=300)
    if r and r.returncode != 0:
        warn("npm cache verify reported a problem — continuing with a clean add.")

    info(f"Caching {REA_PACKAGE}@{REA_VERSION} (one download, then npx reuses it)…")
    r = _npm_run(["cache", "add", f"{REA_PACKAGE}@{REA_VERSION}"], timeout=600)
    if not r or r.returncode != 0:
        warn("The package could not be cached.")
        _npm_tail(r)
        return False, "the package could not be downloaded"

    info("Extracting it into the npx cache (this is what the server launches)…")
    extract = _run([npx_exe, "-y", f"{REA_PACKAGE}@{REA_VERSION}", "--version"],
                   timeout=REA_TIMEOUT_S)
    version_text = ((extract.stdout or "") if extract else "").strip().splitlines()
    if version_text:
        info(f"npx reports {version_text[-1].strip()}")
    if not _rea_cache_dirs(REA_VERSION):
        detail = ((extract.stderr or "").strip() if extract else "") or \
                 "npx did not leave an extracted package behind"
        return False, detail.splitlines()[-1][:200] if detail else detail

    info("Starting the real MCP server once to prove the handshake…")
    good, detail = _rea_handshake()
    if not good:
        warn(f"First handshake failed: {detail}")
        info("Repairing the npx extraction and retrying once…")
        _rea_repair_cache()
        good, detail = _rea_handshake()
    return good, detail


def install_rea(confirm: bool = True) -> bool:
    """Pre-warm the pinned REA package and prove a real MCP handshake.

    Nothing is enabled here: REA ships opt-in (``enabled=False``), so this
    makes it *ready* without spawning it on every boot. The user enables it in
    Settings ▸ MCP (or with the ``mcp`` tool) when they want it.
    """
    section(f"REA {REA_VERSION}  (npm · MCP stdio)")
    good, detail = check_rea()
    if not good:
        return False
    if confirm and not _confirm(
            f"Download and verify {REA_PACKAGE}@{REA_VERSION} now?", default=True):
        info("Skipped — REA will download on its first start instead.")
        return False

    good, detail = _rea_prewarm()
    if good:
        ok(f"REA is ready — {detail}.")
        info("REA stays opt-in: enable it in Settings ▸ MCP when you need it.")
        return True
    bad(f"REA could not start: {detail}")
    info(f"Manual check: npx -y {REA_PACKAGE}@{REA_VERSION} mcp")
    return False


def test_rea() -> bool:
    """Diagnose ▸ Test REA — the same real handshake, without reinstalling."""
    section(f"Test REA {REA_VERSION}")
    good, detail = check_rea()
    if not good:
        return False
    good, detail = _rea_handshake()
    if good:
        ok(f"REA answered — {detail}.")
    else:
        bad(f"REA did not answer: {detail}")
        info("Run Install & update ▸ REA to repair the npx cache.")
    return good


# ── Laya AI (local decision model) ───────────────────────────────────────────

#: The Python package and the checkpoint the project ships/detects. Kept in
#: step with ``requirements.txt``, ``app/installer.py`` and
#: ``core/laya_decision.py``; the model itself is data, not a pip package.
LAYA_PACKAGE = "laya==0.4.14"
LAYA_REQUIREMENTS = (LAYA_PACKAGE, "transformers>=4.48.0")
LAYA_MODEL_CANDIDATES = (("models", "laya-multilingual"),
                         ("laya-multilingual",))
LAYA_REQUIRED_FILES = ("rl_agent_config.json", "model.safetensors")
LAYA_REQUIRED_DIRS = ("tokenizer", "encoder")


def _laya_model_dir() -> tuple[Path | None, list[str]]:
    """Locate the checkpoint and list exactly what is missing (never guesses)."""
    for parts in LAYA_MODEL_CANDIDATES:
        path = ROOT.joinpath(*parts)
        if not path.is_dir():
            continue
        missing = [name for name in LAYA_REQUIRED_FILES
                   if not (path / name).is_file()]
        missing += [f"{name}/" for name in LAYA_REQUIRED_DIRS
                    if not (path / name).is_dir()]
        return path, missing
    return None, ["laya-multilingual/"]


def _laya_import_probe() -> tuple[bool, str]:
    """Prove the venv can import Laya's runtime, without loading the model."""
    py = Path(_venv_python())
    if not py.is_file():
        return False, "the .venv is missing — run [2] Python packages first"
    result = _run([
        str(py), "-c",
        "import laya, transformers; "
        "print(laya.__version__, transformers.__version__)"],
        timeout=120)
    if result and result.returncode == 0:
        text = (result.stdout or "").strip().splitlines()
        return True, (text[-1] if text else "laya and transformers import")
    detail = ((result.stderr or "").strip().splitlines()[-1]
              if result and (result.stderr or "").strip() else "")
    return False, detail[:200] or "laya/transformers could not be imported"


def check_laya() -> tuple[bool, str]:
    """Report the checkpoint and the two runtime packages, truthfully."""
    section("Laya AI  (local decision model)")
    model_dir, missing = _laya_model_dir()
    if model_dir is None:
        bad("laya-multilingual/ was not found in models/ or the project root.")
        info("The checkpoint is data, not a pip package: restore the model folder first.")
        return False, "model not found"
    if missing:
        bad(f"The checkpoint at {model_dir} is incomplete: {', '.join(missing)}")
        return False, "model files missing: " + ", ".join(missing)
    ok(f"Checkpoint found at {model_dir.relative_to(ROOT)}.")
    ready, detail = _laya_import_probe()
    if ready:
        ok(f"Runtime packages import ({detail}).")
    else:
        warn(f"Runtime packages are missing ({detail}).")
        info("Run Install & update ▸ Laya AI runtime to add laya + transformers.")
    return ready, detail


def _install_laya_packages() -> bool:
    """Install Laya's two runtime packages into the project venv."""
    if not ensure_venv():
        return False
    vpy = _venv_python()
    info(f"Installing {' + '.join(LAYA_REQUIREMENTS)} into {VENV.name}/ …")
    result = _run_with_progress(
        [vpy, "-m", "pip", "install", *LAYA_REQUIREMENTS,
         "--disable-pip-version-check"],
        label="Installing Laya AI", timeout=1800,
        env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    if not result or result.returncode != 0:
        bad("The Laya runtime packages could not be installed — see pip's output above.")
        return False
    ready, detail = _laya_import_probe()
    if not ready:
        bad(f"Installed, but the import still fails: {detail}")
        return False
    ok(f"Laya runtime installed ({detail}).")
    return True


def _laya_handshake() -> tuple[bool, str]:
    """Load the real checkpoint and make one real decision (slow, honest)."""
    py = Path(_venv_python())
    if not py.is_file():
        return False, "the .venv is missing — run [2] Python packages first"
    model_dir, missing = _laya_model_dir()
    if model_dir is None or missing:
        return False, ("model not found" if model_dir is None
                       else "model incomplete: " + ", ".join(missing))
    script = (
        "import json, sys, time\n"
        "import laya\n"
        "from core.laya_decision import _QUESTIONS\n"
        "t = time.monotonic()\n"
        "agent = laya.load(sys.argv[1], device='cpu')\n"
        "load_s = time.monotonic() - t\n"
        "t = time.monotonic()\n"
        "out = agent.predict('Open Chrome', _QUESTIONS)\n"
        "predict_s = time.monotonic() - t\n"
        "answer = ((out or {}).get('answers') or {}).get('action') or {}\n"
        "print(json.dumps({'ok': bool(answer.get('choice')),\n"
        "                  'choice': answer.get('choice') or '',\n"
        "                  'confidence': answer.get('confidence'),\n"
        "                  'load_s': round(load_s, 1),\n"
        "                  'predict_s': round(predict_s, 2)}))\n"
    )
    result = _run([str(py), "-c", script, str(model_dir)],
                  timeout=900, capture=True)
    if not result:
        return False, "the Laya probe process could not be started"
    payload = {}
    for line in reversed((result.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if result.returncode != 0 or not payload.get("ok"):
        detail = ((result.stderr or "").strip().splitlines()[-1]
                  if (result.stderr or "").strip() else "no decision returned")
        return False, detail[:240]
    return True, (f"{payload.get('choice')} "
                  f"(confidence {float(payload.get('confidence') or 0):.2f}, "
                  f"{payload.get('load_s')}s load, "
                  f"{payload.get('predict_s')}s decision)")


def install_laya(confirm: bool = True) -> bool:
    """Install Laya's runtime packages, then prove a real CPU decision."""
    section(f"Laya AI  ({LAYA_PACKAGE} · CPU)")
    model_dir, missing = _laya_model_dir()
    if model_dir is None or missing:
        bad("The laya-multilingual checkpoint is not available.")
        if model_dir is not None:
            info("Missing: " + ", ".join(missing))
        info("Restore models/laya-multilingual/ first — the package alone cannot decide.")
        return False
    if confirm and not _confirm(
            f"Install {' + '.join(LAYA_REQUIREMENTS)} and run a real test?",
            default=True):
        info("Skipped — Settings ▸ Laya AI will report the missing packages.")
        return False
    if not _install_laya_packages():
        return False
    info("Loading the checkpoint and making one real decision (CPU, up to a minute)…")
    good, detail = _laya_handshake()
    if good:
        ok(f"Laya is ready — {detail}.")
        info("Laya stays advisory: every turn routes normally while it loads.")
        return True
    bad(f"Laya could not decide: {detail}")
    return False


def test_laya() -> bool:
    """Diagnose ▸ Test Laya — a real load + decision, without reinstalling."""
    section("Test Laya AI")
    good, detail = check_laya()
    if not good:
        return False
    good, detail = _laya_handshake()
    if good:
        ok(f"Laya decided — {detail}.")
    else:
        bad(f"Laya did not decide: {detail}")
        info("Run Install & update ▸ Laya AI runtime to repair the packages.")
    return good


def check_tool(name: str, args: tuple[str, ...], why: str, required: bool = False) -> bool:
    exe = shutil.which(name)
    if not exe:
        (bad if required else warn)(f"{name} not found — {why}")
        return False
    ver = _version_of(name, *args)
    ok(f"{name} {ver or ''}".strip() + dim(f"  ({why})"))
    return True


def _pad(text: str, width: int) -> str:
    """Pad to a visible width (ANSI codes do not count)."""
    return text + " " * max(0, width - _vlen(text))


def _env_status(value: bool) -> tuple[str, str]:
    return (green("READY"), "ok") if value else (yellow("MISSING"), "warn")


_ENV_TAG = {"ok": ("ONLINE", L_GREEN), "warn": ("MISSING", (255, 196, 64)),
            "bad": ("FAILED", (255, 84, 96)), "opt": ("OPTIONAL", L_FAINT)}


def _env_dot(state: str) -> str:
    if not _COLOR:
        return {"ok": "+", "warn": "!", "bad": "x", "opt": "-"}.get(state, "-")
    return _fg(_ENV_TAG.get(state, _ENV_TAG["opt"])[1]) + "●" + _rst()


def _env_card_row(label: str, value: str, state: str) -> str:
    """│ ●  PYTHON        3.12.1                      ONLINE │"""
    inner = _BOX_W - 2
    tag, rgb = _ENV_TAG.get(state, _ENV_TAG["opt"])
    tag_txt = (_fg(rgb) + "\x1b[1m" + tag + _rst()) if _COLOR else tag
    label_txt = (_fg(L_WHITE) + label.upper() + _rst()) if _COLOR else label.upper()
    value_txt = (_fg(L_FAINT) + value + _rst()) if _COLOR else value
    left = f" {_env_dot(state)}  {_pad(label_txt, 12)}{_pad(value_txt, 26)}"
    body = _pad(left, inner - 10) + _pad(tag_txt, 9)
    edge = _fg(L_CYAN) + "│" + _rst() if _COLOR else "|"
    return "  " + edge + _pad(body, inner) + edge


def _gauge(pct: float, width: int = 30) -> str:
    """Launcher-style gradient bar."""
    filled = int(round(width * max(0.0, min(100.0, pct)) / 100.0))
    if not _COLOR:
        return "[" + "#" * filled + "." * (width - filled) + "]"
    cells = []
    for i in range(width):
        if i < filled:
            head = i == filled - 1 and pct < 100
            cells.append(_fg(L_WHITE if head else _lerp(L_DEEP, L_CYAN, i / max(1, width - 1))) + "█")
        else:
            cells.append(_fg(L_TRACK) + "░")
    return _fg(L_FAINT) + "[" + "".join(cells) + _fg(L_FAINT) + "]" + _rst()


def _panel_top(title: str, right: str = "") -> str:
    inner = _BOX_W - 2
    t = (_fg(L_GREEN) + "\x1b[1m" + title + _rst()) if _COLOR else title
    r = (_fg(L_FAINT) + right + _rst()) if (_COLOR and right) else right
    fill = max(1, inner - 4 - _vlen(t) - (_vlen(r) + 2 if right else 0))
    edge = lambda s: (_fg(L_CYAN) + s + _rst()) if _COLOR else s
    return ("  " + edge("╭─ ") + t + edge(" " + "─" * fill) +
            ((" " + r + " ") if right else "") + edge("╮" if not right else "─╮"))


def _panel_bottom() -> str:
    s = "╰" + "─" * (_BOX_W - 2) + "╯"
    return "  " + ((_fg(L_CYAN) + s + _rst()) if _COLOR else s)


def report_env() -> dict:
    """Render a small status card instead of a large diagnostic dump."""
    py = sys.version_info
    python_ok = (py.major, py.minor) >= MIN_PYTHON

    pip_ok = bool(_version_of(sys.executable, "-m", "pip", "--version"))

    node_exe = shutil.which("node")
    node_version = _version_of("node", "--version") if node_exe else ""
    node_ok = bool(node_exe and _parse_node_version(node_version) >= NODE_MIN)

    git_ok = bool(shutil.which("git"))
    ffmpeg_ok = bool(shutil.which("ffmpeg"))
    tars_ok = _node_modules_ready()
    rea_ok = bool(node_ok and _rea_cache_dirs(REA_VERSION))
    venv_ok = Path(_venv_python()).is_file()
    native_ok = _native_pyd_is_fresh()

    requirements_ok = REQUIREMENTS.is_file()
    env_ok = ENV_FILE.is_file()
    config_ok = CONFIG_FILE.is_file()

    res = {
        "python": python_ok,
        "pip": pip_ok,
        "node": node_ok,
        "git": git_ok,
        "ffmpeg": ffmpeg_ok,
        "node_modules": tars_ok,
        "rea": rea_ok,
        "venv": venv_ok,
        "requirements": requirements_ok,
        "env_file": env_ok,
        "config": config_ok,
        "native": native_ok,
    }

    py_value = (
        f"{py.major}.{py.minor}.{py.micro}"
        if python_ok else f"{py.major}.{py.minor}  / need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    )
    node_value = node_version if node_ok else "Not installed"
    pip_value = "Installed" if pip_ok else "Not installed"
    venv_value = f".venv/ ({_venv_python()})" if venv_ok else "Not created (run [2])"

    checks = [
        ("Python", py_value, "ok" if python_ok else "bad"),
        ("pip", pip_value, "ok" if pip_ok else "bad"),
        (".venv", venv_value, "ok" if venv_ok else "warn"),
        ("Node.js", node_value, "ok" if node_ok else "warn"),
        ("Git", "Installed" if git_ok else "Not installed",
         "ok" if git_ok else "warn"),
        ("FFmpeg", "Installed" if ffmpeg_ok else "Optional",
         "ok" if ffmpeg_ok else "warn"),
        ("Agent TARS", "Ready" if tars_ok else "Not installed",
         "ok" if tars_ok else "warn"),
        ("REA", f"Cached ({REA_VERSION})" if rea_ok else "Not cached",
         "ok" if rea_ok else "opt"),
        ("Native", "Rust — built" if native_ok else "Optional — not built",
         "ok" if native_ok else "opt"),
    ]

    # Three-state rows: FFmpeg is genuinely optional, so it is not alarming.
    checks[5] = ("FFmpeg", "Installed" if ffmpeg_ok else "Not installed",
                 "ok" if ffmpeg_ok else "opt")
    scored = [c for c in checks if c[2] != "opt"]
    readiness = 100.0 * sum(1 for c in scored if c[2] == "ok") / max(1, len(scored))

    print(_panel_top("ENVIRONMENT", f"{sum(1 for c in checks if c[2] == 'ok')}/{len(checks)} ONLINE"))
    for label, value, state in checks:
        print(_env_card_row(label, value, state))
    edge = _fg(L_CYAN) + "│" + _rst() if _COLOR else "|"
    gauge = " " + (_fg(L_FAINT) + "READINESS " + _rst() if _COLOR else "READINESS ") + _gauge(readiness, 28) + \
        " " + ((_fg(L_WHITE) + f"\x1b[1m{readiness:3.0f}%" + _rst()) if _COLOR else f"{readiness:3.0f}%")
    print("  " + edge + _pad(gauge, _BOX_W - 2) + edge)
    print(_panel_bottom())

    # Keep these files known to the caller without expanding the main card.
    if not requirements_ok:
        print("  " + faint("requirements.txt  ·  missing"))
    if not venv_ok:
        print("  " + faint(".venv/  ·  not created — run option [2] to create and populate it"))
    if not native_ok:
        print("  " + faint("native backend  ·  optional — add it via [2] ▸ Native Rust backend"))
    if not env_ok or not config_ok:
        print("  " + faint("configuration  ·  not initialized"))

    print()
    return res


# ── 2. Python dependencies — isolated local virtual environment (.venv) ────────

def _venv_python() -> str:
    """Absolute path to the venv's Python executable.

    On Windows the layout is ``.venv/Scripts/python.exe``; on POSIX it is
    ``.venv/bin/python``.  The caller can use this path even before the venv
    exists — it simply won't be a real file yet.
    """
    if _IS_WIN:
        return str(VENV / "Scripts" / "python.exe")
    return str(VENV / "bin" / "python")


def ensure_venv() -> bool:
    """Create the project-local virtual environment if it does not exist yet.

    Uses the same interpreter that is running this script so the venv's Python
    version always matches.  Returns ``True`` when the venv is (or becomes)
    ready, ``False`` on failure.
    """
    py = _venv_python()
    if Path(py).is_file():
        ok(f"Virtual environment already exists  ({VENV.name}/)")
        return True
    info(f"Creating virtual environment at  {VENV.name}/  …")
    r = _run([sys.executable, "-m", "venv", str(VENV)], timeout=120)
    if r and r.returncode == 0 and Path(py).is_file():
        ok(f"Virtual environment created  ({VENV.name}/)")
        return True
    bad(f"Could not create virtual environment — "
        f"check that the `venv` stdlib module is available.")
    return False


def install_python_deps() -> bool:
    """Install ``requirements.txt`` into the project-local ``.venv``.

    All three steps run against the venv's own Python/pip so nothing leaks
    into the system or user interpreter.  To start over, delete ``.venv/``.
    """
    section("Python dependencies  (isolated virtual environment)")

    if not REQUIREMENTS.is_file():
        bad(f"requirements.txt is missing — cannot install ({REQUIREMENTS})")
        return False

    # Step 1 — make sure the venv exists.
    if not ensure_venv():
        return False

    vpy = _venv_python()
    info(f"Installing packages from requirements.txt into  {VENV.name}/  …")

    # Step 2 — upgrade pip inside the venv first (silent, best-effort).
    _run([vpy, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
         timeout=120)

    # Step 3 — install the project requirements.
    r = _run_with_progress(
        [
            vpy, "-m", "pip", "install",
            "-r", str(REQUIREMENTS),
            "--disable-pip-version-check",
        ],
        label="Downloading Python packages", timeout=3600,
        env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok(f"Python dependencies installed into  {VENV.name}/")
        info(f"To remove all packages, delete the  {VENV.name}/  folder.")
    else:
        bad("Dependency install reported a problem — "
            "re-run [2] or check the output above.")
    return good


# ── 2b. Native Rust backend (OPTIONAL — the app runs fully without it) ────────
#
# Hybrid design: Python owns the AI, orchestration, voice providers and UI; an
# OPTIONAL Rust extension (native/, built by tools/build_native.py) accelerates
# the PCM hot paths — mic-block RMS/level, reactive envelopes, silence
# compression, resampling and the int16 playback ring. This wizard never makes
# it look required: every state degrades to the working pure-Python backend
# (core/native.py), and a compiler is never installed without asking.
#
# Rust requirements, when someone chooses to build:
#   cargo/rustc  — rustup (winget install Rustlang.Rustup)
#   a linker     — MSVC (Visual Studio Build Tools) OR the GNU toolchain
#                  (rustup toolchain install stable-x86_64-pc-windows-gnu)
#   dlltool      — binutils, needed by the GNU path for Python linking
#                  (winget install BrechtSanders.WinLibs.POSIX.UCRT)

NATIVE_TOOL = ROOT / "tools" / "build_native.py"
NATIVE_DIR = ROOT / "native"
NATIVE_PYD = NATIVE_DIR / "jarvis_native.pyd"
GNU_RUST_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"


def _cargo_exe() -> str | None:
    """cargo from PATH, else the rustup default location (fresh installs)."""
    found = shutil.which("cargo")
    if found:
        return found
    guess = (Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin"
             / ("cargo.exe" if _IS_WIN else "cargo"))
    return str(guess) if guess.is_file() else None


def _native_python() -> str:
    """The interpreter the extension must load in — the venv when it exists."""
    vpy = _venv_python()
    return vpy if Path(vpy).is_file() else sys.executable


def _native_pyd_is_fresh() -> bool:
    """True when jarvis_native.pyd exists and is newer than every source file."""
    if not NATIVE_PYD.is_file():
        return False
    try:
        built = NATIVE_PYD.stat().st_mtime
        sources = [NATIVE_DIR / "Cargo.toml", NATIVE_DIR / "build.rs"]
        sources += list((NATIVE_DIR / "src").glob("*.rs"))
        newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0.0)
        return built >= newest
    except OSError:
        return True  # stat trouble is not worth nagging about


def _dlltool_installed() -> bool:
    """binutils dlltool on PATH or in a default WinLibs install (GNU builds need it)."""
    if shutil.which("dlltool"):
        return True
    pattern = str(Path(os.environ.get("LOCALAPPDATA", "")) /
                  "Microsoft" / "WinGet" / "Packages" /
                  "BrechtSanders.WinLibs*" / "**" / "bin" / "dlltool.exe")
    return any(Path(hit).is_file() for hit in glob.glob(pattern, recursive=True))


def native_backend_report() -> bool:
    """Print the native backend status: toolchain · build · import."""
    section("Native Rust backend  (optional performance extension)")
    if not NATIVE_TOOL.is_file():
        warn(f"Build tool missing: {NATIVE_TOOL} — native backend unavailable.")
        info("The app keeps running on the pure-Python backend — nothing is lost.")
        return False
    r = _run([_native_python(), str(NATIVE_TOOL), "--check"], timeout=300)
    text = ((r.stdout or "") + (r.stderr or "")) if r else ""
    for line in text.strip().splitlines():
        print("      " + faint(line))
    active = bool(r and r.returncode == 0)
    if active:
        ok("Native backend active — Rust accelerates the audio hot paths.")
    else:
        info("Native backend not active — the app runs the pure-Python backend.")
        if _cargo_exe():
            info("Build it via  [2] ▸ Native Rust backend  (or --native).")
        else:
            info("Rust is not installed (optional). The menu row can set it up.")
    return active


def install_rust_toolchain() -> bool:
    """Install Rust + a linker, entirely user-scope (no admin rights needed).

    rustup via winget; the GNU toolchain (which needs no Visual Studio); and
    WinLibs for the binutils ``dlltool`` that Rust's raw-dylib Python linking
    requires on GNU targets.
    """
    section("Install Rust toolchain  (only needed to BUILD the extension)")
    if not _IS_WIN:
        info("Install rustup from https://rustup.rs, then run: python setup_jarvis.py --native")
        return False
    if not shutil.which("winget"):
        bad("winget is not available — install Rust manually from https://rustup.rs")
        info("(Windows: rustup-installer.exe; it needs no admin rights.)")
        return False

    if not _cargo_exe():
        info("Installing rustup with winget — this may take a few minutes…")
        r = _run_with_progress(
            ["winget", "install", "--id", "Rustlang.Rustup", "-e",
             "--accept-package-agreements", "--accept-source-agreements"],
            label="Installing Rust (rustup)", timeout=1800,
        )
        if not (r and r.returncode == 0) or not _cargo_exe():
            bad("rustup could not be installed automatically.")
            info("Install from https://rustup.rs, then re-run this option.")
            return False
        ok("rustup installed.")

    cargo = _cargo_exe()
    rustup = Path(cargo).with_name("rustup.exe" if _IS_WIN else "rustup")
    if rustup.is_file():
        info("Adding the GNU toolchain (works without Visual Studio Build Tools)…")
        r = _run_with_progress(
            [str(rustup), "toolchain", "install", GNU_RUST_TOOLCHAIN, "--profile", "minimal"],
            label="Installing the GNU Rust toolchain", timeout=1800,
        )
        (ok if r and r.returncode == 0 else warn)(
            "GNU toolchain ready." if r and r.returncode == 0
            else "GNU toolchain step reported a problem — MSVC may still work.")

    if not _dlltool_installed():
        info("Installing WinLibs — provides dlltool (binutils) for the GNU build…")
        r = _run_with_progress(
            ["winget", "install", "--id", "BrechtSanders.WinLibs.POSIX.UCRT", "-e",
             "--accept-package-agreements", "--accept-source-agreements"],
            label="Installing WinLibs (binutils)", timeout=1800,
        )
        (ok if r and r.returncode == 0 else warn)(
            "binutils ready." if r and r.returncode == 0
            else "WinLibs install reported a problem — MSVC linker may still work.")
    else:
        ok("binutils dlltool already available.")

    ok("Rust toolchain ready — build the extension next:  [2] ▸ Native Rust backend")
    return True


def build_native_extension() -> bool:
    """Build / rebuild the optional jarvis_native extension in release mode."""
    section("Build native Rust extension  (optional · release mode)")
    if not NATIVE_TOOL.is_file():
        bad(f"Build tool missing: {NATIVE_TOOL}")
        return False
    if not _cargo_exe():
        warn("Rust is not installed on this machine (it is optional).")
        if _confirm("Install the Rust toolchain now (winget, no admin needed)?",
                    default=True):
            if not install_rust_toolchain():
                return False
        else:
            info("Skipped — the app keeps using the pure-Python backend.")
            return False

    r = _run_with_progress(
        [_native_python(), str(NATIVE_TOOL), "--quiet"],
        label="Building jarvis_native (cargo release)", timeout=2400,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("Native extension built — Rust now accelerates the audio hot paths.")
    else:
        bad("Native build failed — the app keeps using the pure-Python backend.")
        tail = ((r.stdout or "") + (r.stderr or "")) if r else ""
        for line in tail.strip().splitlines()[-6:]:
            print("      " + faint(line))
    return good


# ── 3. Node runtime (Agent TARS) ─────────────────────────────────────────────

NPM_TIMEOUT = 2400


def _npm_exe() -> str:
    return shutil.which("npm") or "npm"


def _node_modules_ready() -> bool:
    """A real, non-empty ``node_modules`` is what 'installed' actually means."""
    nm = ROOT / "node_modules"
    try:
        return nm.is_dir() and any(nm.iterdir())
    except OSError:
        return False


def _npm_tail(result, limit: int = 14) -> None:
    """Print the last meaningful npm lines so a failure is readable."""
    text = ""
    if result is not None:
        text = (result.stdout or "") + "\n" + (result.stderr or "")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        print("      " + faint(line))


def _npm_run(args: list[str], timeout: float = NPM_TIMEOUT):
    return _run_with_progress(
        [_npm_exe(), *args], label="Downloading Node packages",
        timeout=timeout, env={
            "NPM_CONFIG_PROGRESS": "true",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_AUDIT": "false",
        },
    )


def install_node_runtime() -> bool:
    """Install Node.js itself (winget on Windows). Best effort, never fatal."""
    section("Node.js runtime")
    if shutil.which("winget"):
        info("Installing Node.js LTS with winget — this may take a minute…")
        r = _run(["winget", "install", "--id", "OpenJS.NodeJS.LTS", "-e",
                  "--accept-package-agreements", "--accept-source-agreements"],
                 timeout=1800, capture=True)
        good = bool(r and r.returncode == 0)
        (ok if good else warn)(
            "Node.js installed — reopen this terminal if `node` is still missing."
            if good else "winget could not install Node.js.")
        return good
    warn(f"winget is not available — install Node.js {NODE_MIN_TEXT}+ from https://nodejs.org")
    return False

def _repair_npm(has_lock: bool) -> None:
    """Clear the two things that actually break an npm install, then retry."""
    # 1. A partial node_modules left by an interrupted download.
    nm = ROOT / "node_modules"
    if nm.is_dir():
        info("Removing the partial node_modules before retrying…")
        shutil.rmtree(nm, ignore_errors=True)
    # 2. A corrupt npm cache.
    info("Clearing the npm cache (a corrupt cache is the usual cause)…")
    r = _npm_run(["cache", "clean", "--force"], timeout=300)
    (ok if (r and r.returncode == 0) else warn)(
        "npm cache cleared." if (r and r.returncode == 0)
        else "Could not clear the npm cache.")
    if has_lock:
        info("Note: `npm ci` needs package.json and package-lock.json in step; "
             "if they disagree the next attempt falls back to `npm install`.")


def install_node_deps() -> bool:
    """Install the Agent TARS Node runtime, repairing the usual failures.

    The old version ran a single command and gave up on any error. This one
    walks a real ladder — pinned ``npm ci``, then ``npm install``, then a
    legacy-peer-deps install, then a forced ``npm ci`` — and clears the two
    things that actually break npm (a partial node_modules and a corrupt cache)
    between attempts, so a transient failure ends with a working runtime.
    """
    section("Node runtime  (Agent TARS — browser & PC automation)")
    node_ok, _ = check_node()
    if not node_ok:
        warn(f"Node.js {NODE_MIN_TEXT}+ is required for the Agent TARS engine.")
        if _confirm("Install Node.js now with winget?", default=False):
            install_node_runtime()
            node_ok, _ = check_node()
        if not node_ok:
            warn(f"Skipping — install Node.js {NODE_MIN_TEXT}+ first "
                 f"(https://nodejs.org, or `winget install OpenJS.NodeJS.LTS`)")
            return False
    if not PACKAGE_JSON.is_file():
        bad(f"package.json is missing ({PACKAGE_JSON})")
        return False
    if not shutil.which("npm"):
        bad("npm was not found on PATH (it ships with Node.js) — reinstall Node.js.")
        return False

    has_lock = (ROOT / "package-lock.json").is_file()

    # The ladder: each rung is only tried after the previous one failed.
    ladder: list[tuple[str, list[str]]] = []
    if has_lock:
        ladder.append(("npm ci", ["ci", "--no-audit", "--no-fund"]))
    ladder.append(("npm install", ["install", "--no-audit", "--no-fund"]))
    ladder.append(("npm install --legacy-peer-deps",
                   ["install", "--no-audit", "--no-fund", "--legacy-peer-deps"]))
    if has_lock:
        ladder.append(("npm ci --force", ["ci", "--force", "--no-audit", "--no-fund"]))

    for index, (label, args) in enumerate(ladder, start=1):
        info(f"[{index}/{len(ladder)}] {label} — downloading the pinned Agent TARS CLI…")
        result = _npm_run(args)
        if result and result.returncode == 0 and _node_modules_ready():
            ok("Node runtime installed — Agent TARS is ready.")
            return True
        warn(f"{label} did not finish cleanly.")
        _npm_tail(result)
        if index < len(ladder):
            _repair_npm(has_lock)

    bad("The Node runtime could not be installed automatically.")
    info("Manual fix: open a terminal in this folder and run `npm install`.")
    return False


# ── 4. Playwright browser binary ─────────────────────────────────────────────

def install_playwright_browser() -> bool:
    section("Playwright browser")
    # Prefer the venv interpreter so playwright runs inside the isolated env.
    vpy = _venv_python()
    py = vpy if Path(vpy).is_file() else sys.executable
    r = _run([py, "-m", "playwright", "--version"], timeout=30)
    if not r or r.returncode != 0:
        warn("The `playwright` package is not installed yet — choose option [2] first")
        return False
    info("Downloading Chromium…")
    r = _run_with_progress(
        [py, "-m", "playwright", "install", "chromium"],
        label="Downloading Chromium", timeout=1800,
        env={"PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "120000"},
    )
    good = bool(r and r.returncode == 0)
    (ok if good else bad)("Chromium ready for browser automation" if good else
                          "Playwright install failed")
    return good


# ── 5. API keys → .env + config/api_keys.json ────────────────────────────────
# (json key, prompt, secret?, where to get it)
CORE_KEYS: list[tuple[str, str, bool, str]] = [
    ("gemini_api_key",    "Gemini API key   (REQUIRED: AI + Live voice)", True,
     "https://aistudio.google.com/app/apikey"),
    ("openai_api_key",    "OpenAI key       (optional: text)", True,
     "https://platform.openai.com/api-keys"),
    ("anthropic_api_key", "Anthropic Claude (optional: text)", True,
     "https://console.anthropic.com/settings/keys"),
    ("groq_api_key",      "Groq key         (optional: fast text)", True,
     "https://console.groq.com/keys"),
    ("openrouter_api_key", "OpenRouter key  (optional: many models)", True,
     "https://openrouter.ai/keys"),
]
VOICE_KEYS: list[tuple[str, str, bool, str]] = [
    ("fish_audio_api_key", "Fish Audio key (optional: cloud voice)", True,
     "https://fish.audio/go-api/"),
    ("elevenlabs_api_key", "ElevenLabs key (optional: cloud voice)", True,
     "https://elevenlabs.io/app/settings/api-keys"),
]
EXTRA_KEYS: list[tuple[str, str, bool, str]] = [
    ("mem0_api_key",        "Mem0 key        (optional: cloud memory)", True,
     "https://app.mem0.ai/dashboard/api-keys"),
    ("qdrant_api_key",      "Qdrant key      (optional: vector knowledge)", True,
     "https://cloud.qdrant.io/"),
    ("qdrant_url",          "Qdrant URL      (default http://localhost:6333)", False, ""),
    ("weather_api_key",     "Weather key     (optional)", True, ""),
    # (The Spotify MCP client-id/secret fields were removed with the server.)
    ("instagram_username",  "Instagram username    (optional, your account)", False, ""),
    ("instagram_password",  "Instagram password    (optional)", True, ""),
    ("smart_home_username", "Kasa/Tapo username    (optional, local network)", False, ""),
    ("smart_home_password", "Kasa/Tapo password    (optional)", True, ""),
    ("github_token",        "GitHub token          (optional, read-only)", True,
     "https://github.com/settings/tokens?type=beta"),
    ("home_assistant_url",  "Home Assistant URL    (optional, e.g. http://homeassistant.local:8123)", False, ""),
    ("home_assistant_token", "Home Assistant token (optional, long-lived)", True,
     "https://www.home-assistant.io/docs/authentication/"),
    ("home_assistant_verify_tls", "Home Assistant TLS check (true/false, default true)", False, ""),
]


def _read_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data: dict) -> bool:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
        return True
    except Exception as exc:
        bad(f"Could not write {CONFIG_FILE}: {exc}")
        return False


def _mask(value: str) -> str:
    v = str(value or "")
    if not v:
        return dim("(not set)")
    if len(v) <= 10:
        return dim("*" * len(v))
    return dim(f"{v[:6]}…{v[-4:]}")


def _ensure_env() -> None:
    """Create .env from .env.example on first run (never overwrites)."""
    if ENV_FILE.exists():
        return
    try:
        if ENV_EXAMPLE.is_file():
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            ok("Created .env from .env.example")
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass


def _read_env() -> dict:
    out: dict[str, str] = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip().upper()] = v.split("#", 1)[0].strip()
    except Exception:
        pass
    return out


def _sync_gemini_env(gemini: str, env_values: dict) -> None:
    """Reflect the Gemini key into .env so shell launches pick it up too."""
    if not gemini or env_values.get("GEMINI_API_KEY") == gemini:
        return
    try:
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.is_file() else ""
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("GEMINI_API_KEY="):
                lines[i] = f"GEMINI_API_KEY={gemini}"
                break
        else:
            lines.append(f"GEMINI_API_KEY={gemini}")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _prompt_key(label: str) -> str | None:
    """Read one value with plain ``input()``.

    ``getpass.getpass`` was used before, but on Windows it reads the console
    key-by-key through ``msvcrt`` and silently drops a pasted key — the exact
    "I cannot paste my API key" failure. ``input()`` takes a paste intact, so
    the value lands whole. Returns ``None`` when there is nothing to read.
    """
    try:
        return input(f"    {label} : ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _key_urls() -> dict[str, str]:
    """field -> sign-up / key page, from the one key table."""
    return {k: url for k, _label, _secret, url
            in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS if url}


def _key_label(field: str) -> str:
    for key, label, _secret, _url in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS:
        if key == field:
            return label.split("(")[0].strip()
    return field


def _open_url(url: str) -> bool:
    """Open a URL in the default browser (stdlib ``webbrowser``). Never raises."""
    if not url:
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _help_for(field: str, url: str = "") -> None:
    """Where to get one credential — printed in the HUD's small grey."""
    target = url or _key_urls().get(field, "")
    print("      " + faint(f"{field}  —  {_key_label(field)}"))
    if target:
        print("      " + faint(f"open: {target}"))
    else:
        print("      " + faint("no sign-up link on record — see the provider's docs"))


#: One-word commands the key prompt accepts, so the hint and the parser agree.
KEY_COMMANDS = {
    "clear": "-", "delete": "-", "remove": "-",
    "test": "test", "verify": "test", "check": "test",
    "open": "open", "auth": "open", "authorize": "open", "get": "open", "link": "open",
    "help": "help", "?": "help", "info": "help",
    "env": "env", "import": "env",
    "skip": "keep",
}


def authorize_keys() -> bool:
    """Open each missing provider's page so the user can authorize an account.

    Nothing is written and no key is invented: this only opens the browser to
    the page that issues the credential, one provider at a time and only when
    asked, so a fresh machine can be authorised end to end.
    """
    section("Authorize providers")
    cfg = _read_config()
    opened = 0
    for key, label, _secret, url in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS:
        if not url:
            continue
        name = label.split("(")[0].strip()
        if str(cfg.get(key, "") or "").strip():
            print("  " + green("✔ ") + f"{name} " + faint("— already authorized"))
            continue
        print("  " + accent("→ ") + f"{name} " + faint(url))
        if _confirm(f"Open the {name} page in your browser now?", default=False):
            if _open_url(url):
                ok(f"Opened {url}")
                opened += 1
            else:
                warn(f"Could not open a browser — visit {url}")
    if opened:
        info("Authorize on the page, copy the key, then use [5] Configure API keys.")
    else:
        info("Nothing opened. Add a key with [5] Configure API keys.")
    return True


def configure_keys(groups: str = "core", non_interactive: bool = False) -> bool:
    """Collect API keys and persist them, preserving everything else.

    ``groups``: ``core`` (Gemini + text providers), ``voice``, ``extra`` or ``all``.
    Non-interactive mode seeds every key from the process environment / ``.env``
    so a scripted install can pass ``GEMINI_API_KEY=…`` and be done.
    """
    section("API keys")
    if groups == "all":
        spec = CORE_KEYS + VOICE_KEYS + EXTRA_KEYS
    elif groups == "voice":
        spec = VOICE_KEYS
    elif groups == "extra":
        spec = EXTRA_KEYS
    else:
        spec = CORE_KEYS

    cfg = _read_config()
    _ensure_env()
    env_values = _read_env()

    if non_interactive:
        changed = 0
        for key, _label, _secret, _url in spec:
            val = env_values.get(key.upper(), "") or os.environ.get(key.upper(), "")
            if val:
                cfg[key] = val
                changed += 1
        if changed:
            ok(f"Imported {changed} key(s) from .env / environment")
        else:
            warn("No keys found in .env or the environment (set GEMINI_API_KEY=… "
                 "or run without --yes to type them)")
        _write_config(cfg)
        _sync_gemini_env(str(cfg.get("gemini_api_key", "") or ""), env_values)
        return True

    print(faint("    Paste a value · Enter keeps the current one."))
    print(faint("    Commands: '-'=clear · test=verify · open=authorize "
                "· help=where to get it · env=import from .env"))
    touched: list[str] = []
    for key, label, _secret, url in spec:
        current = str(cfg.get(key, "") or "")
        print(f"\n  {bright(label)}")
        print(f"    current: {_mask(current)}")
        print("   " + faint("(Enter=keep · '-'=clear · 'test'=verify · "
                            "'open'=authorize · 'help'=info)"))
        typed = _prompt_key("New API KEY")
        if typed is None:
            continue
        action = KEY_COMMANDS.get(typed.strip().lower())
        if action == "keep" or (action is None and typed.strip() == ""):
            continue                       # keep the current value
        if action == "-":
            cfg[key] = ""
            info(f"cleared {key}")
            continue
        if action == "test":
            _report_probe(label, *probe_key(key, current, cfg))
            continue
        if action == "open":
            target = url or _key_urls().get(key, "")
            if target and _open_url(target):
                ok(f"Opened {target} — authorize there, then paste the key here.")
            else:
                warn("No page could be opened — use this link:")
                _help_for(key, url)
            continue
        if action == "help":
            _help_for(key, url)
            continue
        if action == "env":
            value = env_values.get(key.upper(), "") or os.environ.get(key.upper(), "")
            if value:
                cfg[key] = value.strip()
                touched.append(key)
                ok(f"imported {key} from .env / environment")
                _report_probe(label, *probe_key(key, cfg[key], cfg))
            else:
                warn(f"No {key.upper()} found in .env or the environment.")
            continue
        cfg[key] = typed.strip()
        touched.append(key)
        ok(f"set {key}  {_mask(cfg[key])}")
        # Verify the instant it is typed, so a truncated paste or a typo is
        # caught here instead of at the first conversation.
        _report_probe(label, *probe_key(key, cfg[key], cfg))

    if _write_config(cfg):
        ok(f"Saved to {CONFIG_FILE.relative_to(ROOT)}")
    _sync_gemini_env(str(cfg.get("gemini_api_key", "") or ""), env_values)
    if touched and _confirm("Run a live connection test on the keys you just set?",
                            default=True):
        test_keys("all")
    return True


# ── live connection tests (stdlib urllib only — no third-party import) ───────

def _http_status(url: str, headers: dict | None = None,
                 timeout: float = 12.0) -> tuple[int, bytes, str]:
    """One GET with the standard library. Returns ``(status, body, error)``.

    ``status`` is 0 when no HTTP reply arrived at all, so a caller can tell
    "the network is down" apart from "the provider answered 401".
    """
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), response.read(8192), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(8192)
        except Exception:
            body = b""
        return int(getattr(exc, "code", 0) or 0), body, ""
    except Exception as exc:
        return 0, b"", f"{type(exc).__name__}: {exc}"


def probe_key(field: str, value: str, config: dict | None = None) -> tuple[str, str]:
    """Test one credential against its real provider. Returns ``(state, detail)``.

    ``state`` is ``ready`` (the provider accepted it), ``failed``, ``optional``
    (nothing stored), ``present`` (stored, but the provider exposes no safe
    read-only call) or ``skipped`` (no cheap live endpoint exists). Only the
    standard library is used, so this still works before pip has run once.
    """
    field = str(field or "").strip().lower()
    value = str(value or "").strip()
    if not value:
        return "optional", "no value stored"

    def check(label: str, url: str, headers: dict | None,
              count_models: bool = False) -> tuple[str, str]:
        status, body, err = _http_status(url, headers)
        if status == 0:
            return "failed", f"{label} could not be reached ({err})"
        if status in (401, 403):
            return "failed", f"{label} rejected the key (HTTP {status})"
        if status != 200:
            return "failed", f"{label} answered HTTP {status}"
        if count_models:
            try:
                listed = len((json.loads(body.decode("utf-8", "replace")) or {})
                             .get("data") or [])
            except Exception:
                listed = 0
            return "ready", f"{label} accepted the key ({listed} model(s) listed)"
        return "ready", f"{label} accepted the key"

    if field == "gemini_api_key":
        import urllib.parse
        url = ("https://generativelanguage.googleapis.com/v1beta/models?key="
               + urllib.parse.quote(value, safe=""))
        status, _body, err = _http_status(url)
        if status == 0:
            return "failed", f"Google could not be reached ({err})"
        if status == 200:
            return "ready", "Google accepted the key"
        if status in (400, 401, 403):
            return "failed", f"Google rejected the key (HTTP {status})"
        return "failed", f"Google answered HTTP {status}"

    if field == "openai_api_key":
        return check("OpenAI", "https://api.openai.com/v1/models",
                     {"Authorization": f"Bearer {value}"}, count_models=True)
    if field == "groq_api_key":
        return check("Groq", "https://api.groq.com/openai/v1/models",
                     {"Authorization": f"Bearer {value}"}, count_models=True)
    if field == "openrouter_api_key":
        return check("OpenRouter", "https://openrouter.ai/api/v1/key",
                     {"Authorization": f"Bearer {value}"})
    if field == "anthropic_api_key":
        return check("Anthropic", "https://api.anthropic.com/v1/models",
                     {"x-api-key": value, "anthropic-version": "2023-06-01"},
                     count_models=True)
    if field == "fish_audio_api_key":
        return check("Fish Audio", "https://api.fish.audio/model?page_size=1",
                     {"Authorization": f"Bearer {value}"})
    if field == "elevenlabs_api_key":
        return check("ElevenLabs", "https://api.elevenlabs.io/v1/user",
                     {"xi-api-key": value})
    if field in ("qdrant_api_key", "qdrant_url"):
        cfg = config or _read_config()
        url = str(cfg.get("qdrant_url") or value or "http://localhost:6333")
        if "://" not in url:
            url = "http://" + url
        cluster_key = str(cfg.get("qdrant_api_key") or "")
        headers = {"api-key": cluster_key} if cluster_key else {}
        return check("Qdrant", url.rstrip("/") + "/collections", headers)
    if field == "mem0_api_key":
        return "present", ("Mem0 has no read-only call to test a key against — "
                           "the first memory write is the real test")
    return "skipped", "no cheap live endpoint for this credential"


def _report_probe(label: str, state: str, detail: str) -> None:
    """One credential's verdict, in the wizard's own colour language."""
    tag = label.split("(")[0].strip() or "key"
    if state == "ready":
        ok(f"{tag} — {detail}")
    elif state in ("optional", "present", "skipped"):
        print("  " + dim(f"• {tag} — {detail}"))
    else:
        bad(f"{tag} — {detail}")


def _group_spec(groups: str) -> list[tuple[str, str, bool, str]]:
    if groups == "all":
        return CORE_KEYS + VOICE_KEYS + EXTRA_KEYS
    if groups == "voice":
        return VOICE_KEYS
    if groups == "extra":
        return EXTRA_KEYS
    return CORE_KEYS


def test_keys(groups: str = "all") -> bool:
    """Check every stored credential against its provider.

    Returns True when nothing failed. Every verdict comes from a real request,
    so "working" means the provider answered 200 — not merely that a value was
    typed in.
    """
    section("Connection test")
    cfg = _read_config()
    working = failed = quiet = 0
    for key, label, _secret, _url in _group_spec(groups):
        state, detail = probe_key(key, str(cfg.get(key, "") or ""), cfg)
        tag = label.split("(")[0].strip() or key
        if state == "ready":
            ok(f"{tag} — {detail}")
            working += 1
        elif state in ("optional", "present", "skipped"):
            print("  " + dim(f"• {tag} — {detail}"))
            quiet += 1
        else:
            bad(f"{tag} — {detail}")
            failed += 1
    print()
    (ok if failed == 0 else warn)(
        f"{working} working · {failed} failed · {quiet} empty or not testable")
    if failed:
        info("Fix a failing key with [5] Configure API keys, then test again.")
    return failed == 0


# ── automatic Gemini repair ──────────────────────────────────────────────────
# The Settings ▸ API tab reports "Google Gemini — unavailable" for two very
# different reasons: the `google-genai` package is missing from the interpreter
# JARVIS actually runs with (the TEST detail says exactly that), or the stored
# key is not accepted. Both are repaired from here without opening the app: the
# package is installed into the same interpreter start_jarvis.py picks, the key
# is recovered from config/.env, and the repair is proven with a live probe.

GEMINI_IMPORT = "google.genai"
GEMINI_PACKAGE = "google-genai"


def _gemini_target_python() -> str:
    """The interpreter JARVIS will actually run with.

    The launcher prefers the project's own ``.venv`` outright, so the repair
    must land there too — installing into any other interpreter is the exact
    "wrong Python" problem ``start_jarvis.py`` exists to prevent.
    """
    vpy = _venv_python()
    if Path(vpy).is_file():
        return vpy
    return _get_target_python()


def _interpreter_has_module(exe: str, module: str, timeout: float = 30.0) -> bool:
    """True when *exe* can import *module* — asked in its own process.

    ``find_spec`` in this process would answer for the interpreter running the
    wizard, not the one JARVIS uses; the two are often different.
    """
    code = ("import importlib.util, sys; "
            "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)")
    r = _run([exe, "-c", code, module], timeout=timeout)
    return bool(r and r.returncode == 0)


def _gemini_key() -> str:
    """The stored Gemini key, importing it from .env / the environment if needed."""
    cfg = _read_config()
    key = str(cfg.get("gemini_api_key", "") or "").strip()
    if key:
        return key
    env_values = _read_env()
    key = (env_values.get("GEMINI_API_KEY", "")
           or str(os.environ.get("GEMINI_API_KEY", "") or "")).strip()
    if key:
        cfg["gemini_api_key"] = key
        _write_config(cfg)
        _sync_gemini_env(key, env_values)
        ok("Imported the Gemini key from .env / the environment")
    return key


def gemini_available() -> bool:
    """Offline readiness: the SDK imports in the target interpreter + a key exists."""
    return (_interpreter_has_module(_gemini_target_python(), GEMINI_IMPORT)
            and bool(_gemini_key()))


def fix_gemini(*, verify: bool = True, announce: bool = True) -> tuple[bool, str]:
    """Repair the Gemini setup in place: SDK package, stored key, live probe.

    Returns ``(ready, detail)``. ``ready`` is True only when the interpreter
    JARVIS runs with can import google.genai AND a key is stored AND (with
    *verify*) the provider accepted that key just now.
    """
    section("Gemini repair")
    exe = _gemini_target_python()
    info(f"Target interpreter: {exe}")

    module_ok = _interpreter_has_module(exe, GEMINI_IMPORT)
    if not module_ok:
        info(f"{GEMINI_PACKAGE} is missing — installing it into the interpreter "
             f"JARVIS uses…")
        r = _run_with_progress(
            [exe, "-m", "pip", "install", GEMINI_PACKAGE,
             "--disable-pip-version-check"],
            label=f"Installing {GEMINI_PACKAGE}", timeout=900,
            env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        module_ok = (bool(r and r.returncode == 0)
                     and _interpreter_has_module(exe, GEMINI_IMPORT))
    if module_ok:
        ok(f"{GEMINI_PACKAGE} is importable — the Settings TEST can use the SDK")
    else:
        bad(f"{GEMINI_PACKAGE} could not be installed — see the pip output above")
        info(f"Manual fix: \"{exe}\" -m pip install {GEMINI_PACKAGE}")

    key = _gemini_key()
    if not key:
        warn("No Gemini API key is stored — the Settings TEST reports "
             "'missing key' until one is added")
        info("Add it with [5] API keys, then run [x] again.")
        return False, "no gemini_api_key stored"

    if not verify:
        ready = module_ok
        (ok if ready else warn)(
            "Gemini engine ready (offline check — run [6] Test connections "
            "for a live probe)" if ready
            else "Gemini is still incomplete — see the notes above")
        return ready, "package present and key stored (not verified live)"

    state, detail = probe_key("gemini_api_key", key, _read_config())
    _report_probe("Gemini", state, detail)
    ready = module_ok and state == "ready"
    if ready and announce:
        ok("Gemini is ready — the Settings ▸ API TEST will report it online.")
    return ready, detail


def auto_fix_gemini_before_launch() -> None:
    """Repair a broken Gemini, then let the launch continue regardless.

    "Run JARVIS even when Gemini is unavailable" is the point: the assistant
    degrades to its other engines, so this never blocks the launch — it first
    removes the causes the Settings ▸ API test reports as "unavailable" (the
    missing google-genai package, a key present in only one of .env / config)
    and then hands over. Only the offline check runs here; the live probe stays
    in [x] Fix Gemini / [6] Test connections so a launch is never held up by
    the network.
    """
    if gemini_available():
        return
    warn("Gemini is not fully available — repairing automatically before launch…")
    fix_gemini(verify=False)


# ── missing-dependency install (the repair half of Diagnose) ─────────────────

def _feature_rows() -> list[dict]:
    """The installer's on-demand probe, or [] before dependencies exist."""
    try:
        sys.path.insert(0, str(ROOT))
        from app.installer import feature_status
        return feature_status()
    except Exception:
        return []


def _install_feature(feature: str) -> tuple[bool, str]:
    """Install one optional feature through the installer inside ``.venv``.

    The wizard may be launched by a system interpreter, while
    ``app.installer`` installs with ``sys.executable``. Importing it here would
    therefore put the package in the wrong environment. A tiny subprocess runs
    the SAME installer with the venv's Python, so every install lands in the
    environment J.A.R.V.I.S. actually runs from.
    """
    py = Path(_venv_python())
    if py.is_file():
        script = (
            "import json, sys\n"
            "from app.installer import install_feature\n"
            "ok, sentence = install_feature(\n"
            "    sys.argv[1], log=lambda m: print('      ' + str(m), flush=True))\n"
            "print('__JARVIS_FEATURE__' + json.dumps([bool(ok), str(sentence)]))\n"
        )
        result = _run([str(py), "-c", script, feature], timeout=1800,
                      capture=True)
        if result is not None:
            for line in reversed((result.stdout or "").splitlines()):
                if line.startswith("__JARVIS_FEATURE__"):
                    try:
                        ok, sentence = json.loads(line[len("__JARVIS_FEATURE__"):])
                        return bool(ok), str(sentence)
                    except Exception:
                        break
            stderr = (result.stderr or "").strip().splitlines()
            detail = f" ({stderr[-1][:160]})" if stderr else ""
            return False, f"{feature}: the installer did not report a result{detail}"
    # No venv yet (or a test harness): fall back to the in-process probe.
    try:
        sys.path.insert(0, str(ROOT))
        from app.installer import install_feature
        return install_feature(feature, log=lambda m: print("      " + dim(str(m))))
    except Exception as exc:
        return False, f"{feature}: could not install ({type(exc).__name__}: {exc})"


def _count_configured_keys() -> int:
    cfg = _read_config()
    fields = {key for key, _l, _s, _u in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS}
    return sum(1 for field in fields if str(cfg.get(field, "") or "").strip())


def install_all_missing() -> bool:
    """Install everything still absent with a simple overall progress indicator."""
    section("Install all missing")

    show_progress("Overall setup", 0, "Preparing downloads")
    python_ok = install_python_deps()
    show_progress("Overall setup", 25, "Python dependencies finished")

    # Optional native backend: report-only in this sweep — an "install missing"
    # pass must never implicitly install a compiler or start a minute-long
    # cargo build. Building is an explicit action: [2] ▸ Native Rust backend.
    if _native_pyd_is_fresh():
        ok("Native Rust backend: built.")
    else:
        info("Native Rust backend: optional, not built — "
             "add it via [2] ▸ Native Rust backend.")

    info("Step 2/5 — Gemini engine repair (google-genai package + key)")
    gemini_ok, _gemini_detail = fix_gemini()
    show_progress("Overall setup", 35, "Gemini repair finished")

    info("Step 3/5 — optional feature packages")
    rows = _feature_rows()
    if not rows:
        warn("Feature probe unavailable — skipping the optional packages.")
    pending = sorted({r["feature"] for r in rows
                      if not r.get("ready")
                      and r.get("module") not in {"node", "rea"}})
    if rows and not pending:
        ok("Every optional feature package is already installed.")
    for index, feature in enumerate(pending, start=1):
        good, sentence = _install_feature(feature)
        (ok if good else warn)(sentence)
        show_progress("Overall setup", 35 + (20 * index / max(1, len(pending))), feature)
    show_progress("Overall setup", 55, "Optional packages finished")

    info("Step 4/5 — Node runtime (Agent TARS) and Playwright browser")
    node_ok = install_node_deps()
    show_progress("Overall setup", 70, "Node runtime finished")
    browser_ok = install_playwright_browser()
    show_progress("Overall setup", 80, "Playwright browser finished")

    info("Step 5/5 — REA reverse-engineering MCP server")
    rea_ok = install_rea(confirm=False) if node_ok else False
    if not node_ok:
        info("Skipped — REA needs the Node runtime above.")
    show_progress("Overall setup", 100, "All install steps finished")
    print()

    rule()
    if not gemini_ok:
        warn("Gemini still needs attention — run [x] Fix Gemini or [5] API keys.")
    if node_ok and not rea_ok:
        warn("REA is not cached yet — run [2] ▸ REA to retry, or let npx fetch it on first start.")
    good = python_ok and node_ok and browser_ok
    (ok if good else warn)("Install pass finished — run [9] Diagnose & repair to confirm." if good
                           else "Install pass finished with one or more problems.")
    return good

# ── Speed doctor · settings editor · latency report (operator tools) ─────────
#
# Three tools that act on the SAME config/state the app reads:
#
#   Speed doctor     audits the settings that decide how fast replies are
#                    (voice renderer, client VAD, provider/model, Laya) and can
#                    apply a one-key fast profile — with a backup saved first.
#   Settings editor  edits those knobs from here, without opening the app.
#   Latency report   parses the last session's log: boot/connect markers, the
#                    measured "end of speech → first audio" per turn and the
#                    Laya load time — evidence, not guesses.
#
# All three are stdlib-only like the rest of this wizard.

SETTINGS_BACKUP_DIR = ROOT / "config" / "backups"


def _cfg_get(data: dict, path: str, default=""):
    """Read a dotted path ("features.wake_word") from a config dict."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _cfg_set(data: dict, path: str, value) -> None:
    """Write a dotted path, creating intermediate dicts as needed."""
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _backup_config(tag: str) -> str:
    """Timestamped copy of config/api_keys.json before any wizard write.

    Returns the backup's path relative to the project root ("" when there was
    nothing to back up or the copy failed — the caller reports it)."""
    if not CONFIG_FILE.is_file():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = SETTINGS_BACKUP_DIR / f"{tag}_{stamp}.json"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONFIG_FILE, dest)
    except Exception:
        return ""
    try:
        return str(dest.relative_to(ROOT))
    except ValueError:
        return str(dest)


def speed_doctor(apply: bool = False) -> dict:
    """Audit what decides reply speed; optionally write the fast profile.

    Every row is a REAL config read. The fast profile only touches settings
    that measurably delay replies (a re-synthesizing voice renderer, a
    disabled client VAD) and saves a timestamped backup first. Non-interactive
    runs apply only with ``apply`` — never from a quiet default."""
    section("Speed doctor  ·  what slows replies down")
    cfg = _read_config()
    feats = cfg.get("features") if isinstance(cfg.get("features"), dict) else {}
    finding_rows: list[tuple[str, str, str]] = []
    fixes: list[tuple[str, object]] = []

    # 1. Voice renderer — the single largest voice-latency switch.
    vmode = str(cfg.get("voice_output_mode") or "gemini").strip().lower()
    if vmode in ("local", "gemini_piper"):
        whose = ("Gemini→Piper re-synthesizes every reply locally"
                 if vmode == "gemini_piper" else
                 "the local engine speaks only after the whole reply arrives")
        finding_rows.append(("warn", "Voice renderer",
                             f"{vmode}: {whose} — the fastest mode is "
                             "'gemini' (native streaming audio)"))
        fixes.append(("voice_output_mode", "gemini"))
    else:
        finding_rows.append(("ok", "Voice renderer",
                             f"{vmode or 'gemini'} — Gemini's native audio "
                             "streams while it is generated"))

    # 2. Client VAD — without it every voice turn pays the server window.
    vad = feats.get("voice_activity_detection", None)
    if vad is False:
        finding_rows.append(("warn", "Client VAD",
                             "off — the server's ~800 ms end-of-speech window "
                             "sits in front of every voice reply"))
        fixes.append(("features.voice_activity_detection", True))
    elif vad is None:
        finding_rows.append(("note", "Client VAD",
                             "not set — hybrid VAD is used when the session "
                             "accepts the tuning"))
    else:
        finding_rows.append(("ok", "Client VAD",
                             "on — each turn finalises as soon as you stop "
                             "speaking"))

    # 3. Text provider + model (voice ALWAYS uses Gemini).
    provider = str(cfg.get("ai_provider") or "gemini").strip().lower()
    live_model = ""
    live_note = ""
    try:
        sys.path.insert(0, str(ROOT))
        from app.ai_providers import resolve_live_model
        live_model, live_note = resolve_live_model(cfg)
    except Exception:
        live_model = ""
        live_note = ""
    if provider == "gemini":
        detail = (live_model or "gemini (built-in default)")
        if live_note:
            detail += f"  ({live_note})"
        finding_rows.append(("ok", "Model", detail))
    else:
        finding_rows.append(("note", "Model",
                             f"text replies via '{provider}' (HTTP); voice "
                             "still uses Gemini"))

    # 4. Gemini key — present AND accepted, the one hard requirement.
    key = str(cfg.get("gemini_api_key") or "").strip()
    if not key:
        finding_rows.append(("warn", "Gemini key",
                             "missing — add it under [3] Keys & providers, "
                             "then run [x] Fix Gemini"))
    else:
        state, detail = probe_key("gemini_api_key", key, cfg)
        finding_rows.append(("ok" if state == "ready" else "warn",
                             "Gemini key", detail))

    # 5. Laya — advisory; warms up AFTER the first reply, capped at 2 threads.
    if bool(feats.get("laya_ai", False)):
        finding_rows.append(("ok", "Laya AI",
                             "enabled — loads after the first reply, capped "
                             "at 2 CPU threads (deferred warm-up)"))
    else:
        finding_rows.append(("note", "Laya AI", "off — nothing to wait for"))

    # 6. Side lines (narration/notices) — offline engines, no network per line.
    finding_rows.append(("ok", "Side lines",
                         "narration/notice lines use the offline engine "
                         "(Piper ONNX when present, else Windows SAPI)"))

    for state, label, detail in finding_rows:
        if state == "ok":
            ok(f"{label} — {detail}")
        elif state == "warn":
            warn(f"{label} — {detail}")
        else:
            print("  " + dim(f"• {label} — {detail}"))

    print()
    if not fixes:
        ok("No slow-response settings found — this profile is already fast.")
        return {"findings_fixed": [], "applied": False}

    summary = ", ".join(path for path, _v in fixes)
    _interactive = bool(sys.stdin and sys.stdin.isatty())
    if not (apply or (_interactive and _confirm(
            "  Apply the fast profile now? (backup saved first)", default=True))):
        info(f"Not applied. Fast profile would change: {summary}")
        return {"findings_fixed": [p for p, _v in fixes], "applied": False}

    backup = _backup_config("speeddoctor")
    if backup:
        ok(f"Backup saved: {backup}")
    merged = json.loads(json.dumps(cfg))    # deep copy: the real config is only
    for path, value in fixes:               # replaced by the atomic write below
        _cfg_set(merged, path, value)
    if _write_config(merged):
        ok(f"Fast profile applied: {summary}")
        info("Restart J.A.R.V.I.S. ([L]) so the session picks the new settings up.")
        return {"findings_fixed": [p for p, _v in fixes], "applied": True}
    return {"findings_fixed": [], "applied": False}


#: (dotted path, label, allowed values or None, one-line help)
SETTINGS_FIELDS: list[tuple[str, str, tuple[str, ...] | None, str]] = [
    ("assistant_name", "Assistant name", None, "shown as the speaker in chat"),
    ("user_name", "Your name", None, "how JARVIS addresses you"),
    ("ai_provider", "AI provider",
     ("gemini", "openai", "anthropic", "groq", "openrouter", "custom"),
     "text provider — voice always uses Gemini"),
    ("ai_model", "Chat / live model", None, "blank = built-in default (auto-resolved)"),
    ("voice_output_mode", "Voice renderer", ("gemini", "gemini_piper", "local"),
     "gemini = native streaming audio (fastest)"),
    ("features.voice_activity_detection", "Client VAD", ("on", "off"),
     "on = turns finalise the moment you stop speaking"),
    ("features.wake_word", "Wake word", ("on", "off"),
     "local 'Jarvis' wake detection"),
    ("wake_sensitivity", "Wake sensitivity", None, "number 0.0–1.0 (default 0.5)"),
    ("features.proactive_speaking", "Proactive speaking", ("on", "off"),
     "may JARVIS speak up on its own"),
    ("features.narrator", "Narrator lines", ("on", "off"),
     "spoken status lines like 'opening Chrome'"),
    ("features.laya_ai", "Laya AI", ("on", "off"),
     "local task classifier (advisory; deferred warm-up)"),
]

_BOOL_PATHS = {
    "features.wake_word", "features.proactive_speaking", "features.narrator",
    "features.laya_ai", "features.voice_activity_detection",
}


def edit_settings() -> bool:
    """Edit the behaviour knobs in config/api_keys.json from the wizard.

    Same edit language as the API-key prompts: Enter keeps the current value,
    '-' clears it, everything is validated against its allowed set before it
    is written, and a timestamped backup lands in config/backups/ before the
    first write."""
    section("Settings editor  ·  model · voice · wake · Laya · VAD")
    cfg = _read_config()
    if not cfg:
        warn("config/api_keys.json is empty — run [3] Keys & providers first.")
    print(faint("    Enter keeps the current value · '-' clears · 'q' finishes"))
    changed: list[str] = []
    for path, label, allowed, help_text in SETTINGS_FIELDS:
        current = _cfg_get(cfg, path, "")
        shown = str(current) if str(current) not in ("", "None") else dim("(not set)")
        print(f"\n  {bright(label)}  " + faint(f"— {help_text}"))
        print(f"    current: {shown}")
        if allowed:
            print("   " + faint("allowed: " + " / ".join(allowed)))
        typed = str(ask(f"    {label} > ", "") or "").strip()
        if typed == "":
            continue
        if typed.lower() in ("q", "quit", "done"):
            break
        if typed == "-":
            value: object = ""
        elif allowed:
            if typed.lower() not in allowed:
                bad(f"'{typed}' is not allowed here ({', '.join(allowed)}) — "
                    "kept the current value.")
                continue
            value = typed.lower()
        else:
            value = typed
        if path in _BOOL_PATHS:
            value = str(value).lower() in ("on", "true", "yes", "1")
        if path == "wake_sensitivity":
            try:
                value = max(0.0, min(1.0, float(str(value))))
            except ValueError:
                bad("Wake sensitivity must be a number between 0.0 and 1.0 — "
                    "kept the current value.")
                continue
        if str(_cfg_get(cfg, path, "")) == str(value):
            info(f"{label}: already set — unchanged")
            continue
        _cfg_set(cfg, path, value)
        changed.append(path)
        ok(f"{label}: set")

    if not changed:
        info("Nothing changed.")
        return True
    backup = _backup_config("settings")
    if backup:
        ok(f"Backup saved: {backup}")
    if not _write_config(cfg):
        return False
    ok(f"Saved {len(changed)} change(s) to {CONFIG_FILE.relative_to(ROOT)}")
    info("Restart J.A.R.V.I.S. ([L]) for the changes to take effect.")
    return True


_STARTUP_RE = re.compile(r"\[STARTUP\]\s+(\w+).*?elapsed=([\d.]+)s")
_REPLY_RE = re.compile(r"Reply latency:\s*([\d.]+)\s*ms")
_LAYA_RE = re.compile(r"decision model ready on CPU .*?([\d.]+)s load")


def latency_report() -> dict:
    """Measured boot & reply timings from the last logged session.

    Reads data/logs/jarvis.log (plain text, stdlib only): the [STARTUP]
    markers written by main.py, the per-turn "⏱ Reply latency" lines (end of
    speech → first audio), the Laya load time and the VAD-downgrade warning.
    Evidence over guesses — nothing here is estimated."""
    section("Latency report  ·  measured boot & reply timings")
    log_path = ROOT / "data" / "logs" / "jarvis.log"
    if not log_path.is_file():
        info("No log yet — launch J.A.R.V.I.S. ([L]), talk to it once, then try again.")
        return {}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        bad(f"Could not read {log_path.name}: {exc}")
        return {}

    markers: dict[str, float] = {}
    latencies: list[float] = []
    laya_load_s = 0.0
    vad_downgraded = False
    for line in lines[-5000:]:
        m = _STARTUP_RE.search(line)
        if m:
            markers[m.group(1)] = float(m.group(2))
            continue
        m = _REPLY_RE.search(line)
        if m:
            try:
                latencies.append(float(m.group(1)))
            except ValueError:
                pass
            continue
        m = _LAYA_RE.search(line)
        if m:
            try:
                laya_load_s = float(m.group(1))
            except ValueError:
                pass
        if "Voice tuning rejected" in line:
            vad_downgraded = True

    if not markers and not latencies:
        info("The log has no timing markers yet — launch J.A.R.V.I.S. and "
             "talk to it once.")
        return {}

    rows: list[tuple[str, str, str]] = []
    ui_s = markers.get("UI_CREATED")
    connect_s = markers.get("GEMINI_LIVE_CONNECTED")
    first_resp_s = markers.get("AI_FIRST_RESPONSE")
    first_audio_s = markers.get("FIRST_AUDIO")
    if ui_s is not None:
        rows.append(("UI ready", f"{ui_s:.1f}s", "ok"))
    if connect_s is not None:
        rows.append(("Gemini connected", f"{connect_s:.1f}s",
                     "ok" if connect_s <= 15 else "warn"))
    if first_resp_s is not None:
        rows.append(("First response", f"{first_resp_s:.1f}s",
                     "ok" if first_resp_s <= 25 else "warn"))
    if first_audio_s is not None:
        rows.append(("First audio", f"{first_audio_s:.1f}s", "ok"))
    if latencies:
        avg = sum(latencies) / len(latencies)
        rows.append(("Voice reply avg",
                     f"{avg:.0f} ms  ({len(latencies)} turn(s))",
                     "ok" if avg <= 3000 else "warn"))
        rows.append(("Voice reply last", f"{latencies[-1]:.0f} ms",
                     "ok" if latencies[-1] <= 3000 else "warn"))
    if laya_load_s:
        rows.append(("Laya load", f"{laya_load_s:.0f}s  (background)", "opt"))
    rows.append(("VAD downgrade",
                 "active (+~800 ms)" if vad_downgraded else "none",
                 "warn" if vad_downgraded else "opt"))

    print(_panel_top("LATENCY", "last session"))
    for label, value, state in rows:
        print(_env_card_row(label, str(value)[:26], state))
    print(_panel_bottom())
    print("  " + faint(f"source: {log_path.relative_to(ROOT)}"))
    if any(state == "warn" for _l, _v, state in rows):
        info("Amber rows are the ones to fix — try [5] Speed doctor.")
    return {"markers": markers, "turns": len(latencies),
            "vad_downgraded": vad_downgraded}


# ── menu entry helpers (small wrappers that ask one question first) ──────────

def _quick_setup() -> None:
    """The one-key path from a bare machine to a first conversation."""
    section("Quick setup  ·  keys → packages → Gemini → Node → browser → test")
    configure_keys("core")
    install_python_deps()
    fix_gemini()
    install_node_deps()
    install_playwright_browser()
    test_keys("core")
    rule()
    info("Next: [L] Launch J.A.R.V.I.S. — or [5] Speed doctor for a latency check.")


def _configure_keys_entry() -> bool:
    g = (ask("  group [core / voice / extra / all] (core) > ", "core")
         .strip().lower() or "core")
    return configure_keys(g)


def _fix_gemini_entry():
    """Repair Gemini; when it still cannot be fixed, offer the launch anyway."""
    fixed, _detail = fix_gemini()
    if not fixed and _confirm("Start J.A.R.V.I.S. anyway?", default=True):
        return launch()
    return None


# ── 8. Optimize (housekeeping + precompile + CPU profile) ───────────────────

#: Trees that are never precompiled: vendored runtimes, the model store, user
#: data, VCS metadata and the test suite (not imported at runtime).
_COMPILE_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "models", "data", ".git",
    "tests", "AgentReach", "UI-TARS", "site-packages", ".freebuff",
    ".pytest_cache", "build", "dist", "__pycache__", ".cache",
}


def _performance_profile():
    """The stdlib-only CPU/thread profile shared with start_jarvis.py."""
    sys.path.insert(0, str(ROOT))
    from core import performance_profile
    return performance_profile


def _python_sources_in_scope() -> list[Path]:
    """Project ``.py`` files that the runtime actually imports."""
    sources: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _COMPILE_SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                sources.append(Path(dirpath) / name)
    return sources


def _fresh_bytecode(started: float) -> int:
    """Count ``.pyc`` files written strictly after *started* (real measurement)."""
    count = 0
    prune = _COMPILE_SKIP_DIRS - {"__pycache__"}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in prune]
        if os.path.basename(dirpath) != "__pycache__":
            continue
        dirnames[:] = []
        for name in filenames:
            if not name.endswith(".pyc"):
                continue
            try:
                if (Path(dirpath) / name).stat().st_mtime > started:
                    count += 1
            except OSError:
                continue
    return count


def compile_project() -> dict:
    """Precompile the project's own modules so first imports skip parsing.

    Bytecode is written next to each source (``__pycache__``), which is exactly
    what a normal import does — this only moves the cost from the first launch
    after an update to the setup run the user already expects to be slow.
    ``compile_file`` skips a module whose bytecode is already current, so repeat
    runs are cheap. Never raises: a compile failure is reported and Python falls
    back to compiling that module on import.
    """
    sources = _python_sources_in_scope()
    if not sources:
        return {"ok": False, "sources": 0, "updated": 0, "seconds": 0.0}
    started_wall = time.time()
    started = time.monotonic()
    failures = 0
    try:
        import compileall
    except Exception as exc:                     # pragma: no cover - stdlib
        warn(f"Precompile could not run: {type(exc).__name__}: {exc}")
        return {"ok": False, "sources": len(sources), "updated": 0, "seconds": 0.0}
    for path in sources:
        try:
            if not compileall.compile_file(str(path), quiet=1, force=False):
                failures += 1
        except Exception:
            failures += 1
    elapsed = round(time.monotonic() - started, 2)
    return {"ok": failures == 0, "sources": len(sources),
            "updated": _fresh_bytecode(started_wall), "failures": failures,
            "seconds": elapsed}


def _lock_pid() -> int | None:
    """The PID a running J.A.R.V.I.S. published, or None."""
    try:
        text = (ROOT / "data" / "runtime" / "jarvis.lock").read_text(
            encoding="utf-8").strip()
        pid = int(text.splitlines()[0])
        return pid if pid > 0 and pid != os.getpid() else None
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """True when *pid* is a live process; unsure counts as dead."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87      # 87 = no such process
        try:
            code = ctypes.c_ulong(0)
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_ulong)]
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return (not ok) or code.value == 259      # 259 = STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def apply_priority_to_running() -> tuple[bool | None, str]:
    """Apply the saved priority to a live J.A.R.V.I.S., if there is one.

    ``None`` means "no running instance" — not a failure. Changing a process's
    priority class needs no restart, so an Optimize run takes effect at once
    when the app is open.
    """
    pid = _lock_pid()
    if pid is None or not _pid_alive(pid):
        return None, "J.A.R.V.I.S. is not running"
    perf = _performance_profile()
    level = perf.read_profile()["cpu_priority"]
    good, detail = perf.apply_priority(level, pid=pid)
    return (bool(good), f"pid {pid}: {detail}")


def configure_performance() -> bool:
    """Choose the CPU priority and numerical thread budget (saved profile)."""
    section("CPU priority & thread budget")
    perf = _performance_profile()
    profile = perf.read_profile()
    auto_cap = perf.default_thread_cap()
    info(f"Current: {perf.describe(profile)}")
    info("Above normal keeps the UI and microphone responsive while a model "
         "loads; high can starve the desktop on a small machine.")

    priority_rows = {
        "1": "idle", "2": "below_normal", "3": "normal",
        "4": "above_normal", "5": "high",
    }
    current_num = {v: k for k, v in priority_rows.items()}.get(
        profile["cpu_priority"], "4")
    choice = ask("  priority [1 idle · 2 below normal · 3 normal · "
                 f"4 above normal · 5 high] ({current_num}) > ", current_num)
    profile["cpu_priority"] = priority_rows.get(choice.strip(), profile["cpu_priority"])

    cap_default = str(profile["thread_cap"])
    cap_text = ask(f"  numerical thread cap [0 = auto ({auto_cap}), 1-16] "
                   f"({cap_default}) > ", cap_default).strip()
    try:
        cap = int(cap_text)
        if 0 <= cap <= 64:
            profile["thread_cap"] = cap
    except ValueError:
        warn(f"'{cap_text}' is not a number — keeping {cap_default}.")

    if not perf.write_profile(profile):
        bad(f"Could not write the profile to {CONFIG_FILE.relative_to(ROOT)}.")
        return False
    ok(f"Saved — {perf.describe(profile)}.")
    info("start_jarvis applies this to every launch (environment + process).")

    if _confirm("Switch the Windows power plan to High performance now? "
                "(system-wide, stays until changed)", default=False):
        good, detail = perf.set_power_plan("high")
        (ok if good else warn)(detail)
    return True


# ── Windows graphics & performance suite (reversible, Windows-only) ─────────
#
# The Optimize pass does not stop at Python: on Windows the same click should
# also point the machine at performance. Two halves, both reversible:
#
# * core/joptimizer.jarvis_ultimate_optimize — the ported HI+ Optimizer suite
#   (power plan, visual effects for performance, transparency off, Game DVR
#   capture off, Game Mode on, widgets off, privacy extras, JARVIS caches). It
#   snapshots every touched registry value + service start type first, and [u]
#   restores that snapshot exactly.
# * The per-app graphics preference (Settings ▸ Display ▸ Graphics): the Python
#   executables that run J.A.R.V.I.S. are pointed at the high-performance GPU
#   instead of whatever Windows picks. The previous value of every entry is
#   backed up before it is written.

#: Windows' per-app GPU preference store. The value NAME is the executable's
#: full path; the DATA is "GpuPreference=N;" where 0 = power saving and
#: 2 = high performance (an absent value means "let Windows decide").
_GPU_PREF_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"
_WINDOWS_TUNING_FILE = "data/runtime/windows_tuning.json"


def _joptimizer():
    """The reversible Windows maintenance backend, loaded by path.

    ``core/joptimizer.py`` imports only the standard library at module level
    (psutil is imported lazily inside its metric helpers), so this wizard keeps
    its stdlib-only rule while reusing every tested Windows operation.
    """
    sys.path.insert(0, str(ROOT))
    from core import joptimizer
    return joptimizer


def _tuning_file() -> Path:
    return ROOT / _WINDOWS_TUNING_FILE


def _tuning_backup() -> dict:
    try:
        data = json.loads(_tuning_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_tuning_backup(data: dict) -> bool:
    try:
        path = _tuning_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _gpu_targets() -> list[Path]:
    """The Python executables that actually run J.A.R.V.I.S. (real paths only)."""
    out: list[Path] = []
    for name in ("python.exe", "pythonw.exe"):
        candidate = ROOT / ".venv" / "Scripts" / name
        if candidate.is_file():
            out.append(candidate)
    for name in ("python.exe", "pythonw.exe"):
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found:
            out.append(Path(found))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def gpu_preference_state() -> list[tuple[str, str]]:
    """(executable, preference label) from the real registry; [] off-Windows."""
    try:
        j = _joptimizer()
        winreg = j.winreg
    except Exception:
        return []
    if not getattr(j, "IS_WINDOWS", False):
        return []
    rows: list[tuple[str, str]] = []
    for exe in _gpu_targets():
        value = ""
        try:
            found = j.registry_read(winreg.HKEY_CURRENT_USER, _GPU_PREF_PATH,
                                    str(exe))
            if found.get("exists"):
                value = str(found.get("value") or "")
        except Exception:
            value = ""
        if "2;" in value:
            label = "high performance"
        elif "0;" in value:
            label = "power saving"
        else:
            label = "system default"
        rows.append((str(exe), label))
    return rows


def apply_gpu_preference(high: bool = True) -> tuple[bool, str]:
    """Point Windows' per-app graphics preference at the Python processes.

    Reversible: the previous value of every entry is saved first, and
    :func:`restore_gpu_preference` puts it back (or removes the entry we added).
    """
    try:
        j = _joptimizer()
        winreg = j.winreg
    except Exception as exc:
        return False, f"registry access unavailable ({type(exc).__name__})"
    if not getattr(j, "IS_WINDOWS", False):
        return False, "Windows-only"
    targets = _gpu_targets()
    if not targets:
        return False, "no python.exe/pythonw.exe was found to point at"
    backup = _tuning_backup()
    previous = dict(backup.get("gpu_preferences") or {})
    wanted = "GpuPreference=2;" if high else "GpuPreference=0;"
    written = 0
    for exe in targets:
        key = str(exe)
        if key not in previous:
            try:
                before = j.registry_read(winreg.HKEY_CURRENT_USER,
                                         _GPU_PREF_PATH, key)
                previous[key] = (str(before.get("value"))
                                 if before.get("exists") else None)
            except Exception:
                previous[key] = None
        try:
            j.registry_write(winreg.HKEY_CURRENT_USER, _GPU_PREF_PATH, key,
                             wanted, winreg.REG_SZ)
            written += 1
        except Exception:
            continue
    backup["gpu_preferences"] = previous
    _save_tuning_backup(backup)
    if not written:
        return False, "Windows refused every graphics-preference write"
    return True, (f"{written} Python executable(s) set to "
                  f"{'high performance' if high else 'power saving'} "
                  "(Settings ▸ Display ▸ Graphics)")


def restore_gpu_preference() -> tuple[bool, str]:
    """Put back (or remove) every graphics-preference entry we wrote."""
    try:
        j = _joptimizer()
        winreg = j.winreg
    except Exception as exc:
        return False, f"registry access unavailable ({type(exc).__name__})"
    if not getattr(j, "IS_WINDOWS", False):
        return False, "Windows-only"
    backup = _tuning_backup()
    previous = dict(backup.get("gpu_preferences") or {})
    if not previous:
        return False, "no graphics-preference backup exists"
    restored = 0
    for exe, value in previous.items():
        try:
            if value:
                j.registry_write(winreg.HKEY_CURRENT_USER, _GPU_PREF_PATH, exe,
                                 str(value), winreg.REG_SZ)
            else:
                j.registry_delete_value(winreg.HKEY_CURRENT_USER,
                                        _GPU_PREF_PATH, exe)
            restored += 1
        except Exception:
            continue
    backup.pop("gpu_preferences", None)
    _save_tuning_backup(backup)
    return (restored > 0,
            f"{restored} graphics-preference entr(y/ies) restored")


def _broadcast_setting_change() -> None:
    """Tell Windows a visual setting changed so it applies without sign-out."""
    if os.name != "nt":
        return
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("VisualEffects"), SMTO_ABORTIFHUNG, 3000,
            ctypes.byref(result))
    except Exception:
        pass


def _windows_tuning_rows() -> list[tuple[str, str, str]]:
    """(label, value, state) read from the real registry/powercfg — no guesses."""
    try:
        j = _joptimizer()
        winreg = j.winreg
    except Exception as exc:
        return [("Windows tuning", f"unavailable ({type(exc).__name__})", "warn")]
    if not getattr(j, "IS_WINDOWS", False):
        return [("Windows tuning", "Windows-only", "opt")]

    def _value(target: str, name: str):
        root, path, _ = j.REGISTRY_TARGETS[target]
        found = j.registry_read(root, path, name)
        return found.get("value") if found.get("exists") else None

    visual = _value("visual_effects", "VisualFXSetting")
    visual_label = {0: "let Windows choose", 1: "best appearance",
                    2: "best performance", 3: "custom"}.get(visual)
    rows = [("Visual effects", visual_label or "let Windows choose",
             "ok" if visual == 2 else "opt")]
    transparency = _value("transparency", "EnableTransparency")
    rows.append(("UI transparency", "off" if transparency == 0 else "on",
                 "ok" if transparency == 0 else "opt"))
    dvr = _value("game_dvr", "GameDVR_Enabled")
    rows.append(("Game DVR capture", "off" if dvr == 0 else "on",
                 "ok" if dvr == 0 else "opt"))
    mode = _value("game_mode", "AutoGameModeEnabled")
    rows.append(("Game Mode", "on" if mode == 1 else "off",
                 "ok" if mode == 1 else "opt"))
    plan = ""
    try:
        plan = str(j.get_active_power_plan() or "")
    except Exception:
        plan = ""
    high = ("high" in plan.lower() or "ultimate" in plan.lower())
    rows.append(("Power plan", plan.split("(")[-1].rstrip(")")[:26] or "unknown",
                 "ok" if high else "opt"))
    return rows


def _print_windows_tuning() -> None:
    """The read-only Windows tuning state (settings + per-app graphics)."""
    print(_panel_top("WINDOWS TUNING", "read-only"))
    for label, value, state in _windows_tuning_rows():
        print(_env_card_row(label, str(value)[:26], state))
    print(_panel_bottom())
    gpu_rows = gpu_preference_state()
    if gpu_rows:
        print(_panel_top("GRAPHICS PREFERENCE", "per app"))
        for exe, label in gpu_rows[:4]:
            path = Path(exe)
            # Two installs share the basename "python.exe": the folder is what
            # tells the venv interpreter apart from the system one.
            name = f"{path.parent.name}/{path.name}"
            print(_env_card_row(name[:26], label,
                                "ok" if label == "high performance" else "opt"))
        print(_panel_bottom())


def apply_windows_performance(confirm: bool = True) -> tuple[bool, str]:
    """Apply the reversible Windows performance suite (snapshot first).

    The suite is ``core/joptimizer.jarvis_ultimate_optimize`` — HKCU + power
    plan only, every touched value snapshotted, J.A.R.V.I.S. caches cleared —
    plus the per-app graphics preference for the Python processes. Nothing here
    is irreversible; ``undo_windows_performance`` restores both halves.
    """
    try:
        j = _joptimizer()
    except Exception as exc:
        return False, f"the Windows tuning backend is unavailable ({exc})"
    if not getattr(j, "IS_WINDOWS", False):
        return False, "Windows-only"
    # Default NO on purpose: a non-interactive run (a script, CI, pythonw)
    # must never change machine-wide graphics or power settings by accident.
    # The prompt names the big-ticket changes so consent is informed.
    if confirm and not _confirm(
            "Apply the reversible Windows performance suite now? "
            "(power plan High performance · visual effects for performance · "
            "high-performance GPU for Python · snapshot first; [u] undoes it)",
            default=False):
        info("Skipped — nothing changed.")
        return False, "skipped"
    try:
        text = j.jarvis_ultimate_optimize()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    for line in str(text).splitlines():
        print("  " + line)
    good, detail = apply_gpu_preference(high=True)
    if good:
        ok(detail + ".")
    else:
        info(f"Graphics preference left alone — {detail}.")
    _broadcast_setting_change()
    return True, "applied"


def configure_windows_performance() -> bool:
    """Menu row: show the current state, then offer the reversible suite."""
    section("Windows graphics & performance  (reversible)")
    _print_windows_tuning()
    info("The suite applies: power plan High performance · visual effects for "
         "performance · transparency off · Game DVR capture off · Game Mode on · "
         "widgets off · privacy extras · JARVIS caches cleared · high-performance "
         "GPU for the Python processes.")
    info("A snapshot of every touched value and service start type is saved "
         "first, so [u] restores the machine exactly.")
    applied, detail = apply_windows_performance(confirm=True)
    if applied:
        ok("Windows performance suite applied.")
    elif detail == "skipped":
        return False
    else:
        warn(f"Windows performance suite not applied — {detail}.")
    return applied


def undo_windows_performance() -> bool:
    """Menu row: restore the last snapshot and the graphics preference.

    One gate covers both halves, and its default is NO: a non-interactive run
    (a script, a test, pythonw) must never restore machine state by accident —
    the probe that caught the previous default also proved a bare pytest-created
    snapshot was sitting in the real snapshot folder.
    """
    section("Undo Windows optimization")
    try:
        j = _joptimizer()
    except Exception as exc:
        bad(f"Restore unavailable: {exc}")
        return False
    if not getattr(j, "IS_WINDOWS", False):
        info("Windows-only — nothing to undo on this platform.")
        return False
    snaps = j.list_snapshots()
    backup = _tuning_backup().get("gpu_preferences") or {}
    if not snaps and not backup:
        info("Nothing to undo yet — the Windows performance suite has not run "
             "(no snapshot and no graphics-preference backup).")
        return False
    if snaps:
        info(f"Latest snapshot: {snaps[0].name}")
    if backup:
        info(f"Graphics preference: {len(backup)} entr(y/ies) to restore.")
    if not _confirm("Restore the last Windows optimization now? "
                    "(registry values + service start types + graphics "
                    "preference)", default=False):
        info("Skipped — nothing was restored.")
        return False
    restored_any = False
    if snaps:
        line = str(j.jarvis_revert_latest())
        (warn if "warning" in line.lower() else ok)(line)
        restored_any = True
    good, detail = restore_gpu_preference()
    if good:
        ok(detail + ".")
        restored_any = True
    elif "no graphics-preference backup" not in detail:
        info(detail)
    return restored_any


def windows_performance_report() -> None:
    """Menu row: read-only state + host health from the real machine."""
    section("System health  (read-only)")
    _print_windows_tuning()
    try:
        j = _joptimizer()
        summary = j.get_system_summary()
        score = j.health_score(summary)
        rows = [
            ("Health", f"{score}/100", "ok" if score >= 80 else "warn"),
            ("CPU", f"{summary['cpu']:.0f}%",
             "ok" if summary["cpu"] < 80 else "warn"),
            ("RAM", f"{summary['ram']:.0f}%  of {summary['ram_gb']} GB",
             "ok" if summary["ram"] < 80 else "warn"),
            ("Disk free", f"{summary['disk_free_gb']} GB",
             "ok" if summary["disk_free_gb"] > 10 else "warn"),
        ]
        print(_panel_top("HOST", "now"))
        for label, value, state in rows:
            print(_env_card_row(label, str(value)[:26], state))
        print(_panel_bottom())
        for line in j.top_processes(6):
            print("  " + faint(line))
    except Exception as exc:
        # psutil is a venv dependency, not a setup one: run the same scan on the
        # interpreter J.A.R.V.I.S. actually uses instead of reporting "no data".
        scanned = _venv_smart_scan()
        if scanned:
            ok(scanned)
        else:
            warn(f"Live metrics need the venv's psutil "
                 f"({type(exc).__name__}: {exc}).")
            info("Run [5] Speed doctor for the measured latency panel instead.")


def _venv_smart_scan() -> str:
    """core.joptimizer.smart_scan on the venv interpreter ("" when it cannot)."""
    try:
        vpy = _venv_python()
        if not Path(vpy).is_file():
            return ""
        proc = subprocess.run(
            [vpy, "-c",
             "from core import joptimizer; print(joptimizer.smart_scan())"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (proc.stdout or "").strip()
        return text if proc.returncode == 0 and text else ""
    except Exception:
        return ""


def optimize() -> dict:
    """The full optimization pass, in one deliberate click.

      1. rotate logs / expire cache / drop stale bytecode (core.maintenance)
      2. precompile the project's own modules (first-import cost moves to now)
      3. save the CPU priority & thread profile and apply it to a live instance
         (the priority class Task Manager shows)
      4. the reversible Windows performance suite: graphics/visual effects for
         performance, transparency, Game DVR, Game Mode, power plan and the
         high-performance GPU for the Python processes — snapshot first, so [u]
         restores the machine exactly
      5. a read-only summary of what is now in effect
    """
    section("Optimize  (logs · cache · bytecode · CPU · Windows graphics)")
    report: dict = {}

    info("Step 1/5 — rotate logs, expire cache, drop stale bytecode")
    try:
        sys.path.insert(0, str(ROOT))
        from core.maintenance import run_maintenance, format_report
        report = run_maintenance()
        for line in format_report(report).splitlines():
            print("  " + line)
    except Exception as exc:
        bad(f"Maintenance could not run: {type(exc).__name__}: {exc}")

    info("Step 2/5 — precompile the project's own modules")
    compiled = compile_project()
    report["compile"] = compiled
    if compiled.get("ok"):
        ok(f"{compiled['sources']} source module(s) verified, "
           f"{compiled['updated']} bytecode file(s) refreshed "
           f"in {compiled['seconds']}s.")
    elif compiled.get("sources"):
        warn("Some modules could not be precompiled — Python will compile them "
             "on import instead.")
    else:
        warn("No source modules were found to precompile.")

    info("Step 3/5 — CPU priority & thread budget")
    try:
        perf = _performance_profile()
        profile = perf.read_profile()
        if perf.write_profile(profile):
            ok(f"Profile saved — {perf.describe(profile)}.")
        else:
            warn("The performance profile could not be saved; defaults still apply.")
        applied, detail = apply_priority_to_running()
        if applied is None:
            info("J.A.R.V.I.S. is not running — this applies at the next launch.")
        elif applied:
            ok(f"Running J.A.R.V.I.S. updated — {detail}.")
        else:
            warn(f"Could not raise the running instance's priority — {detail}.")
        report["performance"] = {"profile": profile, "applied": applied,
                                 "detail": detail}
        # Task Manager shows the class this profile produces; state it plainly
        # so "is the priority really set?" is answerable without guessing.
        pid = _lock_pid()
        current_fn = getattr(perf, "current_priority", None)
        if pid is not None and _pid_alive(pid) and callable(current_fn):
            current = current_fn(pid)
            if current:
                info(f"pid {pid} runs at '{current.replace('_', ' ')}' priority "
                     "(Task Manager ▸ Details ▸ Base priority).")
    except Exception as exc:
        warn(f"CPU profile unavailable: {type(exc).__name__}: {exc}")

    info("Step 4/5 — Windows graphics & performance suite")
    try:
        applied_win, win_detail = apply_windows_performance(confirm=True)
        report["windows"] = {"applied": applied_win, "detail": win_detail}
        if not applied_win and win_detail not in ("skipped", "Windows-only"):
            warn(f"Windows tuning left unchanged — {win_detail}.")
        elif win_detail == "Windows-only":
            info("Windows-only step — skipped on this platform.")
    except Exception as exc:
        warn(f"Windows tuning unavailable: {type(exc).__name__}: {exc}")
        report["windows"] = {"applied": False, "detail": str(exc)}

    info("Step 5/5 — what is in effect now")
    try:
        _print_windows_tuning()
    except Exception as exc:
        warn(f"Tuning state unavailable: {type(exc).__name__}: {exc}")

    rule()
    info("Undo the Windows changes with [u]; re-tune the CPU profile with [p]; "
         "see host health with [j].")
    return report


# ── L. Launch ────────────────────────────────────────────────────────────────

_LAUNCH_RUNTIME_CACHE: bool | None = None


def _launch_runtime_complete() -> bool | None:
    """Can the venv JARVIS will use import every hard requirement?

    Returns ``None`` when the probe itself failed, so the caller stays silent
    rather than claiming dependencies are missing. The import list and probe
    come from start_jarvis.py - the same authority the launcher uses - so this
    check can never drift from what the launch actually requires.
    """
    global _LAUNCH_RUNTIME_CACHE
    if _LAUNCH_RUNTIME_CACHE is not None:
        return _LAUNCH_RUNTIME_CACHE
    try:
        sys.path.insert(0, str(ROOT))
        import start_jarvis as _sj
        vpy = _venv_python()
        if not Path(vpy).is_file():
            _LAUNCH_RUNTIME_CACHE = False
        else:
            probe = _sj.probe_interpreter(vpy, _sj.HARD_IMPORTS, timeout=25.0)
            _LAUNCH_RUNTIME_CACHE = bool(probe.complete)
    except Exception:
        _LAUNCH_RUNTIME_CACHE = None
    return _LAUNCH_RUNTIME_CACHE


def _ensure_launch_ready() -> None:
    """Offer to repair what would make this launch fail, before it starts.

    Two things are worth a prompt: the Python packages the app cannot import,
    and the Gemini key that voice and chat require. Everything else degrades.
    Declining is allowed - the launch continues and the in-app setup screen can
    still fix it. Never raises: a pre-check must not be able to block a launch.
    """
    try:
        if _launch_runtime_complete() is False:
            warn("The Python packages J.A.R.V.I.S needs are not installed.")
            if _launch_confirm(
                    "Install all missing Python dependencies now?\n\n"
                    "This happens once and can take a few minutes.", True):
                _gui_message("J.A.R.V.I.S will install its Python packages now. "
                             "This can take a few minutes.")
                install_python_deps()

        if not _gemini_key():
            warn("No Gemini API key is configured (.env / config/api_keys.json).")
            if _launch_confirm(
                    "The Gemini API key is required for voice and chat.\n\n"
                    "Configure it now?", True):
                if _no_console():
                    if not _open_key_setup():
                        _gui_message(
                            "Could not open the key setup window.\n\n"
                            "Run:  python setup_jarvis.py --keys")
                else:
                    configure_keys("core")
                    _ensure_env()
    except Exception as exc:
        warn(f"Launch pre-check skipped: {exc}")


def launch() -> int:
    section("Launch")
    if not START.is_file():
        bad("start_jarvis.py is missing — cannot launch")
        return 1
    # Detect missing packages/keys and offer to fix them BEFORE the core starts.
    _ensure_launch_ready()
    auto_fix_gemini_before_launch()
    info("Starting J.A.R.V.I.S…")
    try:
        if _IS_WIN:
            cmd = [sys.executable, str(START)]
            if _no_console():
                cmd.append("--console-ui")   # preserve the VBS splash path
            subprocess.Popen(cmd, cwd=str(ROOT))
            ok("J.A.R.V.I.S. is starting in a separate process")
            return 0
        return subprocess.call([sys.executable, str(START)], cwd=str(ROOT))
    except Exception as exc:
        bad(f"Launch failed: {exc}")
        return 1


# ── 9. Diagnose & repair ─────────────────────────────────────────────────────

def diagnose(auto_fix: bool = False) -> dict:
    """Report providers and engines, then offer to install what is missing.

    With ``auto_fix`` (or a "y" answer to the prompt) it runs
    :func:`install_all_missing`, so the report is the start of a repair rather
    than a dead end. Returns a small summary dict.
    """
    section("Diagnostics")
    check_python()
    check_pip()
    check_node()

    rows = _feature_rows()
    missing = [r for r in rows if not r.get("ready")]
    ready = len(rows) - len(missing)
    if rows:
        ok(f"optional feature packages ready: {ready}/{len(rows)}")
    else:
        warn("Feature probe unavailable until the Python dependencies are installed.")

    rule()
    if missing:
        warn(f"{len(missing)} item(s) still missing:")
        for row in missing:
            warn(f"  {row['feature']}: {row['detail']}  ->  {row['fix']}")
    else:
        ok("Everything the project probes for is installed.")

    configured = _count_configured_keys()
    info(f"API keys stored: {configured}  (choose [6] Test connections to verify)")

    if missing and (auto_fix or _confirm(
            f"Install all {len(missing)} missing item(s) now?", default=False)):
        install_all_missing()
    return {"ready": ready, "missing": len(missing)}


# ── 10. Auto heal ──────────────────────────────────────────────────────────
#
# "It fixes all bugs or errors" is a promise no installer can honestly make, so
# Auto Heal covers every failure this project can actually detect and repair on
# its own: the interpreter and pip, the .venv and its packages, the optional
# feature packages, a damaged api_keys.json, Gemini, SQLite health, stale locks
# and crash markers, the Node/browser runtime, and cache/bytecode bloat.
# Anything it cannot fix is reported with the exact manual step instead of
# being hidden — a heal pass never lies about what is still broken.

#: The single dedicated cache/temp home (see .cache/README.md).
CACHE_DIR = ROOT / ".cache"
CACHE_SUBDIRS = ("pytest-tmp", "temp", "models", "quarantine")

#: Directories the cache purge must never walk: installed runtimes, vendored
#: tools, user data and the cache home itself.
_CACHE_SKIP_DIRS = {".venv", "venv", "env", "node_modules", ".git",
                    "UI-TARS", "models", "data", ".cache"}


def _cache_dir(*parts: str) -> Path:
    """Return (and create) a path inside the one dedicated cache home."""
    path = CACHE_DIR.joinpath(*parts)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return path


def _tree_size(path: Path) -> int:
    """Bytes held by a file or directory tree (never raises)."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except Exception:
        return 0


def _latest_config_backup() -> Path | None:
    """The newest api_keys/settings backup, or None when there is none."""
    try:
        backups = [p for p in SETTINGS_BACKUP_DIR.glob("*.json") if p.is_file()]
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    return backups[0] if backups else None


def _seed_config_from_env() -> dict:
    """API-key fields present in .env, shaped like api_keys.json."""
    env = _read_env()
    data: dict = {}
    for field, _label, _secret, _url in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS:
        value = env.get(field.upper())
        if value:
            data[field] = value
    return data


def _heal_python_runtime() -> tuple[bool, str]:
    """Check the interpreter, then bootstrap pip when it is missing."""
    if not check_python():
        return False, f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required"
    if check_pip():
        return True, "Python and pip are ready"
    info("pip is missing — bootstrapping it with ensurepip…")
    _run([sys.executable, "-m", "ensurepip", "--upgrade"], timeout=300)
    if check_pip():
        return True, "pip reinstalled with ensurepip"
    return False, "pip could not be bootstrapped (repair Python manually)"


def _heal_python_packages() -> tuple[bool, str]:
    """Create .venv when absent, then reinstall requirements if PyQt6 is missing."""
    if not ensure_venv():
        return False, "the .venv could not be created"
    vpy = _venv_python()
    probe = _run([vpy, "-c", "import PyQt6"], timeout=30)
    if probe and probe.returncode == 0:
        return True, "core packages are present in .venv"
    info("Core packages are incomplete — reinstalling requirements.txt…")
    if install_python_deps():
        return True, "requirements.txt reinstalled"
    return False, "requirements.txt could not be installed"


def _heal_optional_features() -> tuple[bool | None, str]:
    """Install every optional feature package the project probes for."""
    rows = _feature_rows()
    if not rows:
        return None, "feature probe unavailable (fix the core packages first)"
    missing = [r for r in rows
               if not r.get("ready") and r.get("module") != "node"]
    if not missing:
        return True, "every optional feature package is present"
    info(f"{len(missing)} optional package(s) missing — installing…")
    failed: list[str] = []
    for row in missing:
        installed, sentence = _install_feature(row["feature"])
        (ok if installed else warn)(sentence)
        if not installed:
            failed.append(str(row["feature"]))
    if failed:
        return False, "still missing: " + ", ".join(failed)
    return True, f"{len(missing)} optional package(s) installed"


def _heal_config() -> tuple[bool, str]:
    """Make api_keys.json readable with the right shapes, and mirror .env."""
    _ensure_env()
    original = CONFIG_FILE.is_file()
    raw = ""
    if original:
        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8")
        except Exception:
            raw = ""

    data: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            data = {}

    source = ""
    if not data:
        backup = _latest_config_backup()
        if backup is not None:
            try:
                restored = json.loads(backup.read_text(encoding="utf-8"))
                if isinstance(restored, dict) and restored:
                    data, source = restored, f"backup {backup.name}"
            except Exception:
                pass
    if not data:
        seeded = _seed_config_from_env()
        if seeded:
            data, source = seeded, ".env"
    if not data:
        data, source = {}, "defaults"

    changed = not original or bool(source)
    if not isinstance(data.get("features"), dict):
        data["features"] = {}
        changed = True
    if not str(data.get("assistant_name", "") or "").strip():
        data["assistant_name"] = "JARVIS"
        changed = True

    # Fill blanks from .env (the shell launches read both, so drift is a real
    # source of "the key is set but JARVIS says it is missing").
    env = _read_env()
    for field, _label, _secret, _url in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS:
        if str(data.get(field, "") or "").strip():
            continue
        value = env.get(field.upper(), "")
        if value:
            data[field] = value
            changed = True

    if changed and not _write_config(data):
        return False, f"could not write {CONFIG_FILE.name}"

    gemini = str(data.get("gemini_api_key", "") or "").strip()
    _sync_gemini_env(gemini, _read_env())

    if changed:
        detail = "api_keys.json recreated"
        if source:
            detail += f" from {source}"
        return True, detail
    return True, "api_keys.json is valid and .env is in sync"


def _heal_gemini() -> tuple[bool, str]:
    """Repair google-genai, the stored key, and verify it live."""
    return fix_gemini(verify=True, announce=False)


def _heal_databases() -> tuple[bool, str]:
    """quick_check every SQLite store; compact healthy ones, quarantine damage."""
    dbs = sorted((ROOT / "data").glob("*.db"))
    if not dbs:
        return True, "no SQLite databases to verify"
    damaged: list[str] = []
    compacted = 0
    for db in dbs:
        try:
            conn = sqlite3.connect(str(db), timeout=5.0)
            try:
                verdict = conn.execute("PRAGMA quick_check;").fetchone()
            finally:
                conn.close()
            problem = str(verdict[0]) if verdict else "unknown"
        except Exception as exc:
            damaged.append(f"{db.name} ({type(exc).__name__})")
            continue
        if problem.strip().lower() != "ok":
            copy = _cache_dir("quarantine") / f"{db.name}.{int(time.time())}"
            try:
                shutil.copy2(db, copy)
                damaged.append(f"{db.name} damaged (copy in .cache/quarantine)")
            except Exception:
                damaged.append(f"{db.name} damaged")
            continue
        if _compact_sqlite_db(db) > 0:
            compacted += 1
    if damaged:
        return False, "needs attention: " + "; ".join(damaged)
    detail = f"{len(dbs)} database(s) healthy"
    if compacted:
        detail += f", {compacted} compacted"
    return True, detail


def _heal_crashes() -> tuple[bool, str]:
    """Clear stale locks and archive crash markers (inspect_crashes, no prompts)."""
    report = inspect_crashes(auto_fix=True)
    fixes = report.get("fixes_applied") or []
    if report.get("healthy"):
        return True, (f"{len(fixes)} repair(s) applied" if fixes
                      else "no locks or crash markers to clear")
    return False, f"{len(report.get('issues') or [])} issue(s) still open"


def _heal_node_runtime() -> tuple[bool | None, str]:
    """Repair the Agent TARS Node runtime when Node itself is present."""
    if not shutil.which("node") or not shutil.which("npm"):
        return None, (f"Node.js {NODE_MIN_TEXT}+ not installed "
                      f"(optional: Agent TARS automation)")
    version = _version_of("node", "--version")
    if _parse_node_version(version) < NODE_MIN:
        return None, (f"Node.js {version or '?'} is older than {NODE_MIN_TEXT} "
                      f"(optional: Agent TARS automation)")
    if _node_modules_ready():
        return True, f"Node runtime ready ({version or 'node'})"
    info("node_modules is incomplete — walking the npm repair ladder…")
    if install_node_deps():
        return True, "Node runtime reinstalled"
    return False, "npm could not repair node_modules"


def _heal_playwright() -> tuple[bool | None, str]:
    """Install the Playwright Chromium binary when the package is present."""
    vpy = _venv_python()
    py = vpy if Path(vpy).is_file() else sys.executable
    probe = _run([py, "-m", "playwright", "--version"], timeout=20)
    if not probe or probe.returncode != 0:
        return None, "playwright package not installed (optional: browser automation)"
    if _playwright_chromium_ready():
        return True, "Chromium is already downloaded"
    if install_playwright_browser():
        return True, "Chromium downloaded"
    return False, "Chromium could not be downloaded"


def _playwright_chromium_ready() -> bool:
    """True when a Playwright Chromium build is on disk in either cache root."""
    roots = []
    if _IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            roots.append(Path(local) / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        try:
            if root.is_dir() and any(p.name.startswith("chromium")
                                     for p in root.iterdir()):
                return True
        except Exception:
            continue
    return False


def _heal_laya() -> tuple[bool | None, str]:
    """Repair Laya's runtime packages when its checkpoint is present."""
    model_dir, missing = _laya_model_dir()
    if model_dir is None:
        return None, "laya-multilingual/ not found (optional: local decision AI)"
    if missing:
        return False, ("the checkpoint is incomplete: " + ", ".join(missing)
                       + " — restore it from the Laya release")
    ready, detail = _laya_import_probe()
    if ready:
        return True, f"Laya runtime ready ({detail})"
    info(f"Laya runtime is missing ({detail}) — installing the two packages.")
    if _install_laya_packages():
        return True, "Laya runtime packages installed"
    return False, "Laya runtime packages could not be installed"


def _heal_rea() -> tuple[bool | None, str]:
    """Repair the pinned REA npx cache when its Node runtime is usable."""
    if not shutil.which("node") or not shutil.which("npx"):
        return None, "Node.js/npx not installed (optional: REA MCP server)"
    version = _version_of("node", "--version")
    if not _rea_node_supported(version):
        return None, (f"Node.js {version or '?'} is unsupported by REA "
                      f"(needs {REA_NODE_GATE_TEXT}; optional)")
    if _rea_cache_dirs(REA_VERSION):
        return True, f"REA {REA_VERSION} is cached"
    info(f"REA {REA_VERSION} is not cached — pre-warming it now.")
    good, detail = _rea_prewarm()
    if good:
        return True, f"REA {REA_VERSION} installed ({detail})"
    return False, f"REA could not be prepared: {detail}"


def _heal_caches() -> tuple[bool, str]:
    """Purge regenerable bytecode/temp output and keep the single .cache home."""
    for sub in CACHE_SUBDIRS:
        _cache_dir(sub)

    freed = 0
    removed = 0

    # 1. Stale test/scratch dirs at the project root belong in .cache/.
    for name in (".pytest_cache", ".pytest_tmp", ".pytest_tmp_hv",
                 ".pytest_tmp_voice"):
        path = ROOT / name
        if path.exists():
            freed += _tree_size(path)
            shutil.rmtree(path, ignore_errors=True)
            removed += 1

    # 2. In-tree __pycache__ and loose .pyc/.pyo (Python recreates them).
    for root_dir, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _CACHE_SKIP_DIRS]
        if os.path.basename(root_dir) == "__pycache__":
            for name in files:
                try:
                    freed += (Path(root_dir) / name).stat().st_size
                except Exception:
                    pass
            removed += 1
            dirs[:] = []
            shutil.rmtree(root_dir, ignore_errors=True)
            continue
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                target = Path(root_dir) / name
                try:
                    freed += target.stat().st_size
                    target.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass

    if removed:
        return True, (f"{removed} cache item(s) removed "
                      f"({round(freed / (1024 * 1024), 2)} MB)")
    return True, "no stale cache or bytecode to remove"


#: (key, label, repair) for one Auto Heal pass. ``None`` from a repair means the
#: step was skipped because its optional component is not installed.
AUTO_HEAL_STEPS: tuple[tuple[str, str, object], ...] = (
    ("runtime",   "Python runtime & pip",         _heal_python_runtime),
    ("packages",  "Virtualenv & Python packages", _heal_python_packages),
    ("features",  "Optional feature packages",    _heal_optional_features),
    ("config",    "Config & API key file",        _heal_config),
    ("gemini",    "Gemini engine",                _heal_gemini),
    ("databases", "SQLite databases",             _heal_databases),
    ("crashes",   "Stale locks & crash markers",  _heal_crashes),
    ("node",      "Node runtime (Agent TARS)",    _heal_node_runtime),
    ("rea",       "REA MCP server (npm)",         _heal_rea),
    ("laya",      "Laya AI decision model",       _heal_laya),
    ("browser",   "Playwright Chromium",          _heal_playwright),
    ("cache",     "Cache & bytecode",             _heal_caches),
)


def auto_heal(*, steps=None) -> dict:
    """One key: diagnose every known failure and repair what can be repaired.

    Each step returns ``(fixed, detail)`` where ``fixed`` is True (healed),
    False (still broken — the detail names the manual fix) or None (skipped
    because an optional component is not installed). The summary separates
    those three outcomes so "Node isn't installed" never reads as a failure.
    """
    section("Auto heal  — one pass over every known failure")
    chosen = list(AUTO_HEAL_STEPS if steps is None else steps)
    healed: list[str] = []
    skipped: list[str] = []
    unresolved: list[str] = []
    for index, (key, label, repair) in enumerate(chosen, start=1):
        info(f"[{index}/{len(chosen)}] {label}")
        try:
            fixed, detail = repair()
        except Exception as exc:
            fixed, detail = False, f"{type(exc).__name__}: {exc}"
        entry = f"{label}: {detail}"
        if fixed is True:
            ok(entry)
            healed.append(entry)
        elif fixed is None:
            info(entry)
            skipped.append(entry)
        else:
            warn(entry)
            unresolved.append(entry)

    print()
    rule()
    if unresolved:
        warn(f"Auto heal finished — {len(unresolved)} item(s) still need attention.")
    else:
        ok("Auto heal finished — everything it can check is healthy.")
    if skipped:
        info(f"{len(skipped)} optional item(s) skipped (not installed).")
    print("  " + faint(f"healed {len(healed)}  ·  skipped {len(skipped)}  ·  "
                       f"open {len(unresolved)}"))

    return {
        "healthy": not unresolved,
        "healed": healed,
        "skipped": skipped,
        "unresolved": unresolved,
    }


# ── packaging: a .exe that carries its saved data ────────────────────────────

#: Folders that hold J.A.R.V.I.S.'s own saved state. They are bundled into the
#: build so the executable starts with its configuration, memory and assets
#: instead of blank. The project folder itself is never moved or deleted.
DATA_FOLDERS = ("config", "data", "memory", "assets")

#: Where a build is written. dist/ holds only build output, so packaging can
#: never overwrite something the user cares about.
DIST_DIR = "dist"
WORK_DIR = "build/pyinstaller"
SPEC_DIR = "build"

#: Files a reset empties, after they have been backed up.
RESET_BACKUP_FILES = ("config/api_keys.json", "memory/long_term.json",
                      "data/brain_memory.db", "data/conversations.db",
                      "data/memory/growth.json", "data/mcp/servers.json")

#: Directories a reset clears completely: caches, logs and build leftovers.
RESET_CLEAR_DIRS = ("data/cache", "data/logs", WORK_DIR)


def _get_target_python() -> str:
    """Return the interpreter best suited to run JARVIS and packaging tools."""
    # 1. Probe sys.executable first
    try:
        r = _run([sys.executable, "-c", "import PyQt6"], timeout=10)
        if r and r.returncode == 0:
            return sys.executable
    except Exception:
        pass
    # 2. Check if start_jarvis.py resolved an interpreter
    try:
        if START.is_file():
            r = _run([sys.executable, str(START), "--check"], timeout=15)
            if r and r.returncode == 0:
                for line in (r.stdout or "").splitlines():
                    if "Chosen" in line and "(" in line and ")" in line:
                        cand = line.split("(", 1)[1].split(")", 1)[0].strip()
                        if cand and Path(cand).is_file():
                            return cand
    except Exception:
        pass
    # 3. Check 'python' on PATH
    py_cmd = shutil.which("python")
    if py_cmd:
        try:
            r = _run([py_cmd, "-c", "import PyQt6"], timeout=10)
            if r and r.returncode == 0:
                return py_cmd
        except Exception:
            pass
    return sys.executable


def build_pyinstaller_args(name: str, *, mode: str = "onedir",
                           windowed: bool = True, icon: str = "",
                           data_folders: tuple = DATA_FOLDERS,
                           entry: str = "main.py", distpath: str = DIST_DIR,
                           workpath: str = WORK_DIR,
                           specpath: str = SPEC_DIR,
                           python_exe: str | None = None) -> list:
    """The exact PyInstaller command for one build (pure, so tests can read it)."""
    py = python_exe or sys.executable
    args = [py, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--name", str(name), "--distpath", str(distpath),
            "--workpath", str(workpath), "--specpath", str(specpath),
            "--onefile" if str(mode) == "onefile" else "--onedir"]
    if windowed:
        args.append("--windowed")
    if icon:
        args += ["--icon", str(icon)]
    args += ["--paths", str(ROOT)]
    for h in ("core", "core.stability", "core.startup_optimizer",
              "core.maintenance", "observability", "capabilities",
              "PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets",
              "sqlite3", "json"):
        args += ["--hidden-import", h]
    for folder in data_folders:
        path = ROOT / folder
        if path.exists():
            args += ["--add-data", f"{path}{os.pathsep}{folder}"]
    # The optional native extension ships as a single file — never the whole
    # native/ tree (that holds the multi-GB cargo target dir). Resolved from
    # the CURRENT ROOT so callers that point ROOT elsewhere stay exact.
    native_pyd = Path(ROOT) / "native" / NATIVE_PYD.name
    if native_pyd.is_file():
        args += ["--hidden-import", "jarvis_native",
                 "--add-data", f"{native_pyd}{os.pathsep}native"]
    args.append(str(ROOT / entry))
    return args


def _compact_sqlite_db(path: Path) -> int:
    """Run VACUUM and PRAGMA optimize on an SQLite database. Return bytes saved."""
    if not path.is_file():
        return 0
    size_before = path.stat().st_size
    if size_before < 4096:
        return 0
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("VACUUM;")
        conn.execute("PRAGMA optimize;")
        conn.close()
        size_after = path.stat().st_size
        return max(0, size_before - size_after)
    except Exception:
        return 0


def shrink_workspace(aggressive: bool = False) -> dict:
    """Reduce repository folder size: clean builds, caches, bytecode, oversized logs,
    and compact SQLite databases."""
    section("Compress / Shrink  ·  reduce folder size & reclaim disk space")
    bytes_freed = 0
    cleaned_items: list[str] = []

    # 1. Build and cache directories
    junk_dirs = [
        ROOT / "build",
        ROOT / ".pytest_cache",
        ROOT / ".pytest_tmp",
        ROOT / ".freebuff",
        ROOT / ".kilo",
        ROOT / "data" / "cache",
    ]
    if aggressive:
        junk_dirs.append(ROOT / "dist")

    for jd in junk_dirs:
        if jd.is_dir():
            try:
                dir_size = sum(f.stat().st_size for f in jd.rglob("*") if f.is_file())
                shutil.rmtree(jd, ignore_errors=True)
                bytes_freed += dir_size
                cleaned_items.append(f"Directory: {jd.name} ({round(dir_size / (1024*1024), 2)} MB)")
            except Exception:
                pass
    (ROOT / "data" / "cache").mkdir(parents=True, exist_ok=True)

    # 2. Python bytecode __pycache__ and *.pyc (excluding .venv, venv, node_modules)
    pyc_count = 0
    pyc_bytes = 0
    skip_dirs = {".venv", "venv", "env", "node_modules", ".git"}
    for root_dir, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if os.path.basename(root_dir) == "__pycache__":
            for f in files:
                fp = Path(root_dir) / f
                try:
                    sz = fp.stat().st_size
                    pyc_bytes += sz
                    pyc_count += 1
                except Exception:
                    pass
            try:
                shutil.rmtree(root_dir, ignore_errors=True)
            except Exception:
                pass
        else:
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    fp = Path(root_dir) / f
                    try:
                        sz = fp.stat().st_size
                        pyc_bytes += sz
                        pyc_count += 1
                        fp.unlink(missing_ok=True)
                    except Exception:
                        pass
    if pyc_count > 0:
        bytes_freed += pyc_bytes
        cleaned_items.append(f"Bytecode: {pyc_count} .pyc files ({round(pyc_bytes / (1024*1024), 2)} MB)")

    # 3. Compact SQLite databases
    db_freed = 0
    data_dir = ROOT / "data"
    if data_dir.is_dir():
        for db_file in data_dir.glob("*.db"):
            saved = _compact_sqlite_db(db_file)
            if saved > 0:
                db_freed += saved
                cleaned_items.append(f"Compacted {db_file.name} (saved {round(saved / 1024, 1)} KB)")
    bytes_freed += db_freed

    # 4. Truncate oversized crash log and clean old logs
    crash_log = ROOT / "data" / "logs" / "jarvis_crash.log"
    if crash_log.is_file():
        try:
            sz = crash_log.stat().st_size
            if sz > 100 * 1024:
                lines = crash_log.read_text(encoding="utf-8", errors="replace").splitlines()
                kept_lines = lines[-200:] if len(lines) > 200 else lines
                crash_log.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
                new_sz = crash_log.stat().st_size
                freed_log = max(0, sz - new_sz)
                bytes_freed += freed_log
                cleaned_items.append(f"Trimmed jarvis_crash.log (saved {round(freed_log / 1024, 1)} KB)")
        except Exception:
            pass

    log_dir = ROOT / "data" / "logs"
    if log_dir.is_dir():
        for old_log in log_dir.glob("*.log.prev"):
            try:
                sz = old_log.stat().st_size
                old_log.unlink(missing_ok=True)
                bytes_freed += sz
                cleaned_items.append(f"Removed {old_log.name}")
            except Exception:
                pass

    # 5. Git garbage collection
    if (ROOT / ".git").is_dir() and shutil.which("git"):
        r = _run(["git", "gc", "--prune=now", "--quiet"], timeout=60)
        if r and r.returncode == 0:
            cleaned_items.append("Git repository garbage collected & packed")

    # 6. Report
    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    if cleaned_items:
        for item in cleaned_items:
            ok(f"Cleaned: {item}")
        good(f"Shrink complete — freed ~{mb_freed} MB of space.")
    else:
        ok("Workspace is already compact — no reclaimable files found.")

    return {
        "bytes_freed": bytes_freed,
        "mb_freed": mb_freed,
        "cleaned_items": cleaned_items,
    }


def inspect_crashes(auto_fix: bool = False) -> dict:
    """Inspect run_jarvis crash causes, diagnose root causes, and apply fixes."""
    section("Crash Inspection & Diagnostic  ·  run_jarvis stability check")
    issues: list[dict] = []
    fixes: list[str] = []

    # 1. Check data/logs/jarvis_crash.log
    crash_log = ROOT / "data" / "logs" / "jarvis_crash.log"
    crash_count = 0
    recent_errors: list[str] = []
    if crash_log.is_file():
        try:
            content = crash_log.read_text(encoding="utf-8", errors="replace")
            crash_count = content.count("Windows fatal exception") + content.count("Traceback (most recent call last)")
            if crash_count > 0:
                lines = content.strip().splitlines()
                recent_errors = [line.strip() for line in lines[-20:] if line.strip()]
        except Exception:
            pass

    if crash_count > 0:
        issues.append({
            "source": "jarvis_crash.log",
            "count": crash_count,
            "details": recent_errors[-3:] if recent_errors else ["Crash dumps detected in log"],
        })
        warn(f"Found {crash_count} crash event(s) recorded in {crash_log.name}")
        for err_line in recent_errors[-5:]:
            print("    " + faint(err_line))
    else:
        ok(f"{crash_log.name}: No active crash dumps found.")

    # 2. Check jarvis_startup_error.log
    startup_err = ROOT / "jarvis_startup_error.log"
    if startup_err.is_file() and startup_err.stat().st_size > 0:
        err_snippet = startup_err.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        issues.append({
            "source": "jarvis_startup_error.log",
            "count": len(err_snippet),
            "details": err_snippet[-3:],
        })
        warn(f"Startup errors detected in {startup_err.name}:")
        for line in err_snippet[-3:]:
            print("    " + red(line))
    else:
        ok("Startup error log: Clean (no startup errors recorded).")

    # 3. Check for stale runtime lock
    lock_file = ROOT / "data" / "runtime" / "jarvis.lock"
    stale_lock = False
    if lock_file.is_file():
        try:
            pid_str = lock_file.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                running = False
                if _IS_WIN:
                    r = _run(["tasklist", "/FI", f"PID eq {pid}"], timeout=5)
                    running = bool(r and str(pid) in (r.stdout or ""))
                else:
                    try:
                        os.kill(pid, 0)
                        running = True
                    except OSError:
                        running = False
                if not running:
                    stale_lock = True
                    warn(f"Stale lock file found: PID {pid} is no longer running.")
                    issues.append({"source": "lock", "detail": f"Stale lock for PID {pid}"})
                else:
                    info(f"JARVIS is currently running with PID {pid}.")
        except Exception:
            pass

    # 4. Check critical modules
    opt_module = ROOT / "core" / "startup_optimizer.py"
    if not opt_module.is_file():
        warn("Missing core/startup_optimizer.py — causes ImportError on launch.")
        issues.append({"source": "module", "detail": "Missing core/startup_optimizer.py"})
    else:
        ok("Startup optimizer: core/startup_optimizer.py verified.")

    # 5. Check Python environment & PyQt6
    target_py = _get_target_python()
    r = _run([target_py, "-c", "import PyQt6; import core.stability"], timeout=10)
    if not r or r.returncode != 0:
        warn(f"Target Python ({target_py}) failed to import PyQt6 or core.stability.")
        issues.append({"source": "import", "detail": "Failed importing PyQt6 or core.stability"})
    else:
        ok(f"Target Python ({Path(target_py).name}): PyQt6 and core modules import cleanly.")

    # 6. Check OpenMP duplicate lib guard
    kmp = os.environ.get("KMP_DUPLICATE_LIB_OK")
    if kmp != "TRUE":
        info("KMP_DUPLICATE_LIB_OK is guarded at runtime in core/stability.py.")

    # Auto-fix handling
    if issues and (auto_fix or _confirm("Apply automatic repairs for detected issues now?", default=True)):
        if stale_lock and lock_file.is_file():
            try:
                lock_file.unlink(missing_ok=True)
                fixes.append("Removed stale runtime lock file")
                ok("Removed stale jarvis.lock.")
            except Exception as e:
                warn(f"Could not remove lock file: {e}")

        if not opt_module.is_file() and (ROOT / "startup_optimizer.py").is_file():
            try:
                shutil.copy2(ROOT / "startup_optimizer.py", opt_module)
                fixes.append("Restored core/startup_optimizer.py from root")
                ok("Restored core/startup_optimizer.py.")
            except Exception as e:
                warn(f"Could not restore core/startup_optimizer.py: {e}")

        if crash_log.is_file() and crash_count > 0:
            try:
                archive = crash_log.with_name(f"jarvis_crash_archived_{int(time.time())}.log")
                shutil.copy2(crash_log, archive)
                crash_log.write_text("", encoding="utf-8")
                fixes.append(f"Archived crash log to {archive.name} and cleared active log")
                ok(f"Archived previous crash log ({crash_count} entries).")
            except Exception as e:
                warn(f"Could not archive crash log: {e}")

        if startup_err.is_file() and startup_err.stat().st_size > 0:
            try:
                startup_err.unlink(missing_ok=True)
                fixes.append("Cleared jarvis_startup_error.log")
                ok("Cleared jarvis_startup_error.log.")
            except Exception:
                pass

        good(f"Repairs applied: {len(fixes)} fix(es) completed.")
    elif not issues:
        good("System is clean and fully optimized — no crash blockers detected.")

    return {
        "crashes_found": crash_count,
        "stale_lock": stale_lock,
        "issues": issues,
        "fixes_applied": fixes,
        "healthy": len(issues) == 0 or len(fixes) >= len(issues),
    }


def _terminate_running_jarvis() -> bool:
    """Ensure no running JARVIS processes hold locks on databases or logs."""
    lock_file = ROOT / "data" / "runtime" / "jarvis.lock"
    terminated = False
    if lock_file.is_file():
        try:
            pid_str = lock_file.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                if _IS_WIN:
                    _run(["taskkill", "/F", "/PID", str(pid)], timeout=5)
                else:
                    try:
                        os.kill(pid, 9)
                    except Exception:
                        pass
                terminated = True
        except Exception:
            pass
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
    return terminated


def reset_plan(root: Path | None = None) -> dict:
    """What a reset would back up and clear — computed without touching anything."""
    base = Path(root or ROOT)
    vault = base / "Jarvis Obsidian Vault" / "Memory"
    pycaches: list[str] = []
    skip_dirs = {".venv", "venv", "env", "node_modules", ".git"}
    for r_dir, dirs, _files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if os.path.basename(r_dir) == "__pycache__":
            pycaches.append(r_dir)

    return {
        "backup_files": [str(base / p) for p in RESET_BACKUP_FILES],
        "clear_dirs": [str(base / p) for p in RESET_CLEAR_DIRS],
        "pycache": pycaches,
        "vault_memory": str(vault) if vault.is_dir() else "",
    }


def _pyinstaller_ready(python_exe: str | None = None) -> bool:
    py = python_exe or _get_target_python()
    r = _run([py, "-m", "PyInstaller", "--version"], timeout=60)
    return bool(r and r.returncode == 0)


def build_executable(name: str = "", *, mode: str = "",
                     windowed: bool | None = None,
                     onefile: bool = False) -> bool:
    """Build the executable, keeping the project folder exactly as it is."""
    section("Convert to .exe  ·  a Jarvis OS executable that keeps its data")
    info("The project folder is never moved or deleted — output goes to dist/.")
    if not ENTRY.is_file():
        bad(f"{ENTRY.name} is missing — cannot build.")
        return False

    if not name:
        name = ask("  Name of the app [Jarvis OS] > ", "Jarvis OS").strip()
        name = name or "Jarvis OS"
    if not mode:
        mode = "onefile" if (onefile or _confirm(
            "  Single .exe file? (slower first start, one file)",
            False)) else "onedir"
    if windowed is None:
        windowed = not _confirm("  Show a console window while it runs "
                                "(debugging only)?", False)

    icon = ""
    default_icon = ROOT / "assets" / "jarvis.ico"
    if default_icon.is_file():
        if _confirm(f"  Use {default_icon.name} as the icon?", True):
            icon = str(default_icon)
    else:
        icon = ask("  Icon file (blank for none) > ", "").strip()

    target_py = _get_target_python()
    if not _pyinstaller_ready(target_py):
        warn(f"PyInstaller is not installed for {target_py}.")
        if not _confirm("  Install PyInstaller now with pip?", True):
            bad("A build needs PyInstaller — nothing was changed.")
            return False
        r = _run([target_py, "-m", "pip", "install", "pyinstaller"],
                 timeout=900)
        if not r or r.returncode != 0:
            bad("PyInstaller could not be installed — see the pip output above.")
            return False
        good("PyInstaller installed")

    wp = ROOT / WORK_DIR
    if wp.exists():
        try:
            shutil.rmtree(wp, ignore_errors=True)
        except Exception:
            pass

    args = build_pyinstaller_args(name, mode=mode, windowed=windowed, icon=icon, python_exe=target_py)
    info("Running: " + " ".join(args[1:]))
    r = _run_with_progress(args, label=f"Building {name}", timeout=3600)
    if not r or r.returncode != 0:
        bad("The build reported a problem — your project folder is untouched.")
        return False
    exe = (ROOT / DIST_DIR / f"{name}.exe" if str(mode) == "onefile"
           else ROOT / DIST_DIR / name / f"{name}.exe")
    if not exe.is_file():
        candidates = list((ROOT / DIST_DIR).glob(f"*{name}*"))
        if not candidates:
            bad("Build completed but the output executable was not found.")
            return False
        good(f"Built: {candidates[0]}")
    else:
        size_mb = round(exe.stat().st_size / (1024 * 1024), 1)
        good(f"Built: {exe} ({size_mb} MB)")
    print(dim("  The executable carries config/, data/, memory/ and assets/ with it."))
    return True


def _fresh_config() -> dict:
    """The configuration a data reset leaves behind.

    Everything else falls back to the shipped defaults on read; Fish Audio and
    the microphone DSP are seeded explicitly so a fresh install knows its model,
    voice, Opus output and real microphone filters before the user ever opens
    Settings, and a later save round-trips the exact values the engines use.
    """
    from voice.fish_defaults import (
        FISH_DEFAULT_ENDPOINT,
        FISH_DEFAULT_FORMAT,
        FISH_DEFAULT_LATENCY,
        FISH_DEFAULT_MODEL,
        FISH_DEFAULT_VOICE,
    )

    fish = {
        "apiKey": "",
        "endpoint": FISH_DEFAULT_ENDPOINT,
        "model": FISH_DEFAULT_MODEL,
        "voice": FISH_DEFAULT_VOICE,
        "format": FISH_DEFAULT_FORMAT,
        "latency": FISH_DEFAULT_LATENCY,
    }
    return {
        "fish_audio_api_key": "",
        "fish_audio_endpoint": fish["endpoint"],
        "fish_audio_model_id": fish["model"],
        "fish_audio_voice_id": fish["voice"],
        "fish_audio_format": fish["format"],
        "fish_audio_latency": fish["latency"],
        # Microphone input processing (Settings ▸ VOICE ▸ MICROPHONE INPUT,
        # applied by voice/mic_dsp.py). Seeded so a fresh install starts with
        # the same real filters the panel shows.
        "input_device": "",
        "mic_gain_percent": 100,
        "mic_noise_suppression": True,
        "mic_noise_suppression_amount": 60,
        "mic_noise_gate": False,
        "mic_noise_gate_db": -45,
        "mic_compressor": False,
        "mic_compressor_amount": 50,
        "mic_limiter": True,
        "mic_limiter_ceiling_db": -1.0,
        "mic_monitoring": False,
        "voice": {"providers": {"fishAudio": dict(fish)}},
    }


def reset_data(*, yes: bool = False) -> bool:
    """Back up, then return J.A.R.V.I.S. to a fresh start."""
    section("Reset data  ·  fresh start (everything is backed up first)")
    planned = reset_plan()
    print("  Backed up:")
    for path in planned["backup_files"]:
        if Path(path).exists():
            print("    " + faint(Path(path).name))
    if ENV_FILE.is_file():
        print("    " + faint(".env (stored credentials)"))
    if planned["vault_memory"]:
        print("    " + faint("vault Memory/ notes (archived, never deleted)"))
    print("  Cleared:")
    print("    " + faint("API keys (.env & config) · memory · conversations · caches · logs · temp"))
    print("  Kept:")
    print("    " + faint("the project source and your vault notes"))
    print()
    if not yes:
        if ask("  Type RESET to confirm > ", "").strip().upper() != "RESET":
            info("Nothing was changed.")
            return False

    # 0) Stop any running instance holding file locks
    _terminate_running_jarvis()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = ROOT / "config" / "backups" / f"reset_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    backed = 0
    for path in planned["backup_files"]:
        source = Path(path)
        if not source.exists():
            continue
        try:
            shutil.copy2(source, dest / source.name)
            backed += 1
        except Exception as exc:
            warn(f"{source.name} could not be backed up: {exc}")
        for ext in ("-wal", "-shm"):
            sidecar = Path(str(source) + ext)
            if sidecar.is_file():
                try:
                    shutil.copy2(sidecar, dest / sidecar.name)
                except Exception:
                    pass

    if ENV_FILE.is_file():
        try:
            shutil.copy2(ENV_FILE, dest / ".env")
            backed += 1
        except Exception:
            pass

    if planned["vault_memory"]:
        try:
            shutil.copytree(planned["vault_memory"], dest / "vault-Memory",
                            dirs_exist_ok=True)
        except Exception as exc:
            warn(f"the vault memory notes could not be archived: {exc}")

    # 1) Fresh API keys in config/api_keys.json — seeded with the shipped Fish
    # Audio defaults so the first launch already has the verified voice.
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(_fresh_config(), indent=4), encoding="utf-8")
    except Exception as exc:
        bad(f"the configuration could not be reset: {exc}")
        return False

    # 2) Fresh .env file — reset to template or blank
    try:
        if ENV_EXAMPLE.is_file():
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    except Exception as exc:
        warn(f".env could not be reset: {exc}")

    # 3) Memory stores & databases (everything backed up + sidecars)
    for path in planned["backup_files"]:
        target = Path(path)
        if target.name == "api_keys.json":
            continue
        try:
            target.unlink(missing_ok=True)
        except Exception:
            warn(f"{target.name} could not be removed")
        for ext in ("-wal", "-shm"):
            sidecar = Path(str(target) + ext)
            try:
                sidecar.unlink(missing_ok=True)
            except Exception:
                pass

    data_dir = ROOT / "data"
    if data_dir.is_dir():
        for db in data_dir.glob("*.db*"):
            try:
                db.unlink(missing_ok=True)
            except Exception:
                pass

    # 4) Caches, logs, build leftovers, bytecode
    for path in planned["clear_dirs"]:
        p = Path(path)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        if p.name == "cache":
            p.mkdir(parents=True, exist_ok=True)
        elif p.name == "logs":
            p.mkdir(parents=True, exist_ok=True)
            try:
                (p / ".gitkeep").write_text("", encoding="utf-8")
            except Exception:
                pass

    for path in planned["pycache"]:
        try:
            shutil.rmtree(Path(path), ignore_errors=True)
        except Exception:
            pass

    skip_dirs = {".venv", "venv", "env", "node_modules", ".git"}
    for root_dir, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                try:
                    (Path(root_dir) / f).unlink(missing_ok=True)
                except Exception:
                    pass

    # 5) J.A.R.V.I.S.-owned temp files in workspace and system temp
    for temp_pattern in ("temp_*.py", "temp_*.json", "*.tmp"):
        for f in ROOT.glob(temp_pattern):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    temp_root = Path(os.environ.get("TEMP") or "/tmp")
    for pattern in ("jarvis*", "JARVIS*"):
        for target in temp_root.glob(pattern):
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except Exception:
                pass

    lock_file = ROOT / "data" / "runtime" / "jarvis.lock"
    try:
        lock_file.unlink(missing_ok=True)
    except Exception:
        pass

    good(f"Reset complete — {backed} file(s) backed up to {dest}")
    print(dim("  Start J.A.R.V.I.S. and it will ask for a fresh API key."))
    return True


# ── extra menu actions ───────────────────────────────────────────────────────

def wipe_venv() -> bool:
    """Delete the local .venv folder so the next [2] starts completely clean."""
    section("Wipe virtual environment (.venv/)")
    if not VENV.is_dir():
        warn(f"{VENV.name}/ does not exist — nothing to delete.")
        return True
    if not _confirm(
        f"Delete  {VENV}/  and all installed packages?",
        default=False,
    ):
        info("Cancelled — .venv/ was not touched.")
        return False
    info(f"Removing  {VENV.name}/  …")
    try:
        shutil.rmtree(VENV)
        ok(f"{VENV.name}/ deleted.  Run [2] to reinstall into a fresh environment.")
        return True
    except Exception as exc:
        bad(f"Could not delete {VENV.name}/: {exc}")
        return False


def update_python_deps() -> bool:
    """Upgrade every package in the venv to the latest compatible version."""
    section("Update Python packages  (.venv/ — upgrade to latest)")
    if not REQUIREMENTS.is_file():
        bad(f"requirements.txt missing — cannot upgrade ({REQUIREMENTS})")
        return False
    if not ensure_venv():
        return False
    vpy = _venv_python()
    info("Upgrading pip inside the venv…")
    _run([vpy, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], timeout=120)
    info("Upgrading all packages from requirements.txt to latest versions…")
    r = _run_with_progress(
        [
            vpy, "-m", "pip", "install",
            "--upgrade",
            "-r", str(REQUIREMENTS),
            "--disable-pip-version-check",
        ],
        label="Upgrading packages", timeout=3600,
        env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    good = bool(r and r.returncode == 0)
    (ok if good else bad)(
        "All packages upgraded successfully." if good
        else "Upgrade finished with errors — check the output above."
    )
    return good


def install_ffmpeg() -> bool:
    """Install FFmpeg via winget (Windows only)."""
    section("Install FFmpeg")
    if shutil.which("ffmpeg"):
        ok("FFmpeg is already installed and on PATH.")
        return True
    if not _IS_WIN:
        bad("Automatic FFmpeg install is only supported on Windows via winget.")
        info("Install FFmpeg from https://ffmpeg.org/download.html")
        return False
    if not shutil.which("winget"):
        bad("winget is not available — install FFmpeg from https://ffmpeg.org/download.html")
        return False
    info("Installing FFmpeg via winget…")
    r = _run_with_progress(
        [
            "winget", "install", "--id", "Gyan.FFmpeg",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        label="Installing FFmpeg", timeout=1200,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("FFmpeg installed. Reopen this terminal so it lands on PATH.")
    else:
        bad("winget could not install FFmpeg.")
        info("Download from https://ffmpeg.org/download.html and add to PATH manually.")
    return good


def install_git() -> bool:
    """Install Git via winget (Windows only)."""
    section("Install Git")
    if shutil.which("git"):
        ok("Git is already installed and on PATH.")
        return True
    if not _IS_WIN:
        bad("Automatic Git install is only supported on Windows via winget.")
        info("Install Git from https://git-scm.com/downloads")
        return False
    if not shutil.which("winget"):
        bad("winget is not available — install Git from https://git-scm.com/downloads")
        return False
    info("Installing Git for Windows via winget…")
    r = _run_with_progress(
        [
            "winget", "install", "--id", "Git.Git",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        label="Installing Git", timeout=1200,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("Git installed. Reopen this terminal for it to appear on PATH.")
    else:
        bad("winget could not install Git.")
        info("Download from https://git-scm.com/downloads and run the installer.")
    return good


def open_project_folder() -> None:
    """Open the J.A.R.V.I.S. project root in the system file explorer."""
    section("Open project folder")
    info(f"Opening  {ROOT}  in the file explorer…")
    try:
        if _IS_WIN:
            subprocess.Popen(["explorer", str(ROOT)], **_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ROOT)])
        else:
            subprocess.Popen(["xdg-open", str(ROOT)])
        ok("File explorer opened.")
    except Exception as exc:
        bad(f"Could not open folder: {exc}")


def view_logs() -> None:
    """Print the tail of the most recent log files to the console."""
    section("View logs  (last 40 lines each)")
    log_sources = [
        ROOT / "data" / "logs" / "dependency_install.log",
        ROOT / "setup_error.log",
        ROOT / "jarvis_startup_error.log",
        ROOT / "data" / "logs" / "jarvis.log",
    ]
    found_any = False
    for log_path in log_sources:
        if not log_path.is_file() or log_path.stat().st_size == 0:
            continue
        found_any = True
        print()
        print("  " + gold(f"── {log_path.name} ") + faint(str(log_path)))
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-40:]
            for line in tail:
                print("    " + faint(line))
        except Exception as exc:
            bad(f"  Could not read {log_path.name}: {exc}")
    if not found_any:
        info("No log files found yet — run J.A.R.V.I.S. first.")


# ── the interactive menu (two levels: a compact main menu + submenus) ────────
#
# The flat 23-row list read as a wall of options. The wizard now shows a
# compact main menu — Quick setup, three everyday groups, the three operator
# tools, Build and Launch — and every group opens a small submenu with its own
# rows. The keyboard model is unchanged (↑/↓ or W/S move, Enter runs, the
# letter/number shortcuts still work); B or Esc goes back from a submenu, Q
# exits from the main menu. A key may legitimately repeat across levels ([5]
# is Speed doctor on the main menu and API keys inside Keys & providers): the
# OPEN VIEW always wins — see _lookup_choice.

#: Main menu rows: (key, title, description, submenu-id | callable | None=exit).
MAIN_MENU: list[tuple[str, str, str, object]] = [
    ("1", "Quick setup",        "keys → packages → Gemini → Node → browser → test", _quick_setup),
    ("h", "Auto heal",          "one pass: every known failure diagnosed & fixed", auto_heal),
    ("2", "Install & update",   "Python · Node · Rust · Playwright · REA · Laya · FFmpeg · Git", "install-menu"),
    ("3", "Keys & providers",   "configure · test · fix Gemini · authorize", "keys-menu"),
    ("4", "Diagnose & fix",     "environment · crashes · logs · project folder", "diag-menu"),
    ("5", "Speed doctor",       "find & fix what slows replies down", speed_doctor),
    ("6", "Latency report",     "last session's measured boot & reply timings", latency_report),
    ("7", "Settings editor",    "model · voice · wake · Laya · VAD", edit_settings),
    ("8", "Clean & maintain",   "optimize · shrink · reset data", "maintain-menu"),
    ("9", "Build .exe",         "bundle Jarvis OS with its saved data", build_executable),
    ("l", "Launch J.A.R.V.I.S.", "hand over to start_jarvis.py", launch),
    ("0", "Exit",               "", None),
]

#: Submenu rows: same shape, the 4th element is always the action callable.
SUBMENUS: dict[str, list[tuple[str, str, str, object]]] = {
    "install-menu": [
        ("p", "Install Python",      "auto-install Python 3.13 via winget", install_python_runtime),
        ("2", "Python packages",     "requirements.txt → isolated .venv/", install_python_deps),
        ("n", "Native Rust backend", "optional: build jarvis_native (release)", build_native_extension),
        ("u", "Update packages",     "pip upgrade all .venv packages", update_python_deps),
        ("v", "Wipe .venv",          "delete .venv/ for a clean reinstall", wipe_venv),
        ("3", "Node runtime",        "Agent TARS browser + PC automation", install_node_deps),
        ("4", "Playwright browser",  "chromium binary for browser_action", install_playwright_browser),
        ("r", "REA (reverse engineering)", "pin, cache & verify the rea-agents MCP server", install_rea),
        ("a", "Laya AI runtime",     "install laya + transformers and make one real decision", install_laya),
        ("f", "Install FFmpeg",      "auto-install FFmpeg via winget", install_ffmpeg),
        ("g", "Install Git",         "auto-install Git via winget", install_git),
        ("7", "Install ALL missing", "every optional feature package", install_all_missing),
    ],
    "keys-menu": [
        ("5", "API keys",            "Gemini + others → .env / api_keys.json", _configure_keys_entry),
        ("6", "Test connections",    "verify each key with its provider", lambda: test_keys("all")),
        ("x", "Fix Gemini",          "repair google-genai install & verify key", _fix_gemini_entry),
        ("o", "Authorize providers", "open sign-up / key pages in browser", authorize_keys),
    ],
    "diag-menu": [
        ("h", "Auto heal (fix everything)", "one pass over every known failure", auto_heal),
        ("1", "Environment check",   "Python · pip · Node · git · FFmpeg · .venv", report_env),
        ("n", "Native backend status", "Rust extension: toolchain · build · import", native_backend_report),
        ("i", "Inspect & fix crash", "debug run_jarvis crashes & fix", inspect_crashes),
        ("9", "Diagnose & repair",   "report, then fix what is missing", diagnose),
        ("r", "Test REA",            "real MCP handshake with the pinned REA server", test_rea),
        ("y", "Test Laya AI",        "load the checkpoint and make one real decision", test_laya),
        ("t", "View logs",           "tail dependency_install, setup_error, jarvis logs", view_logs),
        ("d", "Open project folder", "open the JARVIS OS folder in Explorer", open_project_folder),
    ],
    "maintain-menu": [
        ("8", "Optimize",            "full pass: logs · cache · bytecode · CPU priority · Windows graphics", optimize),
        ("p", "CPU priority & threads", "choose the priority class and numerical thread budget", configure_performance),
        ("w", "Windows graphics & effects", "GPU preference + visual effects + Game DVR + power (reversible)", configure_windows_performance),
        ("u", "Undo Windows optimization", "restore the last snapshot and the graphics preference", undo_windows_performance),
        ("j", "System health report", "health score · top processes · current tuning state", windows_performance_report),
        ("s", "Compress / Shrink",   "reduce folder size · clean cache & builds", shrink_workspace),
        ("r", "Reset data",          "fresh start — keys, memory, cache (backed up)", reset_data),
    ],
}

SUBMENU_TITLES = {
    "install-menu":  "INSTALL & UPDATE",
    "keys-menu":     "KEYS & PROVIDERS",
    "diag-menu":     "DIAGNOSE & FIX",
    "maintain-menu": "CLEAN & MAINTAIN",
}

#: Flattened (key, title, description) for every row on both levels — the one
#: source tests and docs read.
MENU_ROWS: list[tuple[str, str, str]] = (
    [(k, t, d) for k, t, d, _x in MAIN_MENU]
    + [row[:3] for rows in SUBMENUS.values() for row in rows]
)
MENU_EXTRAS: list[tuple[str, str, str]] = []


def _lookup_choice(choice: str, view: str):
    """(kind, payload) for a typed key in the CURRENT view, else None.

    ``view`` is "" for the main menu or a SUBMENUS id. The open view wins, so
    a key that repeats across levels resolves to what the user is looking at."""
    if view:
        for key, _t, _d, action in SUBMENUS.get(view, []):
            if key == choice:
                return ("action", action)
    for key, _t, _d, target in MAIN_MENU:
        if key == choice:
            if isinstance(target, str):
                return ("submenu", target)
            if target is None:
                return ("exit", None)
            return ("action", target)
    return None


def _menu_line(key: str, title: str, desc: str, selected: bool = False) -> str:
    """Selected row: green ▌ bar, bright title. Others: faint, quiet."""
    inner = _BOX_W - 2
    edge = (_fg(L_CYAN) + "│" + _rst()) if _COLOR else "|"
    if not _COLOR:
        body = f"{'>' if selected else ' '} [{key}] {title:<22} {desc}"
    elif selected:
        body = (_fg(L_GREEN) + "▌" + _rst() + " " + _fg(L_GREEN) + f"\x1b[1m[{key}]" + _rst() + "  " +
                _fg(L_WHITE) + f"\x1b[1m{title:<22}" + _rst() + " " + _fg(L_GREEN) + desc + _rst())
    else:
        body = (" " + " " + _fg(L_CYAN) + f"[{key}]" + _rst() + "  " +
                _fg(_lerp(L_WHITE, L_FAINT, 0.35)) + f"{title:<22}" + _rst() + " " +
                _fg(L_FAINT) + desc + _rst())
    body = " " + body
    # clip the description if the row would overflow the frame
    while _vlen(body) > inner - 1 and desc:
        desc = desc[:-2]
        return _menu_line(key, title, desc.rstrip() + "…", selected) if not desc.endswith("…") else body
    return "  " + edge + _pad(body, inner) + edge


def render_menu(rows=None, selected: int = 0, title: str = "ACTIONS") -> str:
    """One menu level as a framed panel.

    ``rows`` may be full menu entries (key, title, desc, action) or plain
    (key, title, desc) rows; the no-argument form renders the main menu."""
    rows = (list(rows) if rows is not None
            else [(k, t, d) for k, t, d, _x in MAIN_MENU])
    rows = [(row[0], row[1], row[2]) for row in rows]
    lines = [_panel_top(title, "SELECT")]
    for index, (key, mtitle, desc) in enumerate(rows):
        lines.append(_menu_line(key, mtitle, desc, index == selected))
    lines += [
        _panel_bottom(),
        "",
        (" " + _fg(L_FAINT) + "  ↑ ↓ / W S  move    Enter  run    shortcut keys  [ ]"
                              "    B / Esc  back    Q  exit" + _rst())
        if _COLOR else "  up/down or W/S move, Enter run, B/Esc back, Q exit",
    ]
    return "\n".join(lines)


def menu() -> int:
    if not (sys.stdin and sys.stdin.isatty()):
        report_env()
        warn("No interactive terminal detected — run with a flag instead, e.g.")
        print(dim("    python setup_jarvis.py --check"))
        print(dim("    python setup_jarvis.py --all --yes     # install everything"))
        print(dim("    python setup_jarvis.py --launch"))
        return 0

    view = ""            # "" = main menu, else a SUBMENUS id
    selected = 0
    primed = False

    while True:
        rows = ([(k, t, d) for k, t, d, _x in MAIN_MENU] if not view
                else SUBMENUS[view])
        title = "ACTIONS" if not view else SUBMENU_TITLES.get(view, "ACTIONS")
        clear_screen()
        banner()
        if not primed:
            report_env()
            primed = True

        print(render_menu(rows, selected, title))
        choice = _read_menu_key()

        if choice == "up":
            selected = (selected - 1) % len(rows)
            continue
        if choice == "down":
            selected = (selected + 1) % len(rows)
            continue
        if choice in ("esc", "back", "b") or (choice == "q" and view):
            if view:
                view, selected = "", 0
            continue
        if choice in ("q", "0") and not view:
            print(_fg(L_FAINT) + "\n  ● SETUP COMPLETE  —  bye, Sir." + _rst())
            return 0
        if choice == "enter":
            choice = rows[selected][0]

        found = _lookup_choice(choice, view)
        if found is None:
            warn("Unknown choice. Use ↑/↓, W/S, a shortcut key, or B/Esc to go back.")
            continue
        kind, payload = found
        if kind == "submenu":
            view, selected = payload, 0
            continue
        if kind == "exit":
            print(_fg(L_FAINT) + "\n  ● SETUP COMPLETE  —  bye, Sir." + _rst())
            return 0

        clear_screen()
        banner()
        try:
            result = payload()
        except Exception as exc:
            bad(f"Action failed: {type(exc).__name__}: {exc}")
            result = None
        # Only a REAL int (launch's exit code) leaves the menu; a bool result
        # from a check/installer must never be mistaken for one.
        if isinstance(result, int) and not isinstance(result, bool):
            return int(result)
        pause("  Press Enter to return to the menu…")


# ── CLI ──────────────────────────────────────────────────────────────────────

USAGE = """\
python setup_jarvis.py                 interactive Setup & Control Center
python setup_jarvis.py --check         environment report only
python setup_jarvis.py --install       install / repair the Python dependencies
python setup_jarvis.py --node          install the Node runtime (Agent TARS)
python setup_jarvis.py --browser       install the Playwright chromium binary
python setup_jarvis.py --native        build the OPTIONAL Rust extension (release)
python setup_jarvis.py --native-status native backend report (toolchain · build · import)
python setup_jarvis.py --keys          configure API keys (.env + api_keys.json)
python setup_jarvis.py --authorize     open the provider sign-up / key pages
python setup_jarvis.py --test          test every stored key against its provider
python setup_jarvis.py --fix-gemini    repair Gemini: install google-genai into the
                                       interpreter JARVIS uses, check the key, verify live
python setup_jarvis.py --fix-gemini --launch
                                       repair Gemini, then start J.A.R.V.I.S.
python setup_jarvis.py --install-missing  install every dependency still absent
python setup_jarvis.py --auto-heal     one pass: diagnose & repair every known failure
python setup_jarvis.py --optimize      housekeeping (logs, cache, bytecode)
python setup_jarvis.py --shrink        remove build dirs, clean caches, vacuum DBs
python setup_jarvis.py --inspect-crash inspect & auto-fix run_jarvis crash causes
python setup_jarvis.py --diagnose      report providers and engines
python setup_jarvis.py --diagnose --yes  the same, auto-installing what is missing
python setup_jarvis.py --all --yes     keys + dependencies + node, non-interactive
python setup_jarvis.py --speed-doctor  audit what slows replies down
python setup_jarvis.py --speed-doctor --yes  the same, applying the fast profile
python setup_jarvis.py --latency-report  last session's measured boot & reply timings
python setup_jarvis.py --settings      edit behaviour settings (model, voice, wake, Laya)
python setup_jarvis.py --launch        start J.A.R.V.I.S.
python setup_jarvis.py --build-exe     build a Jarvis OS .exe with its saved data
python setup_jarvis.py --build-exe --name "Jarvis OS" --onefile
python setup_jarvis.py --reset --yes   back up, then reset to a fresh start
"""


def build_parser() -> argparse.ArgumentParser:
    """The flag form, for scripts and CI where a menu cannot be answered."""
    parser = argparse.ArgumentParser(
        prog="setup_jarvis.py",
        description="J.A.R.V.I.S. — Setup & Control Center",
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true",
                        help="report the environment and exit")
    parser.add_argument("--install", action="store_true",
                        help="install / repair the Python dependencies")
    parser.add_argument("--node", action="store_true",
                        help="install the Node runtime (Agent TARS)")
    parser.add_argument("--browser", action="store_true",
                        help="install the Playwright chromium binary")
    parser.add_argument("--native", action="store_true",
                        help="build/rebuild the optional native Rust extension "
                             "(jarvis_native) — the app also runs without it")
    parser.add_argument("--native-status", dest="native_status", action="store_true",
                        help="report the native Rust backend (toolchain, build, import)")
    parser.add_argument("--keys", action="store_true",
                        help="configure API keys")
    parser.add_argument("--group", default="core",
                        choices=("core", "voice", "extra", "all"),
                        help="which key group --keys collects (default: core)")
    parser.add_argument("--test", action="store_true",
                        help="test every stored API key against its provider")
    parser.add_argument("--fix-gemini", dest="fix_gemini", action="store_true",
                        help="repair Gemini: install google-genai into the "
                             "interpreter JARVIS uses, recover/check the stored "
                             "key, and verify it with a live probe")
    parser.add_argument("--authorize", action="store_true",
                        help="open each provider's sign-up / key page in a browser")
    parser.add_argument("--install-missing", "--fix", dest="install_missing",
                        action="store_true",
                        help="install every dependency still absent "
                             "(requirements + optional features + node)")
    parser.add_argument("--auto-heal", dest="auto_heal", action="store_true",
                        help="one pass: diagnose and repair every known "
                             "failure (runtime, packages, config, Gemini, "
                             "databases, locks, caches)")
    parser.add_argument("--optimize", action="store_true",
                        help="run housekeeping (logs, cache, bytecode)")
    parser.add_argument("--diagnose", action="store_true",
                        help="report providers and engines")
    parser.add_argument("--all", action="store_true",
                        help="keys, dependencies and node, in order")
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive: never prompt, take defaults")
    parser.add_argument("--launch", action="store_true",
                        help="start J.A.R.V.I.S.")
    parser.add_argument("--build-exe", dest="build_exe", action="store_true",
                        help="build a Jarvis OS executable that bundles its "
                             "saved data (config/, data/, memory/, assets/)")
    parser.add_argument("--name", default="Jarvis OS",
                        help="executable name for --build-exe (default: Jarvis OS)")
    parser.add_argument("--onefile", action="store_true",
                        help="--build-exe: one single .exe instead of a folder")
    parser.add_argument("--console", action="store_true",
                        help="--build-exe: keep a console window (debugging only)")
    parser.add_argument("--reset", action="store_true",
                        help="back up, then reset keys, memory and cache to a "
                             "fresh start")
    parser.add_argument("--shrink", action="store_true",
                        help="reduce folder size: clean build dirs, caches, "
                             "bytecode, and vacuum SQLite databases")
    parser.add_argument("--inspect-crash", dest="inspect_crash",
                        action="store_true",
                        help="inspect run_jarvis crash causes and auto-fix them")
    parser.add_argument("--speed-doctor", dest="speed_doctor", action="store_true",
                        help="audit the settings that slow replies down "
                             "(add --yes to apply the fast profile)")
    parser.add_argument("--latency-report", dest="latency_report",
                        action="store_true",
                        help="show the last session's measured boot & reply timings")
    parser.add_argument("--settings", action="store_true",
                        help="edit behaviour settings: model, voice, wake, Laya, VAD")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. No flag at all drops into the interactive menu.

    Order matters: keys first (so a later step that needs one has it), then the
    Python dependencies, then the Node runtime — the same order the menu's
    ESSENTIALS shortcut uses, and the same order a bare machine needs.
    """
    args = build_parser().parse_args(argv)
    banner()

    did_something = False

    if args.check:
        report_env()
        did_something = True

    if args.all:
        print(bold("\n  ESSENTIALS: keys → dependencies → Gemini → node → optimize"))
        configure_keys("all", non_interactive=bool(args.yes))
        install_python_deps()
        fix_gemini()
        install_node_deps()
        optimize()
        did_something = True

    if args.keys:
        configure_keys(args.group, non_interactive=bool(args.yes))
        did_something = True

    if args.install:
        install_python_deps()
        did_something = True

    if args.node:
        install_node_deps()
        did_something = True

    if args.browser:
        install_playwright_browser()
        did_something = True

    if args.native_status:
        native_backend_report()
        did_something = True

    if args.native:
        build_native_extension()
        did_something = True

    if args.test:
        test_keys(args.group)
        did_something = True

    if args.fix_gemini:
        fix_gemini()
        did_something = True

    if args.authorize:
        authorize_keys()
        did_something = True

    if args.install_missing:
        install_all_missing()
        did_something = True

    if args.auto_heal:
        auto_heal()
        did_something = True

    if args.optimize:
        optimize()
        did_something = True

    if args.diagnose:
        diagnose(auto_fix=bool(args.yes))
        did_something = True

    if args.build_exe:
        build_executable(args.name,
                         mode="onefile" if args.onefile else "onedir",
                         windowed=not args.console)
        did_something = True

    if args.reset:
        reset_data(yes=bool(args.yes))
        did_something = True

    if args.shrink:
        shrink_workspace()
        did_something = True

    if args.inspect_crash:
        inspect_crashes()
        did_something = True

    if args.speed_doctor:
        speed_doctor(apply=bool(args.yes))
        did_something = True

    if args.latency_report:
        latency_report()
        did_something = True

    if args.settings:
        edit_settings()
        did_something = True

    if args.launch:
        return launch()

    if not did_something:
        return menu()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())