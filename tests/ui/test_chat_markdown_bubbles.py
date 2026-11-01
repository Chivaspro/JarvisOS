"""Chat bubbles render Markdown — for J.A.R.V.I.S. and for the user.

A reply full of ``**bold**``, ``# headings`` and fenced code used to show the
literal symbols because the bubble only escaped text and linkified URLs. These
tests pin the new contract:

* Markdown is rendered in BOTH bubble identities;
* source text can never inject HTML (escaping survives the renderer);
* fenced code renders as a monospace block;
* the bubble stays within its width bound for long content.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_md_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def _bubble(text, you=False):
    b = ui._ChatBubble(text, you)
    return b


def test_streaming_paint_is_plain_then_one_rich_render(qapp):
    """The voice-paced reveal paints plain per tick; markdown once at the end.

    Re-rendering the whole accumulated reply as Markdown on every 24 ms
    reveal tick was the streaming stutter; the bubble must stay cheap while
    words arrive and only become rich when the reveal completes.
    """
    from PyQt6.QtCore import Qt

    b = _bubble('')
    b.begin_stream()
    b.append_text('Use **bold** ')
    assert b._body.textFormat() == Qt.TextFormat.PlainText
    assert b._body.text() == 'Use **bold** '

    b.append_text('now.')
    assert b._body.text() == 'Use **bold** now.'
    assert '**' in b._body.text(), "markdown must not be interpreted mid-reveal"

    b.end_stream()
    assert b._body.textFormat() == Qt.TextFormat.RichText
    assert '<b>' in b._body.text()
    assert '**' not in b._body.text()


def test_assistant_bubble_renders_bold(qapp):
    b = _bubble('Use **bold** here')
    html = b._body.text()
    assert '<b>' in html, f"bold was not rendered: {html!r}"
    assert '**bold**' not in html


def test_user_bubble_renders_markdown_too(qapp):
    b = _bubble('Make it **loud**', you=True)
    assert '<b>' in b._body.text()


def test_headings_render_as_headings(qapp):
    b = _bubble('# Big Title\n\nBody text')
    html = b._body.text()
    assert '<h1' in html, f"heading was not rendered: {html!r}"


def test_fenced_code_renders_as_monospace_block(qapp):
    b = _bubble('```python\nx = 1\n```')
    html = b._body.text()
    assert '<pre' in html
    assert 'Share Tech Mono' in html


def test_bubble_escapes_html(qapp):
    b = _bubble('<script>alert(1)</script>')
    html = b._body.text()
    assert '<script>' not in html, "raw HTML must never survive into the bubble"
    assert '&lt;script&gt;' in html


def test_javascript_urls_are_not_links(qapp):
    b = _bubble('[click](javascript:alert(1))')
    html = b._body.text()
    assert 'href="javascript:' not in html


def test_long_content_stays_inside_the_width_bound(qapp):
    b = _bubble('word ' * 400)
    assert b.width() <= 480, f"bubble overflowed its bound: {b.width()}"


def test_streaming_append_re_renders_markdown(qapp):
    b = _bubble('')
    b.append_text('**bo')
    b.append_text('ld** done')
    assert '<b>' in b._body.text()
    assert b._raw_text == '**bold** done'
