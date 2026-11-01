"""Read the text that is really on screen — without asking a model to guess.

Why this exists
---------------
The visual layer (``capabilities/uitars.py``) can *look* at a screen, but it
answers a different question: "where would I click for this instruction?" It
cannot reliably answer "what does this dialog say?" or "click the word
'Settings'". Both are ordinary desktop tasks, and both need real text plus real
coordinates.

Windows already ships an OCR engine (``Windows.Media.Ocr``). It needs no pip
package, no model download and no API key, so it is the default backend here.
The engine is reached through a short PowerShell WinRT script — the Python
``winrt`` bindings are not installed on this machine, and requiring them would
make the feature fail for everyone who did not install a second dependency.
When ``pytesseract`` *and* the Tesseract binary are present they are used as a
fallback, and the result always names the backend that actually ran.

What this module will not do
----------------------------
* It never invents coordinates. Every point comes from a word rectangle the
  engine returned for the captured frame.
* It never claims text is on screen when recognition failed — a missing engine,
  an empty frame and a timeout are three different, named outcomes.
* It does not click on its own except in :func:`_h_click_text`, which is an
  explicit action the model asked for and which reports the coordinates it used.

Coordinate spaces
-----------------
Capture happens in *physical screen pixels*; PyAutoGUI works in *pointer
coordinates*. ``capabilities.desktop.to_pointer`` already owns that conversion,
so every point returned here is already in pointer space and a caller can hand
it straight to ``mouse_click``. Word rectangles in the evidence stay in capture
space and say so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

__all__ = ["run", "status", "read_screen", "find_text"]

#: The WinRT OCR call, written for Windows PowerShell 5.1. Every asynchronous
#: WinRT operation is awaited through ``AsTask``; the JSON is the ONLY thing on
#: stdout so a caller can parse the last object even if a warning was printed.
_PS_OCR = r"""
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }
    [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime] | Out-Null
    $path = $args[0]
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {
        [pscustomobject]@{ ok = $false; error = 'no OCR language pack is installed' } | ConvertTo-Json -Compress
        exit 0
    }
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($line in $result.Lines) {
        $words = @()
        $x1 = [int]::MaxValue; $y1 = [int]::MaxValue; $x2 = 0; $y2 = 0
        foreach ($w in $line.Words) {
            $r = $w.BoundingRect
            $wx = [int]$r.X; $wy = [int]$r.Y; $ww = [int]$r.Width; $wh = [int]$r.Height
            $words += [pscustomobject]@{ t = $w.Text; x = $wx; y = $wy; w = $ww; h = $wh }
            if ($wx -lt $x1) { $x1 = $wx }
            if ($wy -lt $y1) { $y1 = $wy }
            if (($wx + $ww) -gt $x2) { $x2 = $wx + $ww }
            if (($wy + $wh) -gt $y2) { $y2 = $wy + $wh }
        }
        if ($words.Count -eq 0) { continue }
        $lines += [pscustomobject]@{
            text = $line.Text; x = $x1; y = $y1;
            w = ($x2 - $x1); h = ($y2 - $y1); words = $words
        }
    }
    [pscustomobject]@{
        ok = $true; language = $engine.RecognizerLanguage.LanguageTag;
        width = [int]$bitmap.PixelWidth; height = [int]$bitmap.PixelHeight;
        lines = $lines
    } | ConvertTo-Json -Depth 8 -Compress
} catch {
    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""

#: Availability probe. Separate from the OCR call so status is cheap and never
#: depends on a screenshot being possible.
_PS_PROBE = r"""
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {
        [pscustomobject]@{ ok = $false; error = 'no OCR language pack is installed' } | ConvertTo-Json -Compress
        exit 0
    }
    $languages = @()
    foreach ($lang in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
        $languages += $lang.LanguageTag
    }
    [pscustomobject]@{
        ok = $true; language = $engine.RecognizerLanguage.LanguageTag;
        languages = $languages
    } | ConvertTo-Json -Compress
} catch {
    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""

_SCRIPT_LOCK = threading.Lock()
_SCRIPT_PATHS: dict[str, Path] = {}
_STATUS_LOCK = threading.Lock()
_STATUS: dict | None = None
_STATUS_AT = 0.0
_STATUS_TTL = 600.0
_MAX_LINES = 80
_MAX_CHARS = 4000


# ── the PowerShell bridge ─────────────────────────────────────────────────────

def _powershell() -> str:
    """The Windows PowerShell executable, or "" when this is not Windows."""
    if sys.platform != "win32":
        return ""
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found:
        return found
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else ""


def _script_path(name: str, source: str) -> Path:
    """Write one PS1 script into the automation cache; reuse it afterwards."""
    with _SCRIPT_LOCK:
        cached = _SCRIPT_PATHS.get(name)
        if cached is not None and cached.is_file():
            return cached
        try:
            from capabilities import config
            base = Path(config.automation_dir()) / "ocr"
        except Exception:
            base = Path(__file__).resolve().parent.parent / "data" / "automation" / "ocr"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{name}.ps1"
        try:
            current = path.read_text(encoding="utf-8")
        except Exception:
            current = ""
        if current != source:
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(source, encoding="utf-8")
            os.replace(str(tmp), str(path))
        _SCRIPT_PATHS[name] = path
        return path


def _run_ps(script: str, argument: str = "", timeout: float = 30.0) -> dict:
    """Run one PS1 script and return its JSON object. Never raises."""
    exe = _powershell()
    if not exe:
        return {"ok": False, "error": "Windows OCR is only available on Windows"}
    try:
        path = _script_path(
            "windows_ocr_probe" if not argument else "windows_ocr", script)
        cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(path)]
        if argument:
            cmd.append(str(argument))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                   creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the OCR engine did not answer within {timeout:.0f}s"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
    payload = ""
    for line in reversed(stdout.splitlines()):
        if line.strip().startswith("{"):
            payload = line.strip()
            break
    if not payload:
        tail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        detail = tail.splitlines()[-1][:220] if tail else f"exit {completed.returncode}"
        return {"ok": False, "error": f"the OCR engine returned nothing ({detail})"}
    try:
        data = json.loads(payload)
    except Exception as exc:
        return {"ok": False, "error": f"the OCR engine returned unreadable JSON: {exc}"}
    return data if isinstance(data, dict) else {"ok": False, "error": "bad OCR payload"}


def _tesseract_ready() -> bool:
    try:
        import pytesseract  # noqa: F401
        return bool(shutil.which("tesseract"))
    except Exception:
        return False


# ── status ────────────────────────────────────────────────────────────────────

def status() -> dict:
    """Which OCR backend is really usable, probed (and cached for 10 minutes)."""
    global _STATUS, _STATUS_AT
    now = time.monotonic()
    with _STATUS_LOCK:
        if _STATUS is not None and (_STATUS_AT + _STATUS_TTL) > now:
            return dict(_STATUS)
    windows: dict = {"ok": False, "error": "not probed"}
    if sys.platform == "win32" and _powershell():
        windows = _run_ps(_PS_PROBE, timeout=25.0)
    if windows.get("ok"):
        out = {
            "available": True,
            "backend": "windows-ocr",
            "language": str(windows.get("language") or ""),
            "languages": list(windows.get("languages") or []),
            "tesseract": _tesseract_ready(),
            "detail": ("Windows OCR ready"
                       + (f" ({windows.get('language')})" if windows.get("language") else "")),
        }
    elif _tesseract_ready():
        out = {
            "available": True,
            "backend": "tesseract",
            "language": "",
            "languages": [],
            "tesseract": True,
            "detail": "Tesseract OCR ready (pytesseract)",
        }
    else:
        reason = str(windows.get("error") or "the Windows OCR engine is unavailable")
        out = {
            "available": False,
            "backend": "",
            "language": "",
            "languages": [],
            "tesseract": False,
            "detail": (f"Screen OCR is unavailable: {reason}. Install a Windows "
                       "OCR language pack (Settings ▸ Time & language ▸ Language) "
                       "or pip install pytesseract plus the Tesseract binary."),
        }
    with _STATUS_LOCK:
        _STATUS = dict(out)
        _STATUS_AT = now
    return out


# ── capture ───────────────────────────────────────────────────────────────────

def _region(args: dict) -> tuple[int, int, int, int] | None:
    """An optional x/y → to_x/to_y rectangle, in pointer coordinates."""
    x, y = args.get("x"), args.get("y")
    tx, ty = args.get("to_x"), args.get("to_y")
    if None in (x, y, tx, ty):
        return None
    try:
        x, y, tx, ty = int(x), int(y), int(tx), int(ty)
    except (TypeError, ValueError):
        return None
    if tx <= x or ty <= y:
        return None
    return (x, y, tx, ty)


def _capture(region: tuple[int, int, int, int] | None):
    """Capture the screen (or one rectangle) as PNG plus its measurements.

    Returns ``(png, info, origin)`` where *origin* is the capture-space pixel
    that the returned image's (0, 0) corresponds to.
    """
    from capabilities import desktop
    png, info = desktop.grab_png()
    if region is None:
        return png, info, (0, 0)
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("RGB")
        left = float(info.get("left") or 0.0)
        top = float(info.get("top") or 0.0)
        sx = float(info.get("scale_x") or 1.0) or 1.0
        sy = float(info.get("scale_y") or 1.0) or 1.0
        x1 = int(round(left + region[0] * sx))
        y1 = int(round(top + region[1] * sy))
        x2 = int(round(left + region[2] * sx))
        y2 = int(round(top + region[3] * sy))
        x1 = max(0, min(image.width - 1, x1))
        y1 = max(0, min(image.height - 1, y1))
        x2 = max(x1 + 1, min(image.width, x2))
        y2 = max(y1 + 1, min(image.height, y2))
        cropped = image.crop((x1, y1, x2, y2))
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        return buffer.getvalue(), info, (x1, y1)
    except Exception as exc:
        raise RuntimeError(f"the screen region could not be cropped: {exc}") from exc


# ── recognition ───────────────────────────────────────────────────────────────

def _windows_recognise(png: bytes) -> dict:
    """OCR one PNG through Windows.Media.Ocr. Returns normalised lines."""
    try:
        from capabilities import config
        folder = Path(config.automation_dir()) / "ocr"
    except Exception:
        folder = Path(__file__).resolve().parent.parent / "data" / "automation" / "ocr"
    folder.mkdir(parents=True, exist_ok=True)
    image_path = folder / f"frame-{os.getpid()}-{int(time.time() * 1000)}.png"
    try:
        image_path.write_bytes(png)
        data = _run_ps(_PS_OCR, str(image_path), timeout=45.0)
    finally:
        try:
            image_path.unlink()
        except OSError:
            pass
    if not data.get("ok"):
        return {"ok": False, "backend": "windows-ocr",
                "error": str(data.get("error") or "recognition failed")}
    lines: list[dict] = []
    for raw in data.get("lines") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        words = []
        for word in raw.get("words") or []:
            if not isinstance(word, dict):
                continue
            words.append({
                "t": str(word.get("t") or ""),
                "x": int(word.get("x") or 0), "y": int(word.get("y") or 0),
                "w": int(word.get("w") or 0), "h": int(word.get("h") or 0),
            })
        lines.append({
            "text": text,
            "x": int(raw.get("x") or 0), "y": int(raw.get("y") or 0),
            "w": int(raw.get("w") or 0), "h": int(raw.get("h") or 0),
            "words": words,
        })
    return {
        "ok": True,
        "backend": "windows-ocr",
        "language": str(data.get("language") or ""),
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
        "lines": lines,
    }


def _tesseract_recognise(png: bytes) -> dict:
    """OCR one PNG through pytesseract, normalised to the same shape."""
    try:
        import io

        import pytesseract
        from PIL import Image
        from pytesseract import Output
    except Exception as exc:
        return {"ok": False, "backend": "tesseract",
                "error": f"pytesseract is not usable: {exc}"}
    try:
        image = Image.open(io.BytesIO(png))
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception as exc:
        return {"ok": False, "backend": "tesseract",
                "error": f"Tesseract failed: {exc}"}
    grouped: dict[tuple, list[dict]] = {}
    count = len(data.get("text") or [])
    for i in range(count):
        text = str(data["text"][i] or "").strip()
        if not text:
            continue
        key = (data.get("block_num", [0] * count)[i],
               data.get("par_num", [0] * count)[i],
               data.get("line_num", [0] * count)[i])
        grouped.setdefault(key, []).append({
            "t": text,
            "x": int(data["left"][i]), "y": int(data["top"][i]),
            "w": int(data["width"][i]), "h": int(data["height"][i]),
        })
    lines: list[dict] = []
    for words in grouped.values():
        if not words:
            continue
        x1 = min(w["x"] for w in words)
        y1 = min(w["y"] for w in words)
        x2 = max(w["x"] + w["w"] for w in words)
        y2 = max(w["y"] + w["h"] for w in words)
        lines.append({
            "text": " ".join(w["t"] for w in words),
            "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1, "words": words,
        })
    width, height = getattr(image, "size", (0, 0))
    return {"ok": True, "backend": "tesseract", "language": "",
            "width": int(width), "height": int(height), "lines": lines}


def _recognise(png: bytes) -> dict:
    """Recognise one frame with the best available backend."""
    state = status()
    if not state.get("available"):
        return {"ok": False, "backend": "", "error": state.get("detail")
                or "no OCR backend is available"}
    if state.get("backend") == "windows-ocr":
        result = _windows_recognise(png)
        if result.get("ok"):
            return result
        if _tesseract_ready():
            fallback = _tesseract_recognise(png)
            if fallback.get("ok"):
                fallback["note"] = ("Windows OCR failed ("
                                    + str(result.get("error") or "unknown")
                                    + "); Tesseract read the frame instead.")
                return fallback
        return result
    return _tesseract_recognise(png)


# ── normalisation and matching ────────────────────────────────────────────────

def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _absolute(line: dict, origin: tuple[int, int]) -> dict:
    """A line's rectangles translated into capture-space coordinates."""
    ox, oy = origin
    words = [{"t": w["t"], "x": w["x"] + ox, "y": w["y"] + oy,
              "w": w["w"], "h": w["h"]} for w in line.get("words") or []]
    return {"text": line["text"], "x": line["x"] + ox, "y": line["y"] + oy,
            "w": line["w"], "h": line["h"], "words": words}


def _word_window(words: list[dict], query: str) -> dict | None:
    """The smallest run of words whose text contains *query*, or None."""
    wanted = _norm(query)
    if not wanted or not words:
        return None
    limit = len(wanted.split()) + 3
    for start in range(len(words)):
        for end in range(start + 1, min(len(words), start + limit) + 1):
            text = _norm(" ".join(str(w["t"]) for w in words[start:end]))
            if wanted in text:
                chosen = words[start:end]
                x1 = min(w["x"] for w in chosen)
                y1 = min(w["y"] for w in chosen)
                x2 = max(w["x"] + w["w"] for w in chosen)
                y2 = max(w["y"] + w["h"] for w in chosen)
                return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}
    return None


