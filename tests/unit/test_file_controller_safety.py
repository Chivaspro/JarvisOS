"""file_controller rename safety.

``rename_file`` takes ``new_name`` straight from model output. On Windows an
absolute operand makes ``parent / new_name`` replace the base entirely, and a
``..`` step or an embedded separator silently TURNS A RENAME INTO A MOVE —
either could place a file outside the folder (and outside the safe roots) with
no move consent and no path displayed anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import file_controller as fc  # noqa: E402


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """tmp_path as the only safe root."""
    monkeypatch.setattr(fc, "_SAFE_ROOTS", [tmp_path])
    victim = tmp_path / "notes.txt"
    victim.write_text("keep", encoding="utf-8")
    return tmp_path, victim


def test_a_plain_rename_inside_the_folder_works(sandbox):
    folder, victim = sandbox
    out = fc.rename_file(str(folder), "notes.txt", "renamed.txt")
    assert out.startswith("Renamed:")
    assert (folder / "renamed.txt").exists()
    assert not victim.exists()


def test_a_name_with_a_separator_is_refused_as_a_move(sandbox):
    folder, victim = sandbox
    (folder / "sub").mkdir()
    out = fc.rename_file(str(folder), "notes.txt", "sub/evil.txt")
    assert "Access denied" in out
    assert victim.exists() and not (folder / "sub" / "evil.txt").exists()


def test_a_backslash_traversal_is_refused(sandbox):
    folder, victim = sandbox
    out = fc.rename_file(str(folder), "notes.txt", "..\\evil.bat")
    assert "Access denied" in out
    assert victim.exists()
    assert not (folder.parent / "evil.bat").exists()


def test_an_absolute_name_is_refused(sandbox):
    folder, victim = sandbox
    outside = Path(folder).parent / "outside.txt"
    out = fc.rename_file(str(folder), "notes.txt", str(outside))
    assert "Access denied" in out
    assert victim.exists()
    assert not outside.exists()


def test_bare_dotdot_is_refused(sandbox):
    folder, victim = sandbox
    out = fc.rename_file(str(folder), "notes.txt", "..")
    assert "Access denied" in out
    assert victim.exists()
