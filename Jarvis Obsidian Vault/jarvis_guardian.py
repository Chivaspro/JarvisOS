#!/usr/bin/env python3
"""Jarvis Guardian: a defensive, READ-ONLY system checker.

It reports what's running, listening and starting on *this* machine so Sir
(or Jarvis) can decide what to do next. It never kills a process, deletes a
file, or changes a setting. Any actual cleanup is a separate, confirmed step
-- see "Memory/Guardian/Malware Removal.md" in this vault.

Only ever run this on a machine you own or administer.

Usage:
  pip install psutil
  python jarvis_guardian.py scan
  python jarvis_guardian.py scan --json

Import:
  from jarvis_guardian import scan
"""
import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

SUSPICIOUS_PATH_HINTS = ("temp", "tmp", "appdata\\local\\temp", "downloads", "\\recycle", "/tmp", "/var/tmp")
RECENT_HOURS = 24
SENSITIVE_DIRS = {
    "Windows": [os.path.expandvars(r"%APPDATA%"), os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
                os.path.expandvars(r"%USERPROFILE%\Downloads")],
    "Darwin": [str(Path.home() / "Library/LaunchAgents"), str(Path.home() / "Downloads")],
    "Linux": ["/tmp", str(Path.home() / "Downloads"), str(Path.home() / ".config/autostart")],
}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def check_processes():
    if not psutil:
        return {"error": "psutil not installed. Run: pip install psutil"}
    findings = []
    for p in psutil.process_iter(["pid", "name", "exe", "cpu_percent"]):
        info = p.info
        exe = (info.get("exe") or "").lower()
        if exe and any(h in exe for h in SUSPICIOUS_PATH_HINTS):
            findings.append({"pid": info.get("pid"), "name": info.get("name"),
                              "exe": info.get("exe"), "reason": "running from a commonly-abused path"})
    return {"checked": True, "flagged": findings, "note": "A flag is not proof of malware -- "
            "many legitimate installers run from Temp. Verify each one before acting."}


def check_listening_ports():
    if not psutil:
        return {"error": "psutil not installed. Run: pip install psutil"}
    ports = []
    for c in _safe(lambda: psutil.net_connections(kind="inet"), []) or []:
        if c.status == "LISTEN":
            proc_name = None
            if c.pid:
                proc_name = _safe(lambda: psutil.Process(c.pid).name())
            ports.append({"port": c.laddr.port if c.laddr else None, "pid": c.pid, "process": proc_name})
    return {"checked": True, "listening": ports}


def check_startup_items():
    system = platform.system()
    items = []
    if system == "Windows":
        try:
            import winreg
            for root, path in ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                                (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run")):
                try:
                    with winreg.OpenKey(root, path) as key:
                        i = 0
                        while True:
                            try:
                                name, value, _ = winreg.EnumValue(key, i)
                                items.append({"name": name, "command": value})
                                i += 1
                            except OSError:
                                break
                except OSError:
                    continue
        except ImportError:
            items.append({"note": "winreg unavailable; check Task Scheduler and the Startup folder manually."})
    elif system == "Darwin":
        for d in ("/Library/LaunchAgents", "/Library/LaunchDaemons", str(Path.home() / "Library/LaunchAgents")):
            for f in _safe(lambda d=d: list(Path(d).glob("*.plist")), []) or []:
                items.append({"name": f.name, "path": str(f)})
    else:  # Linux
        for d in ("/etc/xdg/autostart", str(Path.home() / ".config/autostart")):
            for f in _safe(lambda d=d: list(Path(d).glob("*.desktop")), []) or []:
                items.append({"name": f.name, "path": str(f)})
    return {"checked": True, "items": items, "note": "Unfamiliar entries are worth investigating, not assuming malicious."}


def check_recent_files(hours=RECENT_HOURS):
    system = platform.system()
    cutoff = time.time() - hours * 3600
    recent = []
    for d in SENSITIVE_DIRS.get(system, []):
        p = Path(d)
        if not p.exists():
            continue
        for f in _safe(lambda p=p: list(p.rglob("*")), []) or []:
            if f.is_file() and _safe(lambda f=f: f.stat().st_mtime, 0) > cutoff:
                recent.append(str(f))
            if len(recent) >= 50:
                break
    return {"checked": True, "hours": hours, "recent_files": recent[:50]}


def scan():
    return {
        "platform": platform.platform(),
        "processes": check_processes(),
        "listening_ports": check_listening_ports(),
        "startup_items": check_startup_items(),
        "recent_files": check_recent_files(),
        "disclaimer": "Read-only report. Nothing was changed, killed or deleted. "
                       "Cross-check anything unfamiliar before acting -- see Guardian notes in the vault.",
    }


def summarize(result):
    lines = [f"Platform: {result['platform']}"]
    flagged = result["processes"].get("flagged", [])
    lines.append(f"Flagged processes: {len(flagged)}" + (f" (e.g. {flagged[0]['name']})" if flagged else ""))
    lines.append(f"Listening ports: {len(result['listening_ports'].get('listening', []))}")
    lines.append(f"Startup items: {len(result['startup_items'].get('items', []))}")
    lines.append(f"Recently modified files (last {result['recent_files'].get('hours')}h): "
                 f"{len(result['recent_files'].get('recent_files', []))}")
    lines.append(result["disclaimer"])
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Jarvis Guardian: read-only system checker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args()
    result = scan()
    print(json.dumps(result, indent=2) if a.json else summarize(result))


if __name__ == "__main__":
    main()