def _best_line(lines: list[dict], query: str) -> tuple[dict | None, float]:
    """The line that best matches *query*, plus a truthful confidence."""
    wanted = _norm(query)
    if not wanted:
        return None, 0.0
    tokens = [t for t in wanted.split() if t]
    best: tuple[float, int, dict | None] = (0.0, 0, None)
    for line in lines:
        text = _norm(line.get("text"))
        if not text:
            continue
        if wanted in text:
            score = 1.0
        else:
            hits = sum(1 for token in tokens if token in text)
            score = (hits / len(tokens)) * 0.85 if tokens else 0.0
        if score <= best[0]:
            continue
        # Prefer the shorter line when two lines score the same: it is the more
        # specific match and its centre is closer to the requested words.
        if score == best[0] and best[2] is not None and len(text) >= best[1]:
            continue
        best = (score, len(text), line)
    return best[2], round(best[0], 2)


def _locate(result: dict, query: str, info: dict,
            origin: tuple[int, int]) -> dict:
    """Resolve *query* to a pointer-space rectangle. Returns a result dict."""
    from capabilities import desktop
    lines = [_absolute(line, origin) for line in result.get("lines") or []]
    line, confidence = _best_line(lines, query)
    if line is None or confidence < 0.5:
        seen = "; ".join(str(l["text"])[:40] for l in lines[:6]) or "no text at all"
        return {
            "ok": False, "controller": result.get("backend") or "ocr",
            "reason": "text_not_found",
            "detail": (f"I could not find '{query}' on the screen. The frame "
                       f"contained: {seen}."),
            "evidence": {"query": query, "confidence": confidence,
                         "lines": len(lines)},
        }
    box = _word_window(line.get("words") or [], query) or {
        "x": line["x"], "y": line["y"], "w": line["w"], "h": line["h"]}
    cx = box["x"] + box["w"] / 2.0
    cy = box["y"] + box["h"] / 2.0
    px, py = desktop.to_pointer(cx, cy, info)
    # The same rectangle in pointer space, for a caller that wants to click it.
    left, top = desktop.to_pointer(box["x"], box["y"], info)
    right, bottom = desktop.to_pointer(box["x"] + box["w"],
                                       box["y"] + box["h"], info)
    return {
        "ok": True,
        "controller": result.get("backend") or "ocr",
        "detail": (f"Found '{line['text']}' on screen at ({px}, {py}) "
                   f"— {result.get('backend')} matched '{query}' with "
                   f"confidence {confidence:.2f}."),
        "evidence": {
            "query": query, "line": line["text"], "confidence": confidence,
            "point": [px, py], "rect": [left, top, right - left, bottom - top],
            "capture_rect": [box["x"], box["y"], box["w"], box["h"]],
            "backend": result.get("backend"),
            "language": result.get("language", ""),
        },
        "point": [px, py],
    }


