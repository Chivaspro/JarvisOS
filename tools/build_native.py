#!/usr/bin/env python3
"""Build the optional native Rust extension (``jarvis_native``).

STDLIB ONLY — like ``start_jarvis.py`` and ``setup_jarvis.py`` this has to run
on a bare interpreter. It shells out to cargo/rustc rather than importing
anything from the project.

What it does
------------
1. Finds ``cargo`` (PATH or ``%USERPROFILE%\\.cargo\\bin``).
2. Picks a toolchain that can actually *link* on this machine:

   * the default toolchain, if a trivial program links with it (MSVC with
     Visual Studio Build Tools installed), else
   * the GNU toolchain (``stable-x86_64-pc-windows-gnu``) — which additionally
     needs ``dlltool`` (binutils) for Rust's raw-dylib Python linking. We look
     in PATH, the winget Links dir, WinLibs/MSYS2/Git installs.

3. Builds ``native/`` in release mode (``cargo build --release``, with
   ``--target x86_64-pc-windows-gnu`` when on GNU).
4. Copies ``jarvis_native.dll`` → ``native/jarvis_native.pyd`` where
   ``core/native.py`` finds it without any install step.
5. Verifies the built module imports in *this* interpreter.

The app never needs any of this: without the ``.pyd`` it runs the pure-Python
backend (``core/native.py``). For redistributable wheels, use maturin with
``native/pyproject.toml``.

Usage::

    python tools/build_native.py            # build (or rebuild) in release mode
    python tools/build_native.py --check    # report toolchain/artifact status only
    python tools/build_native.py --quiet    # only print problems + final line

Exit codes: 0 = built (or --check ok), 1 = build failed, 3 = toolchain missing.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native"
PYD = NATIVE / "jarvis_native.pyd"
GNU_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"
GNU_TARGET = "x86_64-pc-windows-gnu"

_IS_WIN = os.name == "nt"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WIN else {}

PROBE_TIMEOUT = 60.0     # linking a hello-world can be slow on a cold disk
BUILD_TIMEOUT = 2400.0   # first full release build of PyO3+rubato from scratch


def _log(quiet: bool, msg: str) -> None:
    if not quiet:
        print(msg, flush=True)


def _run(cmd: list[str], *, env: dict | None = None, timeout: float, cwd: Path | None = None):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
        cwd=str(cwd) if cwd else None,
        **_NO_WINDOW,
    )


# ── toolchain discovery ─────────────────────────────────────────────────────


def find_cargo() -> str | None:
    cargo = shutil.which("cargo")
    if cargo:
        return cargo
    candidate = Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin" / "cargo.exe"
    if candidate.is_file():
        return str(candidate)
    return None


def find_dlltool() -> str | None:
    """binutils ``dlltool`` — required by rustc's raw-dylib path on GNU targets."""
    found = shutil.which("dlltool")
    if found:
        return found
    if not _IS_WIN:
        return None
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        str(Path(local) / "Microsoft" / "WinGet" / "Links" / "dlltool.exe"),
        str(Path(local) / "Microsoft" / "WinGet" / "Packages" / "BrechtSanders.WinLibs*"
            / "**" / "bin" / "dlltool.exe"),
        r"C:\msys64\mingw64\bin\dlltool.exe",
        r"C:\msys64\usr\bin\dlltool.exe",
        r"C:\Program Files\Git\mingw64\bin\dlltool.exe",
        r"C:\Strawberry\c\bin\dlltool.exe",
    ]
    for pattern in patterns:
        for hit in glob.glob(pattern, recursive=True):
            if Path(hit).is_file():
                return hit
    return None


def rustup_toolchains(cargo: str) -> str:
    rustup = Path(cargo).with_name("rustup.exe" if _IS_WIN else "rustup")
    if not rustup.is_file():
        rustup = Path(shutil.which("rustup") or "")
    if not rustup.is_file():
        return ""
    try:
        out = _run([str(rustup), "toolchain", "list"], timeout=30.0)
        return out.stdout + out.stderr
    except Exception:
        return ""


