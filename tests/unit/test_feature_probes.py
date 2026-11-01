"""The on-demand feature probes: what is installed, and what would add it.

A probe that guesses is worse than no probe — the answer here is what the
Settings card and the diagnostics list repeat to the user — so every state is
asserted from a forced condition rather than from whatever this machine happens
to have.
"""
from __future__ import annotations

import pytest

from app import installer


class _Result:
    def __init__(self, stdout: bytes = b"v22.16.0", returncode: int = 0):
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


# ── the Node runtime the MCP host needs ──────────────────────────────────────

def test_a_missing_node_is_reported_as_missing(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("node")

    monkeypatch.setattr(installer.subprocess, "run", _boom)
    ready, detail = installer.node_status()
    assert ready is False
    assert "PATH" in detail


def test_an_old_node_is_not_good_enough(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result(b"v16.4.0"))
    ready, detail = installer.node_status()
    assert ready is False
    assert "older than" in detail and "22.15.0" in detail


def test_a_minor_below_the_floor_is_not_good_enough(monkeypatch):
    # v22.14.9 has the right major but is below npm's own ">=22.15.0" floor.
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result(b"v22.14.9"))
    ready, detail = installer.node_status()
    assert ready is False and "older than" in detail


def test_the_exact_floor_is_good_enough(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result(b"v22.15.0"))
    ready, detail = installer.node_status()
    assert ready is True and detail == "node v22.15.0"


def test_node_with_no_version_output_is_not_assumed_to_work(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda *a, **k: _Result(b"", returncode=1))
    ready, detail = installer.node_status()
    assert ready is False and detail


def test_a_modern_node_is_ready_and_its_version_is_quoted(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result(b"v22.16.0"))
    ready, detail = installer.node_status()
    assert ready is True
    assert detail == "node v22.16.0"


# ── REA (npm MCP server, opt-in) ─────────────────────────────────────────────

def test_rea_needs_both_a_supported_node_and_the_extracted_package(monkeypatch):
    from mcp_host import rea

    monkeypatch.setattr(rea, "node_readiness",
                        lambda refresh=False: {"ok": False,
                                               "error": "Node.js 20 is not supported"})
    ready, detail = installer.rea_status()
    assert ready is False and "not supported" in detail

    monkeypatch.setattr(rea, "node_readiness",
                        lambda refresh=False: {"ok": True, "warning": ""})
    monkeypatch.setattr(rea, "rea_cached", lambda version=None: False)
    ready, detail = installer.rea_status()
    assert ready is False and "not cached" in detail

    monkeypatch.setattr(rea, "rea_cached", lambda version=None: True)
    ready, detail = installer.rea_status()
    assert ready is True and rea.REA_VERSION in detail


def test_installing_rea_runs_the_npx_extraction_and_verifies_it(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "node_status", lambda: (True, "node v24.20.0"))
    monkeypatch.setattr(installer.shutil, "which", lambda name: f"C:/node/{name}.CMD")
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or _Result(b"6.7.0"))
    monkeypatch.setattr(installer, "rea_status",
                        lambda: (True, f"REA {installer.REA_VERSION} cached"))

    ok, message = installer.install_feature("rea")

    assert ok is True and "ready" in message
    assert calls and calls[0][-1] == "--version"
    assert f"rea-agents@{installer.REA_VERSION}" in calls[0]


def test_rea_install_does_not_claim_success_before_the_cache_has_it(monkeypatch):
    monkeypatch.setattr(installer, "node_status", lambda: (True, "node v24.20.0"))
    monkeypatch.setattr(installer.shutil, "which", lambda name: f"C:/node/{name}.CMD")
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda *a, **k: _Result(b"", returncode=1))
    monkeypatch.setattr(installer, "rea_status",
                        lambda: (False, "REA is not cached"))

    ok, message = installer.install_feature("rea")

    assert ok is False and "failed" in message


def test_rea_is_listed_as_an_opt_in_feature_with_its_pin():
    rows = installer.feature_status()
    row = next(r for r in rows if r["feature"] == "rea")
    assert "opt-in" in row["purpose"]
    assert row["package"] == f"{installer.REA_PACKAGE}@{installer.REA_VERSION}"


def test_laya_is_listed_with_both_runtime_packages():
    rows = installer.feature_status()
    laya_rows = [r for r in rows if r["feature"] == "laya"]
    packages = {r["package"] for r in laya_rows}
    assert "laya==0.4.14" in packages
    assert any(p.startswith("transformers") for p in packages)
    for row in laya_rows:
        assert row["purpose"]
        if not row["ready"]:
            assert row["fix"].startswith("pip install")


# ── the feature table ────────────────────────────────────────────────────────

def test_every_row_names_the_feature_that_needs_it_and_how_to_add_it():
    rows = installer.feature_status()
    assert rows, "the probe must return the table"
    features = {row["feature"] for row in rows}
    assert {"documents", "clipboard", "smart_home", "instagram", "mcp_host"} <= features
    for row in rows:
        assert row["purpose"], row
        if row["ready"]:
            assert row["fix"] == "", "a ready package needs no fix"
        else:
            assert row["fix"], f"{row['package']} is missing and must name a fix"


def test_the_install_never_claims_success_it_cannot_verify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(installer, "_available", lambda module: False)
    monkeypatch.setattr(installer, "_pip", lambda package, log=None: calls.append(package) or True)
    ok, message = installer.install_feature("documents")
    assert calls, "the missing packages must be pip-installed"
    assert ok is False, "importable is what counts, not that pip returned 0"
    assert "still not importable" in message


def test_installing_a_feature_that_is_already_there_changes_nothing(monkeypatch):
    monkeypatch.setattr(installer, "_available", lambda module: True)
    monkeypatch.setattr(installer, "_pip", lambda *a, **k: pytest.fail("nothing to install"))
    ok, message = installer.install_feature("documents")
    assert ok is True and "already installed" in message


def test_an_unknown_feature_lists_the_real_ones():
    ok, message = installer.install_feature("teleporter")
    assert ok is False
    assert "documents" in message and "smart_home" in message


# ── the diagnostics rows ─────────────────────────────────────────────────────

def test_diagnostics_reports_one_row_per_probe_and_carries_the_fix(monkeypatch):
    from capabilities import health
    monkeypatch.setattr(installer, "feature_status", lambda: [
        {"feature": "documents", "module": "openpyxl", "package": "openpyxl",
         "purpose": "Excel workbooks", "ready": True, "detail": "", "fix": ""},
        {"feature": "documents", "module": "pypdf", "package": "pypdf",
         "purpose": "PDF merge", "ready": False, "detail": "missing pypdf",
         "fix": "pip install pypdf"},
    ])
    checks = health.feature_checks()
    assert len(checks) == 2
    ready, missing = checks
    assert ready.name == "documents · openpyxl" and ready.fix == ""
    assert missing.name == "documents · pypdf"
    assert "pip install pypdf" in missing.fix
    assert "missing pypdf" in missing.detail


def test_a_broken_probe_is_reported_instead_of_raising(monkeypatch):
    from capabilities import health

    def _boom():
        raise RuntimeError("installer is broken")

    monkeypatch.setattr(installer, "feature_status", _boom)
    checks = health.feature_checks()
    assert len(checks) == 1
    assert "installer is broken" in checks[0].detail