# ── handlers ──────────────────────────────────────────────────────────────────

def _h_read_screen(args: dict) -> dict:
    state = status()
    if not state.get("available"):
        return {"ok": False, "controller": "ocr", "reason": "ocr_missing",
                "detail": state.get("detail") or "no OCR backend is available",
                "evidence": {"backends": state}}
    region = _region(args)
    try:
        png, info, origin = _capture(region)
    except Exception as exc:
        return {"ok": False, "controller": "ocr", "reason": "capture_failed",
                "detail": f"The screen could not be captured: {exc}"}
    result = _recognise(png)
    if not result.get("ok"):
        return {"ok": False, "controller": "ocr", "reason": "ocr_failed",
                "detail": ("The screen was captured but no text could be read: "
                           + str(result.get("error") or "unknown error")),
                "evidence": {"backend": result.get("backend")}}
    lines = [_absolute(line, origin) for line in result.get("lines") or []]
    if not lines:
        return {"ok": True, "controller": str(result.get("backend") or "ocr"),
                "detail": ("I read the screen and found no text at all. The "
                           "frame is not blank unless the window really is."),
                "evidence": {"lines": 0, "backend": result.get("backend"),
                             "width": result.get("width"),
                             "height": result.get("height")}}
    shown = lines[:_MAX_LINES]
    body = "\n".join(str(line["text"]) for line in shown)
    if len(body) > _MAX_CHARS:
        body = body[:_MAX_CHARS].rsplit("\n", 1)[0] + "\n…"
    extra = len(lines) - len(shown)
    header = (f"Screen text ({result.get('backend')}, "
              f"{result.get('width')}×{result.get('height')}, "
              f"{len(lines)} line(s)):")
    detail = header + "\n" + body + (f"\n… {extra} more line(s)" if extra else "")
    return {"ok": True, "controller": str(result.get("backend") or "ocr"),
            "detail": detail,
            "evidence": {"backend": result.get("backend"),
                         "language": result.get("language", ""),
                         "width": result.get("width"),
                         "height": result.get("height"),
                         "lines": len(lines),
                         "rects": [{"text": l["text"], "x": l["x"], "y": l["y"],
                                    "w": l["w"], "h": l["h"]} for l in lines[:40]]}}