def probe_default_toolchain(cargo: str, env: dict) -> tuple[bool, str]:
    """Compile + link a trivial program with the default toolchain."""
    rustc = Path(cargo).with_name("rustc.exe" if _IS_WIN else "rustc")
    if not rustc.is_file():
        rustc = Path(shutil.which("rustc") or "")
    if not rustc.is_file():
        return False, "rustc not found next to cargo"
    probe_dir = NATIVE / "target" / "_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    src = probe_dir / "probe.rs"
    exe = probe_dir / ("probe.exe" if _IS_WIN else "probe")
    src.write_text('fn main() { println!("ok"); }\n', encoding="utf-8")
    try:
        out = _run([str(rustc), str(src), "-o", str(exe)], env=env, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    if out.returncode == 0 and exe.is_file():
        return True, "link ok"
    detail = (out.stderr or out.stdout or "").strip().splitlines()
    return False, (detail[-1] if detail else "link failed")


def pick_toolchain(cargo: str, env: dict, quiet: bool) -> tuple[list[str], str | None, str, list[str]]:
    """Return ``(cargo_before_build, target, label, extra_bin_dirs)``.

    ``cargo_before_build`` are args that come before the subcommand (e.g.
    ``["+stable-x86_64-pc-windows-gnu"]``); ``target`` is a ``--target`` triple
    or None. Cargo requires ``+toolchain`` before the subcommand and
    ``--target`` after it.
    """
    ok, detail = probe_default_toolchain(cargo, env)
    if ok:
        _log(quiet, "[native] default toolchain links: using it (MSVC)")
        return [], None, "default (MSVC)", []

    _log(quiet, f"[native] default toolchain cannot link ({detail})")
    toolchains = rustup_toolchains(cargo)
    if GNU_TOOLCHAIN not in toolchains:
        raise ToolchainMissing(
            "No usable Rust linker found.\n"
            "  Either install Visual Studio Build Tools (C++ workload):\n"
            "      winget install Microsoft.VisualStudio.2022.BuildTools\n"
            "  or add the GNU toolchain (no admin needed):\n"
            "      rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal"
        )
    dlltool = find_dlltool()
    if not dlltool:
        raise ToolchainMissing(
            "The GNU toolchain needs 'dlltool' (binutils) to link against\n"
            "python3.dll. WinLibs provides everything, user-scope, no admin:\n"
            "    winget install BrechtSanders.WinLibs.POSIX.UCRT\n"
            "(or install MSYS2 and add C:\\msys64\\mingw64\\bin to PATH)"
        )
    extra = [str(Path(dlltool).parent)]
    _log(quiet, f"[native] GNU toolchain ready (dlltool: {dlltool})")
    return ["+" + GNU_TOOLCHAIN], GNU_TARGET, f"GNU ({GNU_TARGET})", extra


class ToolchainMissing(RuntimeError):
    pass


# ── status helpers ──────────────────────────────────────────────────────────


def artifact_status() -> dict:
    info: dict = {"path": str(PYD), "exists": PYD.is_file()}
    if info["exists"]:
        st = PYD.stat()
        info["size"] = st.st_size
        info["mtime"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        info["age_hours"] = (time.time() - st.st_mtime) / 3600.0
    return info


def verify_import(quiet: bool) -> tuple[bool, str]:
    if not PYD.is_file():
        return False, "not built"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(NATIVE)!r})\n"
        "import jarvis_native as jn\n"
        "info = dict(jn.native_info())\n"
        "print(info.get('version'), info.get('target'), info.get('profile'))\n"
    )
    try:
        out = _run([sys.executable, "-c", code], timeout=60.0)
    except subprocess.TimeoutExpired:
        return False, "import probe timed out"
    if out.returncode == 0:
        return True, (out.stdout or "").strip() or "imported"
    detail = (out.stderr or out.stdout or "").strip().splitlines()
    return False, (detail[-1] if detail else "import failed")


