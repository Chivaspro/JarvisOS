"""The local document library — conversion, chunking and SQLite FTS5 search.

The library is deliberately local and deliberately boring:

* **The source file is only ever read.** Ingestion writes an index under
  ``data/library``; it never renames, rewrites or deletes the document it was
  pointed at. ``remove`` forgets the index entry and says so.
* **Conversion is optional, not mandatory.** Plain text, Markdown, HTML, CSV,
  JSON, XML and source code are read with the standard library. PDF, Word,
  Excel and PowerPoint need the optional MarkItDown package; when it is absent
  the result names the package and the pip command instead of pretending the
  file was indexed.
* **Search returns evidence.** Every hit carries the document title, the heading
  it came from, the id and the matched passage, so an answer can cite what it
  actually read. A search that finds nothing says so; it never invents a
  passage.
* **Secrets and private stores are refused by name.** SSH keys, ``.env`` files,
  browser/app-data directories, the library's own database and meeting
  recordings are never walked into, even when a whole folder is ingested.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

__all__ = ["run", "status"]

_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_TEXT_CHARS = 2_000_000
_MAX_FILES_PER_INGEST = 250
_MAX_CHUNKS_PER_DOC = 5000
_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 150
_MAX_SEARCH = 20
_MAX_READ = 50

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".xml", ".yaml", ".yml", ".html", ".htm", ".ini", ".toml",
    ".cfg", ".conf", ".sql", ".py", ".js", ".ts", ".tsx", ".jsx", ".css",
}
_MARKITDOWN_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".epub",
}

#: Directories that are never walked into. The names are compared case-folded
#: against every path component, so a folder named ``.ssh`` is refused wherever
#: it appears.
_DENY_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "site-packages", "dist-packages", ".ssh", ".gnupg", ".aws", ".azure",
    ".config", "appdata", "windows", "system32", "$recycle.bin",
    "system volume information",
}
_DENY_NAMES = {
    ".env", "api_keys.json", "credentials.json", "secrets.json", "id_rsa",
    "id_ed25519", "id_ecdsa", "ntuser.dat", "sam", "security",
}
_DENY_SUFFIXES = {
    ".pem", ".key", ".pfx", ".p12", ".kdbx", ".db", ".sqlite", ".sqlite3",
    ".wav", ".mp3", ".m4a", ".mp4", ".mov", ".avi", ".mkv", ".zip", ".7z",
    ".rar",
}

_MARKITDOWN_HINT = ("pip install 'markitdown[pdf,docx,xlsx,pptx]'")


class LibraryError(Exception):
    """A real library failure with a sentence the model may repeat."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── paths and configuration ──────────────────────────────────────────────────

def _data_dir() -> Path:
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        return Path(__file__).resolve().parents[1] / "data"


def _db_path() -> Path:
    return _data_dir() / "library" / "library.db"


def _resolve(raw: Any) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise LibraryError("bad_path", "Name a file or folder to index.")
    expanded = os.path.expandvars(os.path.expanduser(text))
    try:
        return Path(expanded).resolve()
    except Exception:
        return Path(expanded).absolute()


def _denied_reason(path: Path) -> str:
    """'' when *path* may be read, else why it must not be."""
    parts = [str(part).casefold() for part in path.parts]
    for part in parts:
        if part in _DENY_DIRS:
            return f"'{part}' is a private or system folder"
    name = path.name.casefold()
    if name in _DENY_NAMES or name.startswith(".env"):
        return f"'{path.name}' can contain credentials"
    suffix = path.suffix.casefold()
    if suffix in _DENY_SUFFIXES:
        return f"'{suffix}' files are not indexed (private or binary data)"
    try:
        private = (_data_dir() / "library").resolve()
        meetings = (_data_dir() / "meetings").resolve()
        if path == private or private in path.parents:
            return "the library's own index is not a source document"
        if path == meetings or meetings in path.parents:
            return "meeting recordings are private and are not indexed"
    except Exception:
        pass
    return ""


