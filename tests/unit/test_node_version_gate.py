"""The Node runtime floor must agree everywhere and be minor-aware (defect 4).

``package.json`` / ``package-lock.json`` require ``node >= 22.15.0``, but the
setup wizard accepted ANY Node 18+ and told the user 18+ was enough — so a clean
install could certify a runtime that the very first ``npm ci`` rejects, and the
diagnostics card disagreed with the manifest in the same breath.

These tests pin ONE floor across the manifest, the lockfile, the wizard and the
installer probe, and pin the version parser's behaviour on the shapes it will
actually see (a major-only check passed v22.14.9, which npm refuses).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_jarvis  # noqa: E402  (stdlib-only wizard)
from app import installer  # noqa: E402


def _node_engines(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("engines", {}).get("node", ""))


def test_the_manifest_and_lockfile_agree_on_the_node_floor():
    manifest = _node_engines(ROOT / "package.json")
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    root_engines = str((lock.get("packages", {}).get("", {}) or {})
                       .get("engines", {}).get("node", ""))
    assert manifest, "package.json must declare a Node engines floor"
    assert manifest == root_engines, (
        f"package.json says {manifest!r} but package-lock root says "
        f"{root_engines!r}")


def test_every_gate_uses_the_manifest_floor():
    manifest = _node_engines(ROOT / "package.json")
    m = re.match(r">=\s*(\d+)\.(\d+)\.(\d+)", manifest)
    assert m, f"package.json engines {manifest!r} is not a >=major.minor.patch floor"
    floor = tuple(int(x) for x in m.groups())
    text = ".".join(str(x) for x in floor)

    assert setup_jarvis.NODE_MIN == floor, "the wizard's floor drifted"
    assert installer.NODE_MIN == floor, "the installer probe's floor drifted"
    assert setup_jarvis.NODE_MIN_TEXT == text
    assert installer.NODE_MIN_TEXT == text


def test_the_wizard_no_longer_documents_a_stale_floor():
    source = (ROOT / "setup_jarvis.py").read_text(encoding="utf-8")
    assert "Node.js 18+" not in source
    assert "wants v18" not in source
    assert setup_jarvis.NODE_MIN_TEXT in source


def test_the_installer_card_names_the_enforced_floor():
    rows = installer.feature_status()
    row = next(r for r in rows if r["feature"] == "mcp_host")
    assert installer.NODE_MIN_TEXT in row["package"]
    if not row["ready"]:
        assert installer.NODE_MIN_TEXT in row["fix"]


# ── the parser ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("v22.15.0", (22, 15, 0)),
    ("22.15.0", (22, 15, 0)),
    ("v22.15.1", (22, 15, 1)),
    ("v23.0.0", (23, 0, 0)),
    ("v22.14.9", (22, 14, 9)),
    ("", (0, 0, 0)),
    ("node", (0, 0, 0)),
    ("v22", (0, 0, 0)),
    ("v22.15", (0, 0, 0)),
    ("junk 1.2.3", (0, 0, 0)),
])
def test_the_node_parser_handles_the_shapes_it_will_see(text, expected):
    assert installer._parse_node_version(text) == expected
    assert setup_jarvis._parse_node_version(text) == expected


# ── the probe ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version,ready", [
    ("v22.15.0", True),     # exactly the floor
    ("v22.16.0", True),     # newer patch
    ("v23.1.0", True),      # newer major
    ("v22.14.9", False),    # right major, minor below the floor (the old bug)
    ("v21.7.3", False),
    ("v18.20.4", False),
    ("v16.4.0", False),
    ("v22.15", False),      # unparseable patch
    ("junk", False),
])
def test_the_probe_accepts_only_a_real_22_15_plus_runtime(monkeypatch, version, ready):
    class _R:
        stdout = version.encode()
        stderr = b""
        returncode = 0

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _R())
    got_ready, detail = installer.node_status()
    assert got_ready is ready, detail


def test_the_probe_reports_an_unreadable_version_honestly(monkeypatch):
    class _R:
        stdout = b"junk"
        stderr = b""
        returncode = 0

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _R())
    ready, detail = installer.node_status()
    assert ready is False and "unreadable" in detail


# ── the wizard's own check ───────────────────────────────────────────────────

def test_check_node_rejects_a_minor_below_the_floor(monkeypatch):
    monkeypatch.setattr(setup_jarvis.shutil, "which", lambda name: "C:/node.exe")
    monkeypatch.setattr(setup_jarvis, "_version_of", lambda *a, **k: "v22.14.9")
    good, ver = setup_jarvis.check_node()
    assert good is False and ver == "v22.14.9"


def test_check_node_accepts_the_exact_floor(monkeypatch):
    monkeypatch.setattr(setup_jarvis.shutil, "which", lambda name: "C:/node.exe")
    monkeypatch.setattr(setup_jarvis, "_version_of", lambda *a, **k: "v22.15.0")
    good, _ = setup_jarvis.check_node()
    assert good is True


def test_check_node_reports_missing_node(monkeypatch):
    monkeypatch.setattr(setup_jarvis.shutil, "which", lambda name: None)
    good, detail = setup_jarvis.check_node()
    assert good is False and "PATH" in detail