def _h_find_text(args: dict) -> dict:
    query = str(args.get("query") or args.get("text") or "").strip()
    if not query:
        return {"ok": False, "controller": "ocr", "reason": "no_query",
                "detail": "find_text needs the text to look for."}
    state = status()
    if not state.get("available"):
        return {"ok": False, "controller": "ocr", "reason": "ocr_missing",
                "detail": state.get("detail") or "no OCR backend is available"}
    try:
        png, info, origin = _capture(_region(args))
    except Exception as exc:
        return {"ok": False, "controller": "ocr", "reason": "capture_failed",
                "detail": f"The screen could not be captured: {exc}"}
    result = _recognise(png)
    if not result.get("ok"):
        return {"ok": False, "controller": "ocr", "reason": "ocr_failed",
                "detail": ("The screen was captured but no text could be read: "
                           + str(result.get("error") or "unknown error"))}
    return _locate(result, query, info, origin)


def _h_click_text(args: dict) -> dict:
    located = _h_find_text(args)
    if not located.get("ok"):
        return located
    from capabilities import desktop
    point = located.get("point") or []
    if len(point) != 2:
        return {"ok": False, "controller": "ocr", "reason": "no_point",
                "detail": "The text was found but no clickable point was produced."}
    click = desktop.run("mouse_click", {"x": point[0], "y": point[1]})
    evidence = dict(located.get("evidence") or {})
    evidence["click"] = click.get("evidence") or {}
    if not click.get("ok"):
        return {"ok": False, "controller": "windows-ocr+pyautogui",
                "reason": "click_failed",
                "detail": (str(located.get("detail") or "")
                           + " The click itself was refused: "
                           + str(click.get("detail") or "unknown")),
                "evidence": evidence}
    return {"ok": True, "controller": "windows-ocr+pyautogui",
            "detail": (str(located.get("detail") or "")
                       + " I clicked that position."),
            "evidence": evidence}


