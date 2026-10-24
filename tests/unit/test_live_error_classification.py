"""A dropped Live session must not be reported as a rejected API key.

Measured on a real launch (2026-10-24): the microphone stream was closed by the
server with WebSocket 1007 "Request contains an invalid argument"; the retry
then replayed the dead resumption handle and got 1008 "session not found or
expired". Both codes were in the authentication list, so after two attempts the
HUD said *"Gemini rejected the API key"* and parked in NEEDS KEY while the key
was valid. The codes are transport signals; only the error text can say whether
a credential was refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def main():
    return pytest.importorskip("main")


def test_a_rejected_frame_is_not_an_authentication_failure(main):
    message = ("google.genai.errors.APIError: 1007 None. "
               "Request contains an invalid argument.")
    assert main._live_error_is_auth(message) is False


def test_an_expired_session_is_not_an_authentication_failure(main):
    message = ("websockets.exceptions.ConnectionClosedError: received 1008 "
               "(policy violation) BidiGenerateContent session not found or "
               "expired")
    assert main._live_error_is_auth(message) is False


def test_a_real_refused_credential_is_still_recognised(main):
    assert main._live_error_is_auth(
        "1008 invalid authentication credentials. Expected OAuth 2 access "
        "token, or otherwise authentication failed.") is True
    assert main._live_error_is_auth("API key not valid. Please pass a valid "
                                    "API key.") is True
    assert main._live_error_is_auth("PERMISSION_DENIED") is True


def test_a_dead_resumption_handle_is_dropped(main):
    assert main._live_error_is_stale_resume(
        "1008 None. BidiGenerateContent session not found or expired") is True
    assert main._live_error_is_stale_resume(
        "Resumption handle rejected") is True
    # A plain network failure keeps the handle: it may still be valid.
    assert main._live_error_is_stale_resume("TimeoutError: timed out") is False


def test_sentence_case_invalid_argument_drops_optional_audio(main):
    assert main._live_error_is_optional_feature_rejection(
        "Request contains an invalid argument.") is True
    assert main._live_error_is_optional_feature_rejection(
        "INVALID_ARGUMENT: unknown name realtime_input_config") is True
    assert main._live_error_is_optional_feature_rejection(
        "API key not valid") is False


def test_the_connect_loop_uses_the_classifiers_and_stops_on_shutdown():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_auth_failed = _live_error_is_auth(err_str)" in source
    assert "_live_error_is_stale_resume(str(e))" in source
    assert "_live_error_is_optional_feature_rejection(err_str)" in source
    # A quit that cancels the session must not enter recovery and report the
    # teardown as a real failure.
    guard = source[source.index("except BaseException as e:"):][:1600]
    assert "_stop_all.is_set()" in guard, (
        "the reconnect loop must not recover after shutdown was requested")
