"""The 500 ms resting-state reconciler: truth in, nothing else touched.

The reactor's state is driven in real time by the Live loop, so this safety net
exists for the two things a state machine cannot see from inside a turn:

* the microphone is switched off while the HUD still says LISTENING;
* a link went down and no state event arrived to say so.

Its restraint matters as much as its work: it may only correct the RESTING
states (LISTENING / MUTED / IDLE). THINKING, EXECUTING and SPEAKING belong to
the turn that set them; RECONNECTING, CONNECTION ISSUE and NEEDS KEY belong to
the connection lifecycle; and before a real state event has proven whether the
link is up it must not act at all.

MainWindow cannot be constructed in this sandbox (the native vision stack), so
the methods are exercised on a bare instance carrying exactly the attributes
they read — the same approach ``test_laya_status_activity`` uses.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("ui_impl_reconciler_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


def _window(*, muted=False, link=True, state='LISTENING', busy=False,
            hud_muted=None):
    """A MainWindow shell with only the attributes the reconciler reads."""
    win = ui.MainWindow.__new__(ui.MainWindow)
    win._muted = bool(muted)
    win._link_live = link
    win.hud = types.SimpleNamespace(
        state=state, muted=bool(muted if hud_muted is None else hud_muted))
    win._command_window = (types.SimpleNamespace(is_busy=lambda: bool(busy))
                           if busy is not None else None)
    applied: list[str] = []
    win._apply_state = lambda s: applied.append(str(s))  # type: ignore[method-assign]
    return win, applied


# ── _idle_state(): the one truthful resting answer ───────────────────────────

def test_muted_microphone_is_muted(qapp):
    win, _ = _window(muted=True, link=True)

    assert win._idle_state() == 'MUTED'


def test_link_down_is_idle(qapp):
    win, _ = _window(muted=False, link=False)

    assert win._idle_state() == 'IDLE'


def test_live_unmuted_is_listening(qapp):
    win, _ = _window(muted=False, link=True)

    assert win._idle_state() == 'LISTENING'


def test_muted_beats_the_link(qapp):
    win, _ = _window(muted=True, link=True)

    assert win._idle_state() == 'MUTED'


# ── the reconciler corrects resting states ───────────────────────────────────

def test_a_stale_listening_becomes_muted_when_the_mic_is_off(qapp):
    win, applied = _window(muted=True, link=True, state='LISTENING')

    win._reconcile_state()

    assert applied == ['MUTED']


def test_a_stale_listening_becomes_idle_without_a_link(qapp):
    win, applied = _window(muted=False, link=False, state='LISTENING')

    win._reconcile_state()

    assert applied == ['IDLE']


def test_a_stale_muted_becomes_listening_when_the_mic_returns(qapp):
    win, applied = _window(muted=False, link=True, state='MUTED')

    win._reconcile_state()

    assert applied == ['LISTENING']


def test_an_already_correct_state_is_left_alone(qapp):
    win, applied = _window(muted=False, link=True, state='LISTENING')

    win._reconcile_state()

    assert applied == []


# ── and never stomps what it does not own ────────────────────────────────────

@pytest.mark.parametrize('state', ['THINKING', 'EXECUTING', 'SPEAKING'])
def test_busy_states_are_never_touched(qapp, state):
    win, applied = _window(muted=True, link=True, state=state)

    win._reconcile_state()

    assert applied == [], f'{state} belongs to the turn that set it'


@pytest.mark.parametrize('state', ['RECONNECTING', 'CONNECTION ISSUE',
                                   'NEEDS KEY'])
def test_connection_states_belong_to_their_owners(qapp, state):
    win, applied = _window(muted=True, link=True, state=state)

    win._reconcile_state()

    assert applied == [], f'{state} is owned by the connection lifecycle'


def test_a_resting_idle_is_corrected_to_the_truth(qapp):
    """IDLE is resting, so a live unmuted link upgrades it to LISTENING."""
    win, applied = _window(muted=False, link=True, state='IDLE')

    win._reconcile_state()

    assert applied == ['LISTENING']


def test_before_a_real_state_event_the_reconciler_does_nothing(qapp):
    win, applied = _window(muted=True, link=None, state='LISTENING')

    win._reconcile_state()

    assert applied == []


def test_a_busy_turn_blocks_the_correction(qapp):
    """is_busy() means a request is in flight: the turn owns the state."""
    win, applied = _window(muted=True, link=True, state='LISTENING', busy=True)

    win._reconcile_state()

    assert applied == []


# ── _note_link_state(): only real events decide ──────────────────────────────

@pytest.mark.parametrize('state', ['LISTENING', 'THINKING', 'EXECUTING',
                                   'SPEAKING'])
def test_live_states_prove_the_link_is_up(qapp, state):
    win, _ = _window(link=None)

    win._note_link_state(state)

    assert win._link_live is True


def test_muted_is_a_microphone_action_not_a_link_proof(qapp):
    """Mutating the mic locally must never mark the Live link up or down."""
    win, _ = _window(link=None)

    win._note_link_state('MUTED')

    assert win._link_live is None


@pytest.mark.parametrize('state', ['RECONNECTING', 'CONNECTION ISSUE',
                                   'NEEDS KEY', 'ERROR'])
def test_down_states_prove_the_link_is_down(qapp, state):
    win, _ = _window(link=True)

    win._note_link_state(state)

    assert win._link_live is False


@pytest.mark.parametrize('state', ['BOOTING', 'CLEANING', 'STARTING_UI', 'READY'])
def test_ambiguous_states_leave_the_answer_alone(qapp, state):
    win, _ = _window(link=None)

    win._note_link_state(state)

    assert win._link_live is None, 'startup phases must not guess a link state'