def print_status(quiet: bool) -> int:
    cargo = find_cargo()
    if not cargo:
        print("Rust toolchain :     NOT FOUND (install via https://rustup.rs or winget install Rustlang.Rustup)")
        print("Native module  :     not built (app runs pure-Python backend)")
        return 3
    ver = _run([cargo, "--version"], timeout=30.0)
    print(f"cargo          :     {(ver.stdout or '').strip() or 'unknown'}")
    env = os.environ.copy()
    try:
        _before, _target, label, _extra = pick_toolchain(cargo, env, quiet=True)
        print(f"linkable chain :     {label}")
    except ToolchainMissing as exc:
        print(f"linkable chain :     none — {exc}".replace("\n", "\n                     "))
    st = artifact_status()
    if st["exists"]:
        print(f"native module  :     {st['path']} ({st['size']} bytes, {st['mtime']}, {st['age_hours']:.1f} h old)")
    else:
        print("native module  :     not built (app runs pure-Python backend)")
    ok, detail = verify_import(quiet)
    print(f"import check   :     {'OK — ' + detail if ok else 'not available — ' + detail}")
    return 0 if ok else 3


# ── main flow ───────────────────────────────────────────────────────────────


def build(quiet: bool) -> int:
    cargo = find_cargo()
    if not cargo:
        print(
            "[native] cargo not found. Install Rust (https://rustup.rs) or run:\n"
            "    winget install Rustlang.Rustup\n"
            "The app works without it — this build is optional.",
            file=sys.stderr,
        )
        return 3

    env = os.environ.copy()
    env["PYO3_PYTHON"] = sys.executable  # abi3 config from the venv interpreter
    env.setdefault("CARGO_TERM_COLOR", "never")

    try:
        before, target, label, extra = pick_toolchain(cargo, env, quiet)
    except ToolchainMissing as exc:
        print(f"[native] {exc}", file=sys.stderr)
        print("[native] the app keeps running on the pure-Python backend.", file=sys.stderr)
        return 3

    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])

    cmd = [cargo, *before, "build", "--release"]
    if target:
        cmd += ["--target", target]
    _log(quiet, f"[native] building with {label} … (this can take minutes on the first run)")
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(NATIVE),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=BUILD_TIMEOUT,
            **_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        print(f"[native] build timed out after {BUILD_TIMEOUT:.0f}s", file=sys.stderr)
        return 1
    elapsed = time.time() - started

    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-25:])
        print(f"[native] cargo build failed:\n{tail}", file=sys.stderr)
        return 1

    # Locate the DLL: with --target it lands under target/<triple>/release/.
    if target:
        dll = NATIVE / "target" / target / "release" / "jarvis_native.dll"
    else:
        dll = NATIVE / "target" / "release" / "jarvis_native.dll"
    if not dll.is_file():
        # cargo names it .dll on Windows; tolerate other hosts for tests.
        alt = list(dll.parent.glob("jarvis_native.*"))
        if not alt:
            print(f"[native] build succeeded but {dll} was not found", file=sys.stderr)
            return 1
        dll = alt[0]

    shutil.copy2(dll, PYD)
    _log(quiet, f"[native] built in {elapsed:.0f}s → {PYD}")

    ok, detail = verify_import(quiet)
    if ok:
        print(f"[native] OK — jarvis_native {detail}")
        return 0
    print(f"[native] built, but the import check failed: {detail}", file=sys.stderr)
    print("[native] the app will fall back to the pure-Python backend.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the optional jarvis_native extension")
    parser.add_argument("--check", action="store_true", help="report status only, build nothing")
    parser.add_argument("--quiet", action="store_true", help="only problems + the final line")
    args = parser.parse_args(argv)
    if args.check:
        return print_status(args.quiet)
    return build(args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