def _h_wait_for_text(args: dict) -> dict:
    query = str(args.get("query") or args.get("text") or "").strip()
    if not query:
        return {"ok": False, "controller": "ocr", "reason": "no_query",
                "detail": "wait_for_text needs the text to wait for."}
    try:
        timeout = float(args.get("timeout_s") or 15)
    except (TypeError, ValueError):
        timeout = 15.0
    timeout = max(1.0, min(60.0, timeout))
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        last = _h_find_text(args)
        if last.get("ok"):
            last["detail"] = (f"'{query}' appeared on screen after "
                              f"{timeout - max(0.0, deadline - time.monotonic()):.1f}s. "
                              + str(last.get("detail") or ""))
            return last
        if time.monotonic() >= deadline:
            break
        time.sleep(0.6)
    return {"ok": False, "controller": "ocr", "reason": "timeout",
            "detail": (f"'{query}' did not appear on screen within "
                       f"{timeout:.0f}s. " + str(last.get("detail") or "")),
            "evidence": last.get("evidence") or {}}


_HANDLERS = {
    "read_screen": _h_read_screen,
    "find_text": _h_find_text,
    "click_text": _h_click_text,
    "wait_for_text": _h_wait_for_text,
}


# ── entry points ──────────────────────────────────────────────────────────────

def read_screen(args: dict | None = None) -> dict:
    """Read the screen's text (the module's public, non-registry entry point)."""
    return _h_read_screen(dict(args or {}))


def find_text(args: dict | None = None) -> dict:
    """Locate text on screen and return its pointer-space position."""
    return _h_find_text(dict(args or {}))


def run(action: str, args: dict) -> dict:
    """Execute one OCR action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action or "").strip())
    if handler is None:
        return {"ok": False, "controller": "ocr",
                "detail": f"'{action}' is not a screen-reading action."}
    try:
        return handler(dict(args or {})) or {
            "ok": False, "controller": "ocr", "detail": "No result."}
    except Exception as exc:
        return {"ok": False, "controller": "ocr", "reason": "crashed",
                "detail": f"Screen reading failed: {type(exc).__name__}: {exc}"}
