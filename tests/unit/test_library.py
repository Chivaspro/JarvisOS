"""The local document library: local-only, cited answers, untouched sources.

The library is the one tool that reads the user's own files, so the tests pin
the promises that make that safe rather than the extraction quality:

* the source file is only ever read — ingest, remove and re-ingest leave it
  byte-for-byte where it was;
* credentials and private stores (``.env``, ``.ssh``, AppData, the library's
  own index) are refused by name, even inside a folder walk;
* a missing MarkItDown is reported with its pip command instead of a silent
  "indexed" for a PDF;
* every hit carries the title, heading and id it came from, and a search that
  finds nothing says so.

Nothing here touches the real ``data/library`` database: every test points the
library at its own scratch folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from knowledge import library


@pytest.fixture()
def docs(tmp_path, monkeypatch):
    """Scratch folder plus a per-test database inside the same tree."""
    data = tmp_path / "data"
    monkeypatch.setattr(library, "_data_dir", lambda: data)
    monkeypatch.setattr(library, "_db_path",
                        lambda: data / "library" / "library.db")
    folder = tmp_path / "docs"
    folder.mkdir()
    return folder


def _write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


GUIDE = ("# Contract\n\nThe liability cap is one million pounds.\n\n"
         "# Payment\n\nInvoices are due in thirty days.\n")


# ── ingest, search, read, remove ──────────────────────────────────────────────

def test_a_markdown_file_is_indexed_searched_and_removed_without_touching_it(docs):
    source = _write(docs, "guide.md", GUIDE)
    before = source.read_bytes()

    result = library.run("ingest", {"path": str(source)})
    assert result["ok"], result["detail"]
    assert result["evidence"]["indexed"] == 1
    assert result["evidence"]["chunks"] >= 1
    doc_id = result["evidence"]["doc_id"]
    assert "not modified" in result["detail"]

    result = library.run("search", {"query": "liability"})
    assert result["ok"], result["detail"]
    hit = result["evidence"]["results"][0]
    assert hit["doc_id"] == doc_id
    assert hit["title"] == "guide"
    assert "liability" in result["detail"].lower()
    # The citation names the document and the heading it came from.
    assert "Contract" in result["detail"]
    assert doc_id in result["detail"]

    result = library.run("read", {"doc_id": doc_id})
    assert result["ok"] and "one million pounds" in result["detail"]

    result = library.run("list", {})
    assert result["ok"] and result["evidence"]["count"] == 1

    result = library.run("remove", {"doc_id": doc_id})
    assert result["ok"]
    assert source.read_bytes() == before, "the source file must be untouched"
    assert "was not touched" in result["detail"]

    result = library.run("search", {"query": "liability"})
    assert result["ok"] is False
    assert result["reason"] == "empty_library"


def test_an_unchanged_file_is_not_indexed_twice_but_a_change_re_indexes(docs):
    source = _write(docs, "note.txt", "The first version of this note. " * 3)
    first = library.run("ingest", {"path": str(source)})
    assert first["ok"] and first["evidence"]["indexed"] == 1

    again = library.run("ingest", {"path": str(source)})
    assert again["ok"] and again["evidence"]["unchanged"] == 1
    assert again["evidence"]["indexed"] == 0
    assert again["evidence"]["doc_id"] == first["evidence"]["doc_id"]

    _write(docs, "note.txt", "The second version says something else. " * 3)
    changed = library.run("ingest", {"path": str(source)})
    assert changed["ok"] and changed["evidence"]["indexed"] == 1
    assert changed["evidence"]["doc_id"] == first["evidence"]["doc_id"], \
        "a changed file keeps its id so citations stay stable"


def test_a_folder_walk_skips_credentials_and_private_folders(docs):
    _write(docs, "public.md", "# Notes\n\nThe standup moved to Tuesday.\n")
    _write(docs, ".env", "GITHUB_TOKEN=ghp_averyrealsecret\n")
    (docs / ".ssh").mkdir()
    _write(docs / ".ssh", "id_rsa", "-----BEGIN PRIVATE KEY-----\n")

    result = library.run("ingest", {"path": str(docs)})
    assert result["ok"], result["detail"]
    assert result["evidence"]["indexed"] == 1
    # The credential file is counted as skipped; the private folder is pruned
    # during the walk, and would be refused if it were ever reached.
    assert result["evidence"]["skipped"] >= 1
    assert library._denied_reason(docs / ".ssh" / "id_rsa")

    assert (docs / ".env").exists()
    assert (docs / ".ssh" / "id_rsa").exists()
    found = library.run("search", {"query": "realsecret"})
    assert found["ok"], found["detail"]
    assert found["evidence"]["count"] == 0, "a secret may never be indexed"


def test_the_librarys_own_index_and_meeting_audio_are_refused(docs, monkeypatch):
    private = library._data_dir() / "library" / "library.db"
    assert library._denied_reason(private)
    meetings = library._data_dir() / "meetings" / "20260101-000000-abcd" / "mic.wav"
    assert library._denied_reason(meetings)
    assert library._denied_reason(Path(r"C:\Users\someone\AppData\Local\thing.txt"))


# ── honest failure modes ─────────────────────────────────────────────────────

def test_a_pdf_without_markitdown_names_the_pip_command(docs, monkeypatch):
    source = docs / "contract.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")
    monkeypatch.setitem(sys.modules, "markitdown", None)  # import raises

    result = library.run("ingest", {"path": str(source)})
    assert result["ok"] is False
    assert result["reason"] == "converter_missing"
    assert "pip install" in result["detail"]
    assert "not modified" in result["detail"]


def test_an_unsupported_extension_lists_what_is_supported(docs):
    source = _write(docs, "thing.xyz", "some bytes")
    result = library.run("ingest", {"path": str(source)})
    assert result["ok"] is False
    assert result["reason"] == "unsupported_type"
    assert ".md" in result["detail"] and ".pdf" in result["detail"]


def test_a_missing_path_is_reported_not_guessed(docs):
    result = library.run("ingest", {"path": str(docs / "nothing-here.txt")})
    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_ingest_without_a_path_is_refused(docs):
    result = library.run("ingest", {})
    assert result["ok"] is False
    assert result["reason"] == "bad_path"


def test_search_on_an_empty_library_explains_the_first_step(docs):
    result = library.run("search", {"query": "anything"})
    assert result["ok"] is False
    assert result["reason"] == "empty_library"
    assert "Ingest" in result["detail"]


def test_an_unknown_action_lists_the_real_ones(docs):
    result = library.run("delete_everything", {})
    assert result["ok"] is False
    assert result["reason"] == "unknown_action"
    assert "ingest" in result["detail"]


def test_status_reports_fts5_and_the_optional_converter(docs):
    result = library.run("status", {})
    evidence = result["evidence"]
    assert evidence["fts5"] is True, "this machine's SQLite has FTS5"
    assert isinstance(evidence["markitdown"], bool)
    assert "FTS5" in result["detail"]
