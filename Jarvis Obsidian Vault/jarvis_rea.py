#!/usr/bin/env python3
"""Jarvis <-> REA bridge (https://github.com/morluto/rea).

Runs the REA command line safely:
  * no shell (arguments are a list, so nothing can be injected)
  * a timeout on every run
  * an audit log (rea_audit.log) of what was run and when (GMT+8)
  * refuses to run unless authorization is explicitly confirmed

Only analyze software you own or are authorized to examine.

Usage:
  python jarvis_rea.py help
  python jarvis_rea.py js /absolute/path/to/app --authorized
Import:
  from jarvis_rea import analyze_javascript_application
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT = Path(__file__).resolve().parent
AUDIT = VAULT / "rea_audit.log"
GMT8 = timezone(timedelta(hours=8))


class REAError(Exception):
    pass


def _base_cmd():
    exe = shutil.which("rea")
    if exe:
        return [exe]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "rea-agents@latest"]
    raise REAError("Neither 'rea' nor 'npx' was found. Install Node.js (22.19+, 24.11+ or 26+) first, "
                   "then run: npm install --global rea-agents")


def _audit(cmd, target):
    stamp = datetime.now(GMT8).strftime("%Y-%m-%d %H:%M:%S GMT+8")
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} | target={target or '-'} | {' '.join(cmd)}\n")


def run_rea(args, *, authorized=False, target=None, timeout=300, runner=subprocess.run):
    """Run `rea <args>`. Returns (returncode, stdout, stderr)."""
    if not authorized:
        raise REAError("Authorization required: confirm you own this software or are allowed to analyze it "
                       "(authorized=True).")
    if not args or not all(isinstance(a, str) and a for a in args):
        raise REAError("args must be a non-empty list of non-empty strings.")
    cmd = _base_cmd() + list(args)
    _audit(cmd, target)
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        raise REAError(f"REA timed out after {timeout}s. Try a smaller target or a longer timeout.")
    except OSError as exc:
        raise REAError(f"Could not start REA: {exc}")
    return p.returncode, p.stdout, p.stderr


def help_text(runner=subprocess.run, timeout=60):
    """`rea --help` is safe and needs no authorization."""
    cmd = _base_cmd() + ["--help"]
    p = runner(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
    return p.stdout or p.stderr


def analyze_javascript_application(path, *, authorized=False, timeout=300, runner=subprocess.run):
    """Static analysis of an extracted JavaScript/Electron app folder or ASAR. Returns parsed JSON."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise REAError(f"Path not found: {p}")
    code, out, err = run_rea(["analyze-javascript-application", str(p), "--json"],
                             authorized=authorized, target=str(p), timeout=timeout, runner=runner)
    if code != 0:
        raise REAError((err or out).strip() or f"REA exited with code {code}")
    try:
        return json.loads(out)
    except ValueError:
        return {"raw_output": out}


def summarize(result, limit=12):
    """Short text overview of a REA JSON result."""
    if isinstance(result, dict):
        lines = []
        for k, v in list(result.items())[:limit]:
            size = f"{len(v)} items" if isinstance(v, (list, dict)) else str(v)[:60]
            lines.append(f"- {k}: {size}")
        return "\n".join(lines)
    return str(result)[:500]


def main():
    ap = argparse.ArgumentParser(description="Jarvis REA bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("help")
    j = sub.add_parser("js")
    j.add_argument("path")
    j.add_argument("--authorized", action="store_true",
                   help="I own this software or am authorized to analyze it")
    j.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    try:
        if a.cmd == "help":
            print(help_text())
        else:
            print(summarize(analyze_javascript_application(a.path, authorized=a.authorized, timeout=a.timeout)))
    except REAError as e:
        print(f"REA error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
