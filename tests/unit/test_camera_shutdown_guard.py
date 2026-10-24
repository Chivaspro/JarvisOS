"""The camera singleton must not race process exit (0xC0000005, 2026-10-24).

The measured crash: ``release_global_camera`` -> ``CameraManager.stop(force=True)``
in native OpenCV while the UI warm-up thread was still inside
``get_camera_manager`` -> ``load_vision_config`` -> ``detect_cuda``. The fixes
are a shutdown flag (no new camera during teardown), a bounded join of the
warm thread before the release, and a release that never waits forever for
the global lock. No camera is opened here: a stub manager stands in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

camera = pytest.importorskip("vision.camera")


class _StubCamera:
    def __init__(self):
        self.stops = []

    def stop(self, force: bool = False) -> None:
        self.stops.append(bool(force))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(camera, "_GLOBAL", None, raising=False)
    monkeypatch.setattr(camera, "_SHUTTING_DOWN", False, raising=False)


def test_begin_shutdown_marks_the_subsystem():
    assert camera._SHUTTING_DOWN is False
    camera.begin_shutdown()
    assert camera._SHUTTING_DOWN is True
    camera.begin_shutdown()  # idempotent
    assert camera._SHUTTING_DOWN is True


def test_a_new_camera_is_refused_during_shutdown():
    camera.begin_shutdown()
    with pytest.raises(RuntimeError):
        camera.get_camera_manager()


def test_an_existing_manager_is_still_returned_during_shutdown():
    stub = _StubCamera()
    camera._GLOBAL = stub
    camera.begin_shutdown()
    assert camera.get_camera_manager() is stub


def test_release_stops_the_manager_and_clears_it():
    stub = _StubCamera()
    camera._GLOBAL = stub
    camera.release_global_camera()
    assert stub.stops == [True]
    assert camera._GLOBAL is None


def test_release_gives_up_when_a_warmup_holds_the_lock(monkeypatch):
    stub = _StubCamera()
    camera._GLOBAL = stub
    monkeypatch.setattr(camera, "_RELEASE_LOCK_TIMEOUT", 0.1)
    camera._GLOBAL_LOCK.acquire()
    try:
        camera.release_global_camera()   # must not hang, must not stop()
        assert stub.stops == []
        assert camera._GLOBAL is stub    # left for the OS to reclaim
    finally:
        camera._GLOBAL_LOCK.release()


def test_shutdown_path_flags_joins_then_releases():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    body = source[source.index("def _release_vision_resources():"):][:3000]
    assert "_camera_mod.begin_shutdown()" in body
    assert '"jarvis-camera-warm"' in body
    assert "release_global_camera()" in body
    # Order matters: flag and join must both come before the release.
    assert body.index("begin_shutdown") < body.index("release_global_camera")
    assert body.index("jarvis-camera-warm") < body.index("release_global_camera")