def _doc_id(path: Path) -> str:
    key = os.path.normcase(str(path)).casefold()
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def _title(path: Path) -> str:
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem or path.name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _has_module(name: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# ── schema ───────────────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS probe_fts USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS probe_fts")
    except Exception as exc:
        raise LibraryError(
            "fts5_missing",
            "SQLite on this machine has no FTS5 module, so the library cannot "
            "be searched. Nothing was indexed.") from exc
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            sha256 TEXT NOT NULL,
            converter TEXT NOT NULL,
            chunk_count INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            heading TEXT NOT NULL,
            text TEXT NOT NULL,
            start_char INTEGER NOT NULL,
            end_char INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ordinal);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, heading, doc_id UNINDEXED, chunk_id UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    conn.commit()


def _connect(create: bool = True) -> sqlite3.Connection | None:
    path = _db_path()
    if not path.exists() and not create:
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if create:
        _ensure_schema(conn)
    return conn


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        finally:
            conn.close()
        return True
    except Exception:
        return False


# ── conversion ───────────────────────────────────────────────────────────────

class _HTMLText(HTMLParser):
    """Small, dependency-free HTML-to-text converter."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
              "h6", "section", "article", "header", "footer", "table", "ul",
              "ol", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        del attrs
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _decode_bytes(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",)
    else:
        candidates = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _markitdown_text(path: Path) -> str:
    try:
        from markitdown import MarkItDown
    except Exception as exc:
        raise LibraryError(
            "converter_missing",
            f"Indexing {path.name} needs the optional MarkItDown package, "
            f"which is not installed. Run {_MARKITDOWN_HINT} and try again. "
            "The source file was not modified.") from exc
    try:
        result = MarkItDown().convert(str(path))
    except Exception as exc:
        raise LibraryError(
            "conversion_failed",
            f"{path.name} could not be converted ({type(exc).__name__}: {exc}). "
            "The source file was not modified.") from exc
    text = getattr(result, "text_content", None)
    if text is None:
        text = result if isinstance(result, str) else str(result)
    return str(text or "")


def _convert(path: Path) -> tuple[str, str]:
    suffix = path.suffix.casefold()
    if suffix in _TEXT_EXTENSIONS:
        text = _decode_bytes(path)
        if suffix in (".html", ".htm"):
            parser = _HTMLText()
            try:
                parser.feed(text)
                text = parser.text()
            except Exception:
                pass
        return text, "stdlib-text"
    if suffix in _MARKITDOWN_EXTENSIONS:
        return _markitdown_text(path), "markitdown"
    supported = ", ".join(sorted(_TEXT_EXTENSIONS | _MARKITDOWN_EXTENSIONS))
    raise LibraryError(
        "unsupported_type",
        f"{path.name} has an extension this library does not index. Supported: "
        f"{supported}. The source file was not modified.")


def _clean_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n\n… (truncated at the library limit)"
    return text


# ── chunking ─────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")


def _chunks(text: str, title: str) -> list[dict]:
    headings = [(m.start(), m.group(1).strip().rstrip("#").strip())
                for m in _HEADING_RE.finditer(text)]
    chunks: list[dict] = []
    start = 0
    length = len(text)
    while start < length and len(chunks) < _MAX_CHUNKS_PER_DOC:
        end = min(length, start + _CHUNK_CHARS)
        if end < length:
            window_start = start + _CHUNK_CHARS // 2
            window = text[window_start:end]
            cut = window.rfind("\n\n")
            if cut < 0:
                cut = window.rfind("\n")
            if cut >= 0:
                end = window_start + cut
        if end <= start:
            end = min(length, start + _CHUNK_CHARS)
        piece = text[start:end].strip()
        if piece:
            heading = title
            for position, name in headings:
                if position <= start:
                    heading = name or title
                else:
                    break
            chunks.append({"heading": heading, "text": piece,
                           "start_char": start, "end_char": end})
        if end >= length:
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


# ── search ───────────────────────────────────────────────────────────────────

def _fts_query(text: str) -> str:
    tokens = re.findall(r"[^\W_]+", str(text or ""), flags=re.UNICODE)
    tokens = [token for token in tokens if token][:32]
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens)


def _snippet(text: str, query: str, width: int = 260) -> str:
    flat = " ".join(str(text or "").split())
    needle = str(query or "").strip()
    position = flat.casefold().find(needle.casefold()) if needle else -1
    if position < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, position - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _search_rows(conn: sqlite3.Connection, query: str, limit: int,
                 doc_id: str = "") -> list[sqlite3.Row]:
    fts = _fts_query(query)
    if fts:
        sql = (
            "SELECT c.id AS chunk_id, c.doc_id, c.ordinal, c.heading, c.text, "
            "d.title, d.path, bm25(chunks_fts) AS rank, "
            "snippet(chunks_fts, 0, '[', ']', '…', 18) AS snip "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "JOIN documents d ON d.doc_id = c.doc_id "
            "WHERE chunks_fts MATCH ?"
        )
        params: list[Any] = [fts]
        if doc_id:
            sql += " AND c.doc_id = ?"
            params.append(doc_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = list(conn.execute(sql, params))
            if rows:
                return rows
        except sqlite3.Error:
            pass
    # Substring fallback: a query the FTS tokenizer cannot express (a phrase
    # with punctuation, a partial word) still deserves an honest attempt.
    sql = (
        "SELECT c.id AS chunk_id, c.doc_id, c.ordinal, c.heading, c.text, "
        "d.title, d.path, 0.0 AS rank, '' AS snip "
        "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
        "WHERE (instr(lower(c.text), lower(?)) > 0 "
        "OR instr(lower(c.heading), lower(?)) > 0 "
        "OR instr(lower(d.title), lower(?)) > 0)"
    )
    params = [query, query, query]
    if doc_id:
        sql += " AND c.doc_id = ?"
        params.append(doc_id)
    sql += " ORDER BY d.indexed_at DESC, c.ordinal LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


# ── result helpers ───────────────────────────────────────────────────────────

def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "sqlite-fts5", "detail": detail,
            "reason": "", "evidence": evidence}


def _fail(detail: str, reason: str = "library_error") -> dict:
    return {"ok": False, "controller": "sqlite-fts5", "detail": detail,
            "reason": reason, "evidence": {}}


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    try:
        docs = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        return docs, chunks
    except Exception:
        return 0, 0


# ── ingestion ────────────────────────────────────────────────────────────────

def _ingest_file(conn: sqlite3.Connection, path: Path) -> dict:
    denied = _denied_reason(path)
    if denied:
        raise LibraryError("denied_path", f"{path.name} was not indexed: {denied}.")
    try:
        stat = path.stat()
    except Exception as exc:
        raise LibraryError("unreadable",
                           f"{path.name} could not be read ({exc}).") from exc
    if stat.st_size > _MAX_FILE_BYTES:
        raise LibraryError(
            "file_too_large",
            f"{path.name} is {stat.st_size / 1024 / 1024:.1f} MB, larger than "
            f"the {_MAX_FILE_BYTES // 1024 // 1024} MB library limit.")
    text, converter = _convert(path)
    text = _clean_text(text)
    if len(text.strip()) < 20:
        raise LibraryError("no_text",
                           f"{path.name} contained no extractable text.")
    digest = _sha256(path)
    existing = conn.execute(
        "SELECT * FROM documents WHERE path = ? COLLATE NOCASE",
        (str(path),)).fetchone()
    if existing is not None and existing["sha256"] == digest \
            and int(existing["size"]) == int(stat.st_size):
        return {"unchanged": True, "doc_id": existing["doc_id"],
                "title": existing["title"], "chunks": int(existing["chunk_count"]),
                "converter": existing["converter"]}
    doc_id = str(existing["doc_id"]) if existing is not None else _doc_id(path)
    title = _title(path)
    pieces = _chunks(text, title)
    if not pieces:
        raise LibraryError("no_text", f"{path.name} contained no indexable text.")
    indexed_at = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "DELETE FROM chunks_fts WHERE rowid IN "
            "(SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        conn.execute(
            "INSERT INTO documents(doc_id, path, title, kind, size, mtime, "
            "sha256, converter, chunk_count, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, str(path), title, path.suffix.casefold().lstrip("."),
             int(stat.st_size), float(stat.st_mtime), digest, converter,
             len(pieces), indexed_at))
        for ordinal, piece in enumerate(pieces):
            cursor = conn.execute(
                "INSERT INTO chunks(doc_id, ordinal, heading, text, start_char, "
                "end_char) VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, ordinal, piece["heading"], piece["text"],
                 int(piece["start_char"]), int(piece["end_char"])))
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text, heading, doc_id, chunk_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(cursor.lastrowid), piece["text"], piece["heading"],
                 doc_id, int(cursor.lastrowid)))
    return {"unchanged": False, "doc_id": doc_id, "title": title,
            "chunks": len(pieces), "converter": converter}


def _h_ingest(args: dict) -> dict:
    root = _resolve(args.get("path"))
    if not root.exists():
        raise LibraryError("not_found",
                           f"{root} does not exist, so nothing was indexed.")
    recursive = args.get("recursive")
    recursive = True if recursive is None else bool(recursive)
    conn = _connect(create=True)
    assert conn is not None
    try:
        if root.is_file():
            item = _ingest_file(conn, root)
            if item["unchanged"]:
                return _ok(
                    f"'{item['title']}' is already indexed and unchanged "
                    f"({item['chunks']} chunks, id {item['doc_id']}). The source "
                    "file was not modified.", doc_id=item["doc_id"],
                    title=item["title"], chunks=item["chunks"],
                    indexed=0, unchanged=1, failed=0, converter=item["converter"])
            return _ok(
                f"Indexed '{item['title']}' ({item['chunks']} chunks, id "
                f"{item['doc_id']}) with {item['converter']}. The source file was "
                "not modified.", doc_id=item["doc_id"], title=item["title"],
                chunks=item["chunks"], indexed=1, unchanged=0, failed=0,
                converter=item["converter"])

        files: list[Path] = []
        skipped = 0
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames
                if name.casefold() not in _DENY_DIRS)
            if not recursive:
                dirnames[:] = []
            for name in sorted(filenames):
                candidate = Path(directory) / name
                if _denied_reason(candidate):
                    skipped += 1
                    continue
                if len(files) >= _MAX_FILES_PER_INGEST:
                    skipped += 1
                    continue
                files.append(candidate)
        if not files:
            raise LibraryError(
                "no_files",
                f"No indexable files were found under {root}. Private folders "
                "and unsupported file types are skipped.")
        indexed = unchanged = 0
        failed: list[str] = []
        doc_ids: list[str] = []
        for candidate in files:
            try:
                item = _ingest_file(conn, candidate)
            except LibraryError as exc:
                failed.append(f"{candidate.name}: {exc.detail}")
                continue
            except Exception as exc:
                failed.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
                continue
            if item["unchanged"]:
                unchanged += 1
            else:
                indexed += 1
            doc_ids.append(item["doc_id"])
        if indexed == 0 and unchanged == 0:
            detail = (f"No file under {root} could be indexed. "
                      + ("; ".join(failed[:5]) if failed else "No readable files."))
            return {"ok": False, "controller": "sqlite-fts5", "detail": detail,
                    "reason": "nothing_indexed",
                    "evidence": {"failed": len(failed), "skipped": skipped}}
        detail = (f"Indexed {indexed} new document(s), {unchanged} unchanged, "
                  f"{len(failed)} failed and {skipped} skipped under {root}.")
        if failed:
            detail += " First failures: " + "; ".join(failed[:5]) + "."
        detail += " Source files were not modified."
        return _ok(detail, indexed=indexed, unchanged=unchanged,
                   failed=len(failed), skipped=skipped, doc_ids=doc_ids[:50],
                   path=str(root), recursive=recursive)
    finally:
        conn.close()


# ── read-only actions ────────────────────────────────────────────────────────

def _h_status(args: dict) -> dict:
    del args
    path = _db_path()
    fts = _fts5_available()
    markitdown = _has_module("markitdown")
    docs = chunks = 0
    if path.exists():
        try:
            conn = _connect(create=False)
            if conn is not None:
                try:
                    docs, chunks = _counts(conn)
                finally:
                    conn.close()
        except Exception:
            docs = chunks = 0
    detail = f"Library database: {path}. {docs} document(s), {chunks} chunk(s)."
    detail += (" SQLite FTS5 is available." if fts else
               " SQLite FTS5 is NOT available, so search cannot work.")
    detail += (" MarkItDown is installed for PDF/Office conversion." if markitdown
               else f" MarkItDown is not installed ({_MARKITDOWN_HINT}); plain "
                    "text and Markdown still work.")
    return {"ok": bool(fts), "controller": "sqlite-fts5", "detail": detail,
            "reason": "" if fts else "fts5_missing",
            "evidence": {"documents": docs, "chunks": chunks, "fts5": fts,
                         "markitdown": markitdown, "db_path": str(path)}}


def _h_search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise LibraryError("bad_query", "Search needs a query.")
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(_MAX_SEARCH, limit))
    doc_id = str(args.get("doc_id") or "").strip()
    conn = _connect(create=False)
    if conn is None:
        raise LibraryError(
            "empty_library",
            "The library is empty. Ingest a file or folder first.")
    try:
        docs, _ = _counts(conn)
        if docs == 0:
            raise LibraryError("empty_library",
                               "The library is empty. Ingest a file or folder first.")
        rows = _search_rows(conn, query, limit, doc_id)
        if not rows:
            return _ok(f"No indexed passage matched '{query}'. The library "
                       f"contains {docs} document(s).", query=query, count=0,
                       results=[])
        results = []
        lines = []
        for row in rows:
            text = str(row["text"] or "")
            snip = str(row["snip"] or "") or _snippet(text, query)
            heading = str(row["heading"] or "document")
            title = str(row["title"] or "document")
            score = round(-float(row["rank"] or 0.0), 4)
            results.append({"doc_id": str(row["doc_id"]), "title": title,
                            "heading": heading, "ordinal": int(row["ordinal"]),
                            "score": score, "path": str(row["path"])})
            lines.append(f"[{title} — {heading}] {snip} "
                         f"(doc_id: {row['doc_id']}, chunk {row['ordinal']})")
        return _ok(f"{len(results)} passage(s) matched '{query}':\n"
                   + "\n".join(lines), query=query, count=len(results),
                   results=results)
    finally:
        conn.close()


def _h_read(args: dict) -> dict:
    doc_id = str(args.get("doc_id") or "").strip()
    if not doc_id:
        raise LibraryError("bad_doc_id", "Reading needs a doc_id.")
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        count = int(args.get("count") or 20)
    except (TypeError, ValueError):
        count = 20
    count = max(1, min(_MAX_READ, count))
    conn = _connect(create=False)
    if conn is None:
        raise LibraryError("not_found", f"No indexed document has id {doc_id}.")
    try:
        doc = conn.execute("SELECT * FROM documents WHERE doc_id = ?",
                           (doc_id,)).fetchone()
        if doc is None:
            raise LibraryError("not_found", f"No indexed document has id {doc_id}.")
        rows = list(conn.execute(
            "SELECT * FROM chunks WHERE doc_id = ? ORDER BY ordinal "
            "LIMIT ? OFFSET ?", (doc_id, count, offset)))
        if not rows:
            return _ok(f"No more chunks in '{doc['title']}' after offset {offset}.",
                       doc_id=doc_id, title=str(doc["title"]), offset=offset,
                       count=0, total=int(doc["chunk_count"]))
        lines = [f"{doc['title']} (doc_id: {doc_id}) — {doc['path']}"]
        for row in rows:
            lines.append(f"\n[{int(row['ordinal'])}] {row['heading']}\n{row['text']}")
        return _ok("\n".join(lines), doc_id=doc_id, title=str(doc["title"]),
                   offset=offset, count=len(rows),
                   total=int(doc["chunk_count"]), path=str(doc["path"]))
    finally:
        conn.close()


def _h_list(args: dict) -> dict:
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(100, limit))
    conn = _connect(create=False)
    if conn is None:
        return _ok("No documents are indexed yet.", count=0, documents=[])
    try:
        rows = list(conn.execute(
            "SELECT * FROM documents ORDER BY indexed_at DESC, title LIMIT ?",
            (limit,)))
        if not rows:
            return _ok("No documents are indexed yet.", count=0, documents=[])
        lines = []
        docs = []
        for row in rows:
            docs.append({"doc_id": str(row["doc_id"]), "title": str(row["title"]),
                         "path": str(row["path"]), "chunks": int(row["chunk_count"]),
                         "indexed_at": str(row["indexed_at"])})
            lines.append(f"{row['doc_id']} — {row['title']} — "
                         f"{row['chunk_count']} chunks — {row['path']}")
        return _ok(f"{len(rows)} indexed document(s):\n" + "\n".join(lines),
                   count=len(rows), documents=docs)
    finally:
        conn.close()


def _h_remove(args: dict) -> dict:
    doc_id = str(args.get("doc_id") or "").strip()
    if not doc_id:
        raise LibraryError("bad_doc_id", "Removing needs a doc_id.")
    conn = _connect(create=False)
    if conn is None:
        raise LibraryError("not_found", f"No indexed document has id {doc_id}.")
    try:
        doc = conn.execute("SELECT * FROM documents WHERE doc_id = ?",
                           (doc_id,)).fetchone()
        if doc is None:
            raise LibraryError("not_found", f"No indexed document has id {doc_id}.")
        with conn:
            conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN "
                "(SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        return _ok(f"Removed '{doc['title']}' from the library index. The source "
                   f"file at {doc['path']} was not touched.",
                   doc_id=doc_id, title=str(doc["title"]), path=str(doc["path"]),
                   chunks=int(doc["chunk_count"]))
    finally:
        conn.close()


_HANDLERS = {
    "status": _h_status,
    "ingest": _h_ingest,
    "search": _h_search,
    "read": _h_read,
    "list": _h_list,
    "remove": _h_remove,
}


def status() -> dict:
    """Compact state for diagnostics (never creates the database)."""
    result = _h_status({})
    return {"available": bool(result.get("ok")),
            "detail": str(result.get("detail") or ""),
            **(result.get("evidence") or {})}


def run(action_name: str, args: dict) -> dict:
    """Run one library action; never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip())
    if handler is None:
        return _fail(f"'{action_name}' is not a library action. Valid actions: "
                     f"{', '.join(_HANDLERS)}.", "unknown_action")
    try:
        return handler(dict(args or {}))
    except LibraryError as exc:
        return _fail(exc.detail, exc.reason)
    except sqlite3.Error as exc:
        return _fail(f"The library database failed ({type(exc).__name__}: {exc}). "
                     "No source file was modified.", "database_error")
    except Exception as exc:
        return _fail(f"Library action failed ({type(exc).__name__}: {exc}). No "
                     "source file was modified.", "library_error")
