"""External-application detection for plugin ``requires`` entries.

A media-editor integration must only appear when the editor is actually
installed — otherwise the model would offer a tool that can only fail, and the
user would see a plugin that claims a capability this machine does not have.

Detection is deliberately conservative and cheap: an executable on ``PATH`` or a
known install directory on Windows/macOS. It runs once per plugin scan and the
result is cached, so it costs nothing at dispatch time. Everything is best
effort — an unknown token is reported as missing rather than assumed present.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

__all__ = ["check", "locate", "satisfied", "satisfied_any", "describe",
           "known_tokens"]

_CACHE: dict[str, tuple[bool, str]] = {}

#: token -> (human label, CLI executable names, GUI install paths)
_CATALOG: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "ffmpeg": ("FFmpeg", ("ffmpeg",), ()),
    "obs": ("OBS Studio", ("obs64",), (
        r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        "/Applications/OBS.app")),
    "vlc": ("VLC media player", ("vlc",), (
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        "/Applications/VLC.app")),
    "davinci_resolve": ("DaVinci Resolve", (), (
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe",
        "/Applications/DaVinci Resolve/DaVinci Resolve.app")),
    "premiere": ("Adobe Premiere Pro", (), (
        r"C:\Program Files\Adobe\Adobe Premiere Pro 2024\Adobe Premiere Pro.exe",
        r"C:\Program Files\Adobe\Adobe Premiere Pro 2023\Adobe Premiere Pro.exe",
        "/Applications/Adobe Premiere Pro 2024/Adobe Premiere Pro 2024.app")),
    "after_effects": ("Adobe After Effects", (), (
        r"C:\Program Files\Adobe\Adobe After Effects 2024\Support Files\AfterFX.exe",
        "/Applications/Adobe After Effects 2024/Adobe After Effects 2024.app")),
    "photoshop": ("Adobe Photoshop", (), (
        r"C:\Program Files\Adobe\Adobe Photoshop 2024\Photoshop.exe",
        "/Applications/Adobe Photoshop 2024/Adobe Photoshop 2024.app")),
    "gimp": ("GIMP", ("gimp", "gimp-2.10"), (
        r"C:\Program Files\GIMP 2\bin\gimp-2.10.exe",
        "/Applications/GIMP.app")),
    "inkscape": ("Inkscape", ("inkscape",), (
        r"C:\Program Files\Inkscape\bin\inkscape.exe",
        "/Applications/Inkscape.app")),
    "krita": ("Krita", ("krita",), (
        r"C:\Program Files\Krita (x64)\bin\krita.exe",
        "/Applications/krita.app")),
    "audacity": ("Audacity", ("audacity",), (
        r"C:\Program Files\Audacity\Audacity.exe",
        "/Applications/Audacity.app")),
    "blender": ("Blender", ("blender",), (
        r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
        "/Applications/Blender.app")),
}


def known_tokens() -> list[str]:
    return sorted(_CATALOG)


def _label(token: str) -> str:
    entry = _CATALOG.get(token)
    return entry[0] if entry else token


def _probe(token: str) -> tuple[bool, str]:
    entry = _CATALOG.get(token)
    if entry is None:
        return False, f"{token} (unknown requirement)"
    label, exes, paths = entry
    for exe in exes:
        found = shutil.which(exe)
        if found:
            return True, f"{label} found ({found})"
    for raw in paths:
        try:
            if Path(os.path.expandvars(raw)).exists():
                return True, f"{label} found ({raw})"
        except Exception:
            continue
    return False, f"{label} is not installed"


def locate(token: str) -> str | None:
    """The executable/install path for *token*, or ``None``. Never raises."""
    entry = _CATALOG.get(str(token or "").strip().lower())
    if entry is None:
        return None
    _label_text, exes, paths = entry
    for exe in exes:
        found = shutil.which(exe)
        if found:
            return found
    for raw in paths:
        try:
            if Path(os.path.expandvars(raw)).exists():
                return raw
        except Exception:
            continue
    return None


def check(token: str) -> tuple[bool, str]:
    """Is *token* present on this machine? Cached, never raises."""
    key = str(token or "").strip().lower()
    if not key:
        return True, ""
    if key in _CACHE:
        return _CACHE[key]
    try:
        result = _probe(key)
    except Exception as exc:  # never let detection break a plugin scan
        result = (False, f"{key} could not be checked: {exc}")
    _CACHE[key] = result
    return result


def satisfied(requirements) -> tuple[bool, list[str]]:
    """``(all_present, missing_details)`` for a manifest's ``requires`` list."""
    missing: list[str] = []
    for token in (requirements or []):
        ok, detail = check(token)
        if not ok:
            missing.append(detail)
    return (not missing), missing


def satisfied_any(requirements) -> tuple[bool, list[str]]:
    """``(one_present, labels_when_none)`` for an ``any of`` requirement list."""
    tokens = [str(t).strip().lower() for t in (requirements or []) if str(t).strip()]
    if not tokens:
        return True, []
    labels = [_label(t) for t in tokens]
    for token in tokens:
        ok, _detail = check(token)
        if ok:
            return True, []
    return False, [f"one of: {', '.join(labels)}"]


def describe(requirements) -> str:
    """A one-line summary of what a plugin needs from this machine."""
    tokens = [str(t).strip().lower() for t in (requirements or []) if str(t).strip()]
    if not tokens:
        return ""
    return ", ".join(_label(t) for t in tokens)


def reset_cache() -> None:
    """Forget cached detections (used by tests and a manual REFRESH)."""
    _CACHE.clear()


# Keep the interpreter/platform import from looking unused in trimmed builds.
_ = sys.platform
