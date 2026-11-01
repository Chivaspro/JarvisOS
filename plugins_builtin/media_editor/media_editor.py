"""Built-in media-editor integration — only as real as this machine.

The manifest's ``requires_any`` means this plugin is not even offered unless a
media tool is installed. Every action then works through the detected tool
itself: ``editors`` lists what is present, ``open`` launches a real GUI editor
with the file, and ``probe`` runs ffprobe and reports the streams and duration
it printed. If the named editor is not installed the answer says so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

PLUGIN = {
    "name": "media_editor",
    "description": "Use the media editors installed on this PC.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "editors | open | probe"},
            "path": {"type": "STRING", "description": "Media file path."},
            "editor": {"type": "STRING", "description": "Editor token (optional)."},
        },
        "required": ["action"],
    },
}

#: GUI editors OPEN may launch. CLI-only tools (ffmpeg) are probe-only.
_GUI_TOKENS = ("davinci_resolve", "premiere", "after_effects", "photoshop",
               "gimp", "inkscape", "krita", "audacity", "blender", "obs", "vlc")
_ALL_TOKENS = ("ffmpeg", *_GUI_TOKENS)


def _requires():
    try:
        from app import plugin_requires
        return plugin_requires
    except Exception:
        return None


def _detected() -> list[tuple[str, str, str]]:
    """[(token, label, path)] for every installed tool, in a stable order."""
    requires = _requires()
    found: list[tuple[str, str, str]] = []
    for token in _ALL_TOKENS:
        path = ""
        if requires is not None:
            path = requires.locate(token) or ""
        else:
            path = shutil.which(token) or ""
        if path:
            found.append((token, token.replace("_", " ").title(), path))
    return found


def _action_editors() -> str:
    found = _detected()
    if not found:
        return ("No media editors or FFmpeg are installed on this machine, so "
                "there is nothing to open files with.")
    lines = ["Media tools found on this PC:"]
    lines.extend(f"• {label} — {path}" for _token, label, path in found)
    lines.append("")
    lines.append("GUI editors can be launched with action='open'; ffmpeg also "
                 "supports action='probe'.")
    return "\n".join(lines)


def _resolve_editor(settings: dict, requested: str) -> tuple[str, str]:
    """(token, path) for the editor to use, or ('', '') when none applies."""
    requires = _requires()
    candidates: list[str] = []
    if requested:
        candidates.append(requested.strip().lower())
    preferred = str(settings.get("default_editor") or "").strip().lower()
    if preferred:
        candidates.append(preferred)
    candidates.extend(_GUI_TOKENS)
    for token in candidates:
        if token not in _GUI_TOKENS:
            continue
        path = (requires.locate(token) if requires else shutil.which(token)) or ""
        if path:
            return token, path
    return "", ""


def _launch(path: str, target: str) -> tuple[bool, str]:
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # noqa: S606 - deliberate local UI action
            return True, ""
        subprocess.Popen([path, target])
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _action_open(parameters: dict, settings: dict) -> str:
    target = str(parameters.get("path") or "").strip().strip('"')
    if not target:
        return "Open needs a file path."
    if not os.path.exists(target):
        return f"There is no file at {target}, so nothing was opened."
    token, editor_path = _resolve_editor(settings, str(parameters.get("editor") or ""))
    if not editor_path:
        return ("No GUI media editor is installed, so the file was not opened. "
                "Install one (DaVinci Resolve, GIMP, VLC, …) or use probe for "
                "FFmpeg metadata.")
    ok, error = _launch(editor_path, target)
    if not ok:
        return f"Could not launch {token} ({editor_path}): {error}"
    return f"Opened {target} in {token} ({editor_path})."


def _action_probe(parameters: dict, settings: dict) -> str:
    target = str(parameters.get("path") or "").strip().strip('"')
    if not target:
        return "Probe needs a file path."
    if not os.path.exists(target):
        return f"There is no file at {target}, so nothing was probed."
    requires = _requires()
    ffprobe = shutil.which("ffprobe")
    if not ffprobe and requires is not None:
        ffmpeg_path = requires.locate("ffmpeg")
        if ffmpeg_path:
            candidate = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
            ffprobe = candidate if os.path.exists(candidate) else None
    if not ffprobe:
        return ("ffprobe is not available, so no media metadata could be read. "
                "Install FFmpeg to probe files.")
    timeout = int(settings.get("ffprobe_timeout") or 15)
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", target],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"ffprobe did not finish within {timeout}s for {target}."
    except Exception as exc:
        return f"ffprobe failed: {exc}"
    if proc.returncode != 0:
        return (f"ffprobe could not read {target}: "
                f"{(proc.stderr or '').strip()[:300] or 'no detail'}")
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return f"ffprobe returned output that could not be parsed for {target}."

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    lines = [f"Media info for {target}"]
    if fmt.get("format_long_name") or fmt.get("format_name"):
        lines.append(f"• Container: {fmt.get('format_long_name') or fmt.get('format_name')}")
    if fmt.get("duration"):
        try:
            lines.append(f"• Duration: {float(fmt['duration']):.2f} s")
        except Exception:
            lines.append(f"• Duration: {fmt['duration']}")
    if fmt.get("bit_rate"):
        try:
            lines.append(f"• Bit rate: {int(fmt['bit_rate']) / 1000:.0f} kbps")
        except Exception:
            pass
    for stream in streams:
        kind = stream.get("codec_type") or "?"
        codec = stream.get("codec_name") or "?"
        detail = [f"{kind} · {codec}"]
        if stream.get("width") and stream.get("height"):
            detail.append(f"{stream['width']}x{stream['height']}")
        if stream.get("sample_rate"):
            detail.append(f"{stream['sample_rate']} Hz")
        if stream.get("channels"):
            detail.append(f"{stream['channels']} ch")
        lines.append("• Stream: " + " · ".join(str(x) for x in detail))
    if len(lines) == 1:
        lines.append("• No stream information was reported.")
    return "\n".join(lines)


def run(parameters, settings=None, limits=None, logger=None) -> str:
    params = dict(parameters or {})
    settings = dict(settings or {})
    action = str(params.get("action") or "").strip().lower()
    if action in ("", "editors", "list"):
        return _action_editors()
    if action == "open":
        return _action_open(params, settings)
    if action == "probe":
        return _action_probe(params, settings)
    return ("Unknown action. Use 'editors' to list installed tools, 'open' to "
            "launch one with a file, or 'probe' to read FFmpeg metadata.")
