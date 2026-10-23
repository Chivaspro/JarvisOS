"""YouTube transcripts across BOTH youtube-transcript-api generations.

Two independent breakages are pinned here:

* v1.x replaced the class-level ``list_transcripts`` API with an instance
  ``fetch()``; the module called the removed class method, so every fetch
  raised AttributeError and "summarize this video" always answered that it
  could not retrieve a transcript.
* v1.x yields ``FetchedTranscriptSnippet`` dataclasses (no ``__getitem__``);
  the old ``entry["text"]`` join raised TypeError on them even when a fetch
  had succeeded. 0.6.x returned raw dicts, so both shapes must work.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("youtube_transcript_api")

from actions import youtube_video as yv  # noqa: E402


class _Snippet:                     # mirrors FetchedTranscriptSnippet's shape
    def __init__(self, text: str):
        self.text = text
        self.start = 0.0
        self.duration = 1.0


class _Fetched(list):               # iterable, like FetchedTranscript
    pass


def _v1_api(entries):
    """A fake v1.x class: instance fetch() returns the snippet objects."""

    class _Api:
        def __init__(self, *a, **k):
            pass

        def fetch(self, video_id, languages=None, preserve_formatting=False):
            return _Fetched(entries)

    return _Api


def _legacy_api(entries):
    """A fake 0.6.x class: class-level list_transcripts + transcript.fetch()."""

    class _Transcript:
        def fetch(self):
            return _Fetched(entries)

    class _List:
        def find_manually_created_transcript(self, langs):
            return _Transcript()

        def find_generated_transcript(self, langs):
            return _Transcript()

        def __iter__(self):
            return iter([_Transcript()])

    class _Api:
        def __init__(self, *a, **k):
            pass

        @classmethod
        def list_transcripts(cls, video_id):
            return _List()

    return _Api


def _wire(monkeypatch, api_cls):
    monkeypatch.setattr(yv, "_TRANSCRIPT_OK", True)
    monkeypatch.setattr(yv, "YouTubeTranscriptApi", api_cls)


def test_v1_dataclass_snippets_are_joined(monkeypatch):
    _wire(monkeypatch, _v1_api([_Snippet("hello"), _Snippet("world")]))
    assert yv._get_transcript("abc12345678") == "hello world"


def test_v1_html_entities_are_left_to_the_summariser(monkeypatch):
    _wire(monkeypatch, _v1_api([_Snippet("it&apos;s fine")]))
    assert yv._get_transcript("abc12345678") == "it&apos;s fine"


def test_legacy_dict_snippets_still_work(monkeypatch):
    _wire(monkeypatch, _legacy_api([{"text": "old"}, {"text": "shape"}]))
    assert yv._get_transcript("abc12345678") == "old shape"


def test_a_v1_fetch_failure_is_none_not_a_crash(monkeypatch):
    class _Api:
        def __init__(self, *a, **k):
            pass

        def fetch(self, video_id, languages=None, preserve_formatting=False):
            raise RuntimeError("no transcript for this video")

    _wire(monkeypatch, _Api)
    assert yv._get_transcript("abc12345678") is None


def test_a_legacy_failure_is_none_not_a_crash(monkeypatch):
    class _Api:
        def __init__(self, *a, **k):
            pass

        @classmethod
        def list_transcripts(cls, video_id):
            raise RuntimeError("blocked")

    _wire(monkeypatch, _Api)
    assert yv._get_transcript("abc12345678") is None
