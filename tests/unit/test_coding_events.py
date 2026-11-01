"""Real coding progress events → the chat's live coding card.

``actions/coding_events.py`` is the fail-safe bridge the dev agent and the code
helper use to report REAL steps (planning, writing a file, running, fixing) to
the UI. These tests pin:

* the event envelope the UI receives (``(kind, payload)`` on ``coding_activity``);
* that a missing or broken player can never break a build;
* bounded unified diffs for the "what changed" view;
* the wiring points: the UI signal and both action modules.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import coding_events as ce  # noqa: E402

UI = ROOT / "ui.py"
DEV_AGENT = ROOT / "actions" / "dev_agent.py"
CODE_HELPER = ROOT / "actions" / "code_helper.py"


class _Player:
    def __init__(self):
        self.calls = []

    def coding_activity(self, kind, payload):
        self.calls.append((str(kind), dict(payload)))


def test_emit_coding_forwards_the_event_envelope():
    p = _Player()
    ce.emit_coding(p, 'file', path='a.py', code='x = 1\n')
    assert p.calls == [('file', {'path': 'a.py', 'code': 'x = 1\n'})]


def test_emit_coding_never_raises_without_a_player():
    ce.emit_coding(None, 'stage', stage='planning')   # attribute-less player


def test_emit_coding_never_raises_when_the_ui_is_broken():
    class _Boom:
        def coding_activity(self, *a, **k):
            raise RuntimeError('ui exploded')

    ce.emit_coding(_Boom(), 'stage', stage='planning')  # must be swallowed


def test_emit_coding_ignores_players_without_the_channel():
    class _OldPlayer:
        def write_log(self, text):
            pass

    ce.emit_coding(_OldPlayer(), 'stage', stage='x')


def test_unified_diff_reports_real_changes():
    diff = ce.unified_diff_text('a\nb\n', 'a\nc\n', path='f.py')
    assert '-b' in diff
    assert '+c' in diff


def test_unified_diff_is_empty_when_nothing_changed():
    assert ce.unified_diff_text('same\n', 'same\n') == ''


def test_unified_diff_is_bounded():
    old = '\n'.join(f'line {i}' for i in range(500))
    new = '\n'.join(f'LINE {i}' for i in range(500))
    diff = ce.unified_diff_text(old, new, max_lines=40)
    assert len(diff.splitlines()) <= 41


class _BuildPlayer:
    """The player the real dev-agent build talks to (records the events)."""

    def __init__(self):
        self.events = []
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))

    def coding_activity(self, kind, payload):
        self.events.append((str(kind), dict(payload)))


def test_a_driven_build_reports_its_real_steps(monkeypatch, tmp_path):
    """The events the chat card consumes come from the REAL build flow."""
    import sys

    from actions import dev_agent as da

    plan = {
        "project_name": "proj",
        "entry_point": "main.py",
        "files": [{"path": "main.py", "description": "entry", "imports": []},
                  {"path": "util.py", "description": "helper", "imports": []}],
        "run_command": "python main.py",
        "dependencies": [],
    }
    monkeypatch.setattr(da, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(da, "_plan_project", lambda desc, lang: plan)
    monkeypatch.setattr(da, "_write_file", lambda **kw: "print('hi')\n")
    monkeypatch.setattr(da, "_open_vscode", lambda d: True)
    monkeypatch.setattr(da, "_ensure_project_venv", lambda d: (sys.executable, ""))
    monkeypatch.setattr(da, "_run_project",
                        lambda *a, **kw: da._RunOutcome("STDOUT:\nhello", exit_code=0))
    monkeypatch.setattr(da.time, "sleep", lambda *_: None)

    player = _BuildPlayer()
    msg = da._build_project("do a thing", "python", "proj", 5, player=player)

    kinds = [kind for kind, _ in player.events]
    assert "stage" in kinds, "the card needs the real stage lines"
    assert "file" in kinds, "the card needs the real written files"
    assert "run" in kinds, "the card needs the real run attempt"
    assert "done" in kinds, "the card needs the real completion"

    files = [p for k, p in player.events if k == "file"]
    assert [f["path"] for f in files] == ["main.py", "util.py"]
    assert files[0]["code"] == "print('hi')\n"
    assert files[0]["total"] == 2

    done = [p for k, p in player.events if k == "done"][-1]
    assert done["success"] is True
    assert "runs and exits cleanly" in done["summary"]
    assert "runs and exits cleanly" in msg


def test_a_driven_fix_reports_the_real_diff(monkeypatch, tmp_path):
    """An autofix sends the before → after diff the card colours."""
    from actions import dev_agent as da

    monkeypatch.setattr(da, "PROJECTS_DIR", tmp_path)

    fixed = tmp_path / "proj"
    fixed.mkdir()

    class _Resp:
        text = "def main():\n    return 1\n"

    class _Model:
        def generate_content(self, prompt):
            return _Resp()

    monkeypatch.setattr(da, "_get_model", lambda role: _Model())
    monkeypatch.setattr(da, "_safe_project_path",
                        lambda root, rel: fixed / rel)

    player = _BuildPlayer()
    updated = da._fix_files(
        error_output="ValueError: boom",
        project_description="desc",
        all_files=[{"path": "main.py", "description": "entry", "imports": []}],
        file_codes={"main.py": "def main():\n    return 0\n"},
        language="python",
        project_dir=fixed,
        entry_point="main.py",
        player=player,
    )

    assert "main.py" in updated
    diffs = [p for k, p in player.events if k == "diff"]
    assert diffs, "the autofix must report its real change"
    assert "-    return 0" in diffs[0]["diff"]
    assert "+    return 1" in diffs[0]["diff"]


def test_ui_exposes_the_thread_safe_coding_preview_channel():
    src = UI.read_text(encoding="utf-8")
    assert "_coding_preview_sig = pyqtSignal(str, dict)" in src
    assert "_coding_preview_sig.connect(self._apply_coding_preview)" in src
    assert "def coding_activity(self, kind" in src, "MainWindow needs the emitter"
    assert "_coding_preview_sig.emit" in src


def test_both_coding_actions_emit_real_events():
    dev = DEV_AGENT.read_text(encoding="utf-8")
    helper = CODE_HELPER.read_text(encoding="utf-8")
    assert "emit_coding(" in dev, "dev_agent must report its real build steps"
    assert "emit_coding(" in helper, "code_helper must report its real edits"
