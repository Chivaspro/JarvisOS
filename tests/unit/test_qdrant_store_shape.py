"""The optional Qdrant adapter must at least be importable and internally whole.

``ingest()`` used ``Path(src)`` while ``pathlib.Path`` was imported only inside
``_root()`` — so the moment the qdrant_knowledge feature called the one
documented way to populate the collection, it died with NameError. This file
pins the module-scope import (no qdrant-client install needed).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge import qdrant_store  # noqa: E402


def test_path_is_available_at_module_scope():
    assert qdrant_store.Path is Path, (
        "ingest() builds Path(src); the name must exist at module scope")


def test_ingest_and_search_are_callable():
    assert callable(qdrant_store.ingest)
    assert callable(qdrant_store.search)


def test_the_chunker_is_bounded_and_shapes_chunks():
    # 5000 words at the default 900-word window / 450-word stride: the
    # i > 4000 bound caps a single file's ingest instead of indexing forever.
    text = " ".join(f"w{i}" for i in range(5000))
    chunks = list(qdrant_store._chunks(text))
    assert chunks, "the chunker produced nothing"
    assert all(c.strip() for c in chunks)
    assert len(chunks) <= 12, "the per-file ingest bound must hold"
