"""file_processor safety + correctness regressions.

Three defects pinned here:

* ``pd.read_csv(..., errors="replace")`` — pandas has no ``errors`` parameter
  (it is ``encoding_errors`` since 1.3), so every CSV read raised TypeError.
* "code → run" executed any Python file with no approval card and with
  ``python`` from PATH instead of the interpreter running J.A.R.V.I.S.
* An unknown text action overwrote the requested action with the literal
  "custom" before using it as the model instruction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import file_processor as fp  # noqa: E402


@pytest.fixture()
def allow_coding(monkeypatch):
    """coding_agents on, whatever the machine's config says."""
    import config.settings as cs
    monkeypatch.setattr(cs, "get_feature", lambda name, default=True: True)


# ── CSV reads ─────────────────────────────────────────────────────────────────

class _Cols(list):
    def tolist(self):
        return list(self)


class _FakeDf:
    columns = _Cols(["a", "b"])

    def __len__(self):
        return 3


class _FakePandas:
    def __init__(self):
        self.read_csv_kwargs = None

    def read_csv(self, path, **kw):
        self.read_csv_kwargs = kw
        return _FakeDf()

    def read_excel(self, path, **kw):
        return _FakeDf()


def test_csv_read_uses_encoding_errors_not_errors(monkeypatch, tmp_path):
    fake = _FakePandas()
    monkeypatch.setitem(sys.modules, "pandas", fake)
    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")

    out = fp._process_data(csv, "csv", "info", {})

    assert out.startswith("Rows: 3")
    assert fake.read_csv_kwargs is not None
    assert fake.read_csv_kwargs.get("encoding_errors") == "replace"
    assert "errors" not in fake.read_csv_kwargs


# ── code execution is gated ───────────────────────────────────────────────────

def test_run_is_refused_when_the_approval_gate_declines(monkeypatch, tmp_path, allow_coding):
    script = tmp_path / "danger.py"
    marker = tmp_path / "ran.txt"
    script.write_text(
        f"from pathlib import Path\nPath(r'{marker}').write_text('ran')\n",
        encoding="utf-8",
    )
    import security.coding_approval as ca
    monkeypatch.setattr(ca, "request_approval", lambda title, detail: False)

    out = fp._process_code(script, "run", {})

    assert "not run" in out
    assert not marker.exists(), "the file executed despite the declined approval"


def test_run_is_refused_when_coding_is_switched_off(monkeypatch, tmp_path):
    import config.settings as cs
    monkeypatch.setattr(cs, "get_feature", lambda name, default=True: False)
    script = tmp_path / "danger.py"
    marker = tmp_path / "ran.txt"
    script.write_text(f"open(r'{marker}', 'w').close()\n", encoding="utf-8")

    out = fp._process_code(script, "run", {})

    assert "switched off" in out
    assert not marker.exists()


def test_run_uses_the_project_interpreter_once_approved(monkeypatch, tmp_path, allow_coding):
    script = tmp_path / "hello.py"
    script.write_text("import sys; print('OK', sys.executable)\n", encoding="utf-8")
    import security.coding_approval as ca
    monkeypatch.setattr(ca, "request_approval", lambda title, detail: True)

    out = fp._process_code(script, "run", {})

    assert "OK" in out
    assert sys.executable in out, "the run did not use the project interpreter"


# ── the text branch keeps the requested action ────────────────────────────────

def test_an_unknown_text_action_is_not_replaced_with_the_word_custom(monkeypatch, tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("some notes to translate", encoding="utf-8")

    seen = {}

    class _Model:
        def generate_content(self, prompt):
            seen["prompt"] = prompt
            class _R:
                text = "ok"
            return _R()

    monkeypatch.setattr(fp, "_gemini_client", lambda: _Model())

    out = fp._process_text_doc(txt, "txt", "translate", {})

    assert out == "ok"
    assert seen["prompt"].startswith("translate"), (
        "the requested action must survive as the model instruction")
