"""CPU priority and thread budget — one profile shared by setup and the launcher.

Why this module exists
----------------------
J.A.R.V.I.S. runs three CPU-hungry things beside its UI: the Live audio path,
the local decision model (Laya, torch) and OpenCV/numpy work. Each library
sizes its own thread pool from the machine's logical core count, so a 16-core
box can end up with three 16-thread pools fighting for the same cores. The
symptom is not low throughput — it is a UI that stutters while nothing looks
busy.

Two measured fixes, applied together:

* **A thread budget.** ``OMP_NUM_THREADS`` and the other BLAS/OpenMP knobs are
  set to half the logical cores (capped at :data:`AUTO_THREAD_MAX`) before the
  child interpreter starts. Libraries read these at import time, so they have
  to be in the environment, not set from Python after the fact. Threads that
  wait passively (``OMP_WAIT_POLICY=PASSIVE``) stop burning a core while idle.
* **A process priority.** ``ABOVE_NORMAL_PRIORITY_CLASS`` on Windows keeps the
  UI and audio callbacks responsive when a background model load saturates the
  machine. It is deliberately not ``HIGH``: a foreground app at high priority
  can starve the compositor and make the whole desktop feel worse.

Rules:

* **Stdlib only.** ``start_jarvis.py`` loads this file *by path* on the bare
  interpreter that may not have a single dependency installed, exactly like
  ``core/startup_optimizer.py``. Nothing here imports project or third-party
  code.
* **Never raises.** A denied ``SetPriorityClass`` (protected process, other
  user) is reported as a plain sentence; the launch continues at normal
  priority.
* **Persisted, not guessed.** The profile lives in ``config/api_keys.json``
  under ``"performance"``. Environment variables override it for one run, using
  the same ``JARVIS_*`` convention as the feature flags.

Usage::

    from core.performance_profile import read_profile, thread_environment
    env.update(thread_environment(read_profile()))
    apply_priority(read_profile()["cpu_priority"], pid=child.pid)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

__all__ = [
    "PRIORITY_CHOICES", "DEFAULT_PRIORITY", "DEFAULT_THREAD_CAP",
    "AUTO_THREAD_MAX", "read_profile", "write_profile", "normalize_priority",
    "thread_environment", "apply_priority", "describe", "default_thread_cap",
    "current_priority",
]

#: Lowest → highest. ``realtime`` is intentionally absent: it can lock a
#: machine up when a process spins, and nothing J.A.R.V.I.S does needs it.
PRIORITY_CHOICES: tuple[str, ...] = (
    "idle", "below_normal", "normal", "above_normal", "high",
)

DEFAULT_PRIORITY = "above_normal"
#: 0 means "derive from the logical core count" (:func:`default_thread_cap`).
DEFAULT_THREAD_CAP = 0
AUTO_THREAD_MAX = 8

# Windows PROCESS_SET_INFORMATION + priority class constants.
_PROCESS_SET_INFORMATION = 0x0200
_WIN_PRIORITY = {
    "idle": 0x00000040,          # IDLE_PRIORITY_CLASS
    "below_normal": 0x00004000,  # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,        # NORMAL_PRIORITY_CLASS
    "above_normal": 0x00008000,  # ABOVE_NORMAL_PRIORITY_CLASS
    "high": 0x00000080,          # HIGH_PRIORITY_CLASS
}

# POSIX nice values. Negative values usually need privileges; a failure is
# reported, never hidden.
_NICE = {
    "idle": 19,
    "below_normal": 10,
    "normal": 0,
    "above_normal": -2,
    "high": -5,
}

_ENV_PRIORITY = "JARVIS_CPU_PRIORITY"
_ENV_THREAD_CAP = "JARVIS_THREAD_CAP"


def _config_path() -> Path:
    """``config/api_keys.json`` next to the project root."""
    return Path(__file__).resolve().parents[1] / "config" / "api_keys.json"


def default_thread_cap(cpu_count: int | None = None) -> int:
    """Half the logical cores, at least 1 and at most :data:`AUTO_THREAD_MAX`.

    Half, not all: the UI, the audio callback and the Live websocket all need
    a core of their own. The cap keeps a 32-core workstation from spawning
    pools so wide that cache contention costs more than the parallelism buys.
    """
    try:
        logical = int(cpu_count if cpu_count is not None else (os.cpu_count() or 4))
    except Exception:
        logical = 4
    return max(1, min(AUTO_THREAD_MAX, max(1, logical // 2)))


def normalize_priority(value) -> str:
    """A known priority name, or the default when *value* is junk."""
    name = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "low": "below_normal",
        "belownormal": "below_normal",
        "abovenormal": "above_normal",
        "normal_priority": "normal",
        "above": "above_normal",
        "highest": "high",
    }
    name = aliases.get(name, name)
    return name if name in PRIORITY_CHOICES else DEFAULT_PRIORITY


def _as_thread_cap(value) -> int | None:
    """A valid 0..64 thread cap, or None when *value* is not a number."""
    if isinstance(value, bool):
        return None
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= cap <= 64:
        return cap
    return None


def read_profile(config_path: str | Path | None = None) -> dict:
    """The effective profile: environment override → config file → defaults.

    Never raises. A missing or corrupt config file simply yields the defaults,
    which are safe on every machine.
    """
    out = {"cpu_priority": DEFAULT_PRIORITY, "thread_cap": DEFAULT_THREAD_CAP}
    try:
        data = json.loads(Path(config_path or _config_path()).read_text(encoding="utf-8"))
        perf = data.get("performance") if isinstance(data, dict) else None
        if isinstance(perf, dict):
            out["cpu_priority"] = normalize_priority(perf.get("cpu_priority"))
            cap = _as_thread_cap(perf.get("thread_cap"))
            if cap is not None:
                out["thread_cap"] = cap
    except Exception:
        pass

    env_priority = os.environ.get(_ENV_PRIORITY, "").strip()
    if env_priority:
        out["cpu_priority"] = normalize_priority(env_priority)
    env_cap = _as_thread_cap(os.environ.get(_ENV_THREAD_CAP, "").strip())
    if env_cap is not None:
        out["thread_cap"] = env_cap
    return out


def write_profile(profile: dict, config_path: str | Path | None = None) -> bool:
    """Persist ``{"cpu_priority", "thread_cap"}`` merge-safely. Never raises.

    Only the ``"performance"`` map is replaced; API keys, feature flags and
    every other top-level key are preserved. The write is atomic (temp file +
    ``os.replace``) so a crash cannot leave a half-written config behind.
    """
    path = Path(config_path or _config_path())
    try:
        data: dict = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        perf = data.get("performance")
        perf = dict(perf) if isinstance(perf, dict) else {}
        perf["cpu_priority"] = normalize_priority(
            profile.get("cpu_priority", perf.get("cpu_priority")))
        cap = _as_thread_cap(profile.get("thread_cap", perf.get("thread_cap")))
        perf["thread_cap"] = cap if cap is not None else DEFAULT_THREAD_CAP
        data["performance"] = perf
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".performance.tmp")
        tmp.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True
    except Exception:
        return False


def thread_environment(profile: dict | None = None,
                       cpu_count: int | None = None) -> dict[str, str]:
    """Environment variables that cap every numerical library's thread pool.

    Returned as plain strings so the caller can use ``env.setdefault`` —
    an explicit choice by the user (or a CI harness) always wins over this
    profile. ``OMP_WAIT_POLICY=PASSIVE`` is the other half of the fix: an
    OpenMP worker that spins while idle shows up as constant CPU load on an
    otherwise quiet desktop.
    """
    prof = profile if isinstance(profile, dict) else read_profile()
    cap = _as_thread_cap(prof.get("thread_cap"))
    if not cap:
        cap = default_thread_cap(cpu_count)
    value = str(cap)
    return {
        "OMP_NUM_THREADS": value,
        "MKL_NUM_THREADS": value,
        "OPENBLAS_NUM_THREADS": value,
        "NUMEXPR_NUM_THREADS": value,
        "VECLIB_MAXIMUM_THREADS": value,
        "OMP_WAIT_POLICY": "PASSIVE",
        "KMP_BLOCKTIME": "1",
        # Tokenizers warns (and forks threads) when it guesses parallelism.
        "TOKENIZERS_PARALLELISM": "false",
    }


def _win_set_priority(pid: int | None, level: str) -> tuple[bool, str]:
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.SetPriorityClass.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    own = pid is None or int(pid) == os.getpid()
    handle = k32.GetCurrentProcess() if own else k32.OpenProcess(
        _PROCESS_SET_INFORMATION, False, int(pid))
    if not handle:
        err = ctypes.get_last_error()
        return (False, f"could not open the process (Windows error {err})")
    try:
        if not k32.SetPriorityClass(handle, _WIN_PRIORITY[level]):
            err = ctypes.get_last_error()
            return (False, f"Windows refused the change (error {err})")
    finally:
        if not own:
            k32.CloseHandle(handle)
    return (True, f"priority set to {level.replace('_', ' ')}")


def _posix_set_priority(pid: int | None, level: str) -> tuple[bool, str]:
    if not hasattr(os, "setpriority"):
        return (False, "this platform has no process priority control")
    try:
        os.setpriority(os.PRIO_PROCESS, int(pid or 0), _NICE[level])
        return (True, f"priority set to {level.replace('_', ' ')}")
    except PermissionError:
        return (False, "permission denied (lowering niceness needs privileges)")
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}")


def apply_priority(level: str | None = None, pid: int | None = None) -> tuple[bool, str]:
    """Set *pid* (default: this process) to *level*. Never raises.

    ``level`` defaults to the persisted profile. The return value says exactly
    what happened so a launcher can log it instead of claiming success.
    """
    name = normalize_priority(level if level is not None else read_profile()["cpu_priority"])
    try:
        if sys.platform == "win32":
            return _win_set_priority(pid, name)
        return _posix_set_priority(pid, name)
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}")


def current_priority(pid: int | None = None) -> str:
    """Best-effort name of *pid*'s current priority class; "" when unknown."""
    try:
        if sys.platform == "win32":
            import ctypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
            k32.GetPriorityClass.restype = ctypes.c_uint32
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            own = pid is None or int(pid) == os.getpid()
            handle = k32.GetCurrentProcess() if own else k32.OpenProcess(
                0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ""
            try:
                value = int(k32.GetPriorityClass(handle))
            finally:
                if not own:
                    k32.CloseHandle(handle)
            for name, constant in _WIN_PRIORITY.items():
                if value == constant:
                    return name
            return ""
        if hasattr(os, "getpriority"):
            nice = os.getpriority(os.PRIO_PROCESS, int(pid or 0))
            for name, value in _NICE.items():
                if nice == value:
                    return name
    except Exception:
        pass
    return ""


def describe(profile: dict | None = None, cpu_count: int | None = None) -> str:
    """One factual line for the setup report and the launch log."""
    prof = profile if isinstance(profile, dict) else read_profile()
    cap = _as_thread_cap(prof.get("thread_cap")) or default_thread_cap(cpu_count)
    return (f"CPU priority {normalize_priority(prof.get('cpu_priority')).replace('_', ' ')}"
            f" · numerical threads {cap}")


def set_power_plan(name: str = "high") -> tuple[bool, str]:
    """Switch the Windows power plan now (system-wide). Never raises.

    Kept separate from :func:`apply_priority` on purpose: a power plan is a
    machine-wide change that outlives J.A.R.V.I.S, so it is only ever applied
    from an explicit setup choice — never silently on every launch.
    """
    if sys.platform != "win32":
        return (False, "power plans are Windows-only")
    plans = {
        "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
        "high": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
        "ultimate": "e9a42b02-d5df-448d-aa00-03f14749eb61",
    }
    guid = plans.get(str(name or "").strip().lower())
    if not guid:
        return (False, f"unknown power plan '{name}'")
    try:
        result = subprocess.run(["powercfg", "/setactive", guid],
                                capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return (False, f"powercfg could not run ({type(exc).__name__}: {exc})")
    if result.returncode == 0:
        return (True, f"power plan set to {name}")
    tail = (result.stderr or b"").decode(errors="replace").strip()
    detail = tail.splitlines()[-1][:160] if tail else f"exit {result.returncode}"
    return (False, f"powercfg refused the change — {detail}")
