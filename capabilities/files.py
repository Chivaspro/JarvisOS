"""Real file work, with the destructive edge fenced off.

Why this exists
---------------
"Find the invoice from March", "copy that folder to my desktop", "zip these
files" are ordinary things to ask an assistant, and none of them need a model
to guess at a screen position. This module is the deterministic filesystem
controller: it resolves a path, does the operation with the standard library,
and reports exactly what changed.

What it will not do
-------------------
* It never permanently deletes. ``delete`` goes to the Recycle Bin through
  ``send2trash``; if that package is missing the action refuses instead of
  falling back to an irreversible unlink.
* It never deletes or moves a drive root, a Windows/Program Files tree, the
  user's home or one of its top-level folders, or this project itself.
* It never opens an executable. ``open`` is for documents and folders; a
  ``.exe``/``.bat``/``.ps1`` is refused and named.
* It never silently overwrites: an existing destination is reported unless the
  caller explicitly set ``overwrite``.
* It never extracts a zip outside the chosen folder (zip-slip entries are
  refused by name).
* Read-only actions (``list``, ``find``, ``info``) work even when the file
  switch is off, because looking at a folder is not changing it.

Every result names the absolute path it actually used.
"""

from __future__ import annotations

import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

__all__ = ["run", "status", "list_dir", "find", "info"]

_MAX_LIST = 500
_MAX_FIND = 200
_SEARCH_DEADLINE_S = 8.0

#: Extensions that run code when the shell opens them. ``open`` refuses these;
#: launching an application is ``pc_control.open_application``'s job, where the
#: permission switch and the activity row already live.
_EXECUTABLE_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js",
    ".jse", ".wsf", ".wsh", ".msi", ".msp", ".scr", ".pif", ".cpl", ".hta",
    ".jar", ".reg", ".lnk", ".url",
}

#: Folders that a filesystem assistant must never move or delete wholesale.
_PROTECTED_NAMES = (
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "System32", "SysWOW64", "Recovery", "Boot", "$Recycle.Bin",
)


def status() -> dict:
    """Whether the module can delete safely, probed (never assumed)."""
    try:
        import importlib.util
        trash = importlib.util.find_spec("send2trash") is not None
    except Exception:
        trash = False
    return {
        "available": True,
        "controller": "filesystem",
        "recycle_bin": trash,
        "detail": ("Filesystem control ready; deletes go to the Recycle Bin"
                   if trash else
                   "Filesystem control ready, but send2trash is missing — "
                   "delete will refuse rather than erase permanently"),
    }


# ── paths ─────────────────────────────────────────────────────────────────────

def _path(value, *, must_exist: bool = False) -> Path:
    """Resolve one user-supplied path (env vars and ``~`` expanded)."""
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("no path was given")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.home() / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"{resolved} does not exist")
    return resolved


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _protected(path: Path) -> str:
    """Why *path* must not be deleted or moved, or "" when it is safe."""
    try:
        target = path.resolve(strict=False)
    except OSError:
        target = path.absolute()
    if target.parent == target:
        return "that is a drive root"
    if target == _project_root():
        return "that is the J.A.R.V.I.S. project folder itself"
    home = Path.home().resolve(strict=False)
    if target == home:
        return "that is your user home folder"
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music",
                 "Videos", "OneDrive", "AppData"):
        if target == (home / name).resolve(strict=False):
            return f"that is your {name} folder itself"
    roots: list[Path] = []
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value).resolve(strict=False))
    for root in roots:
        try:
            if target == root or target.is_relative_to(root):
                return f"it is inside the protected system folder {root}"
        except (OSError, ValueError):
            continue
    for part in target.parts:
        if part in _PROTECTED_NAMES:
            return f"it is inside the protected system folder '{part}'"
    return ""


def _split_paths(value) -> list[Path]:
    """One or more paths from a comma/newline-separated string or a list."""
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        text = str(value or "")
        raw = [part for part in text.replace("\r", "\n").replace(",", "\n").split("\n")]
    return [_path(part) for part in raw if str(part).strip()]


def _kind(path: Path) -> str:
    try:
        if path.is_symlink():
            return "link"
        if path.is_dir():
            return "folder"
        return "file"
    except OSError:
        return "unknown"


def _stamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _size_text(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return (f"{value:.0f} {unit}" if unit == "B"
                    else f"{value:.1f} {unit}")
        value /= 1024.0
    return f"{value:.1f} TB"


def _entry(path: Path) -> dict:
    try:
        stat = path.stat()
        size = 0 if path.is_dir() else int(stat.st_size)
        modified = _stamp(stat.st_mtime)
    except OSError:
        size, modified = 0, ""
    return {"name": path.name, "path": str(path), "kind": _kind(path),
            "size": size, "modified": modified}


def _done(detail: str, **evidence) -> dict:
    return {"ok": True, "controller": "filesystem", "detail": detail,
            "evidence": evidence}


def _fail(reason: str, detail: str, **evidence) -> dict:
    return {"ok": False, "controller": "filesystem", "reason": reason,
            "detail": detail, "evidence": evidence}


# ── read-only actions ─────────────────────────────────────────────────────────

def _h_list(args: dict) -> dict:
    try:
        path = _path(args.get("path") or Path.home())
    except ValueError as exc:
        return _fail("bad_path", f"I cannot list that: {exc}.")
    if not path.exists():
        return _fail("not_found", f"{path} does not exist, so there is nothing to list.")
    if not path.is_dir():
        return _fail("not_a_folder", f"{path} is a file, not a folder. Use info instead.")
    try:
        limit = max(1, min(_MAX_LIST, int(args.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        entries = [_entry(child) for child in path.iterdir()]
    except PermissionError:
        return _fail("permission_denied", f"Windows denied access to {path}.")
    except OSError as exc:
        return _fail("list_failed", f"I could not read {path}: {exc}")
    entries.sort(key=lambda e: (e["kind"] != "folder", e["name"].casefold()))
    shown = entries[:limit]
    lines = []
    for item in shown:
        mark = "DIR " if item["kind"] == "folder" else "    "
        size = "" if item["kind"] == "folder" else f"  {_size_text(item['size'])}"
        lines.append(f"{mark}{item['name']}{size}  ({item['modified']})")
    body = "\n".join(lines) or "(empty folder)"
    detail = (f"{path} — {len(entries)} item(s), showing {len(shown)}:\n{body}")
    if len(entries) > len(shown):
        detail += f"\n… {len(entries) - len(shown)} more item(s)."
    return _done(detail, path=str(path), items=len(entries), shown=len(shown),
                 entries=shown)


def _h_find(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return _fail("no_query", "find needs the name (or part of it) to look for.")
    try:
        root = _path(args.get("path") or Path.home())
    except ValueError as exc:
        return _fail("bad_path", f"I cannot search that: {exc}.")
    if not root.exists():
        return _fail("not_found", f"{root} does not exist, so there is nothing to search.")
    if not root.is_dir():
        return _fail("not_a_folder", f"{root} is a file, not a folder to search in.")
    try:
        limit = max(1, min(_MAX_FIND, int(args.get("limit") or 50)))
    except (TypeError, ValueError):
        limit = 50
    wildcard = any(ch in query for ch in "*?[")
    wanted = query.casefold()
    skip = {"node_modules", ".git", ".venv", "__pycache__", ".cache",
            "site-packages", "$recycle.bin"}
    started = time.monotonic()
    matches: list[dict] = []
    truncated = False
    timed_out = False
    for current, dirs, files in os.walk(root):
        if time.monotonic() - started > _SEARCH_DEADLINE_S:
            timed_out = True
            break
        dirs[:] = [d for d in dirs if d.casefold() not in skip]
        names = list(dirs) + list(files)
        for name in names:
            hit = (Path(name).match(query) if wildcard
                   else wanted in name.casefold())
            if not hit:
                continue
            matches.append(_entry(Path(current) / name))
            if len(matches) >= limit:
                truncated = True
                break
        if truncated:
            break
    matches.sort(key=lambda e: (e["kind"] != "folder", e["name"].casefold()))
    lines = [f"{'DIR ' if m['kind'] == 'folder' else '    '}{m['path']}"
             for m in matches]
    detail = (f"Found {len(matches)} match(es) for '{query}' under {root}"
              + (" (stopped at the limit)" if truncated else "")
              + (" (stopped after the time limit)" if timed_out else "")
              + ":\n" + ("\n".join(lines) or "(none)"))
    return _done(detail, query=query, root=str(root), matches=matches,
                 truncated=bool(truncated or timed_out))


def _h_info(args: dict) -> dict:
    try:
        path = _path(args.get("path"), must_exist=True)
    except ValueError as exc:
        return _fail("bad_path", f"I cannot inspect that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    try:
        stat = path.stat()
    except OSError as exc:
        return _fail("stat_failed", f"I could not read {path}: {exc}")
    kind = _kind(path)
    bits = [f"{path}", f"kind: {kind}",
            f"size: {_size_text(stat.st_size)} ({stat.st_size} bytes)",
            f"modified: {_stamp(stat.st_mtime)}",
            f"created: {_stamp(stat.st_ctime)}"]
    if kind == "file":
        bits.append(f"extension: {path.suffix or '(none)'}")
        bits.append("read-only" if not os.access(path, os.W_OK) else "writable")
    elif kind == "folder":
        try:
            children = sum(1 for _ in path.iterdir())
            bits.append(f"direct children: {children}")
        except OSError:
            bits.append("direct children: could not be counted")
    return _done("\n".join(bits), path=str(path), kind=kind, size=stat.st_size,
                 modified=_stamp(stat.st_mtime))


# ── actions that change things ───────────────────────────────────────────────

def _h_open(args: dict) -> dict:
    try:
        path = _path(args.get("path"), must_exist=True)
    except ValueError as exc:
        return _fail("bad_path", f"I cannot open that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    suffix = path.suffix.casefold()
    if suffix in _EXECUTABLE_SUFFIXES:
        return _fail("executable_refused",
                     (f"{path.name} is an executable ({suffix}), so I have not "
                      "opened it. Use pc_control open_application when you really "
                      "mean to launch a program."),
                     path=str(path), suffix=suffix)
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except AttributeError:
        return _fail("unsupported_platform", "Opening a file is only supported on Windows.")
    except OSError as exc:
        return _fail("open_failed", f"Windows could not open {path}: {exc}")
    return _done(f"Asked Windows to open {path} with its default application.",
                 path=str(path), kind=_kind(path))


def _h_mkdir(args: dict) -> dict:
    try:
        path = _path(args.get("path"))
    except ValueError as exc:
        return _fail("bad_path", f"I cannot create that: {exc}.")
    if path.exists():
        if path.is_dir():
            return _done(f"{path} already exists as a folder; nothing needed creating.",
                         path=str(path), created=False)
        return _fail("exists_as_file", f"{path} already exists as a file, so I did "
                                       "not create a folder over it.")
    try:
        path.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return _fail("mkdir_failed", f"I could not create {path}: {exc}")
    return _done(f"Created the folder {path}.", path=str(path), created=True)


def _destination(source: Path, destination: Path, overwrite: bool) -> tuple[Path, str]:
    """Resolve where a copy/move should land, refusing a silent overwrite."""
    target = destination
    if destination.exists() and destination.is_dir():
        target = destination / source.name
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} already exists; I have not overwritten it. Say "
            "'overwrite' if that is what you want")
    return target, str(target)


def _copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _h_copy(args: dict) -> dict:
    try:
        source = _path(args.get("path"), must_exist=True)
        destination = _path(args.get("destination"))
    except ValueError as exc:
        return _fail("bad_path", f"I cannot copy that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    overwrite = bool(args.get("overwrite"))
    try:
        target, _ = _destination(source, destination, overwrite)
    except FileExistsError as exc:
        return _fail("destination_exists", str(exc) + ".",
                     source=str(source), destination=str(destination))
    existed = target.exists()
    try:
        if source.is_dir():
            _copy_tree(source, target)
            count = sum(1 for _ in target.rglob("*")) + 1
            detail = f"Copied the folder {source} to {target} ({count} item(s))."
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            detail = f"Copied {source} to {target}."
    except OSError as exc:
        return _fail("copy_failed", f"I could not copy {source} to {target}: {exc}",
                     source=str(source), destination=str(target))
    return _done(detail, source=str(source), destination=str(target),
                 kind=_kind(source), overwritten=bool(overwrite and existed))


def _h_move(args: dict) -> dict:
    try:
        source = _path(args.get("path"), must_exist=True)
        destination = _path(args.get("destination"))
    except ValueError as exc:
        return _fail("bad_path", f"I cannot move that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    blocked = _protected(source)
    if blocked:
        return _fail("protected_path", f"I have not moved {source}: {blocked}.",
                     source=str(source))
    overwrite = bool(args.get("overwrite"))
    try:
        target, _ = _destination(source, destination, overwrite)
    except FileExistsError as exc:
        return _fail("destination_exists", str(exc) + ".",
                     source=str(source), destination=str(destination))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        return _fail("move_failed", f"I could not move {source} to {target}: {exc}",
                     source=str(source), destination=str(target))
    return _done(f"Moved {source} to {target}.", source=str(source),
                 destination=str(target))


def _h_rename(args: dict) -> dict:
    try:
        source = _path(args.get("path"), must_exist=True)
        raw = str(args.get("destination") or "").strip()
        if not raw:
            return _fail("no_destination", "rename needs the new name or path.")
        destination = _path(raw)
        if not destination.is_absolute():
            destination = source.parent / destination
    except ValueError as exc:
        return _fail("bad_path", f"I cannot rename that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    # A bare name is a rename in place; a path with a parent is a move.
    if os.sep not in raw and "/" not in raw:
        destination = source.parent / raw
    blocked = _protected(source)
    if blocked:
        return _fail("protected_path", f"I have not renamed {source}: {blocked}.",
                     source=str(source))
    if destination.exists() and destination != source:
        return _fail("destination_exists",
                     f"{destination} already exists, so I have not renamed over it.",
                     source=str(source), destination=str(destination))
    try:
        source.rename(destination)
    except OSError as exc:
        return _fail("rename_failed", f"I could not rename {source}: {exc}")
    return _done(f"Renamed {source.name} to {destination.name} "
                 f"({destination}).", source=str(source),
                 destination=str(destination))


def _h_delete(args: dict) -> dict:
    try:
        path = _path(args.get("path"), must_exist=True)
    except ValueError as exc:
        return _fail("bad_path", f"I cannot delete that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    blocked = _protected(path)
    if blocked:
        return _fail("protected_path", f"I have not deleted {path}: {blocked}.",
                     path=str(path))
    try:
        from send2trash import send2trash
    except Exception:
        return _fail("recycle_bin_unavailable",
                     ("I have not deleted anything: send2trash is not installed, "
                      "and I will not erase a file permanently. Install it with "
                      "'pip install send2trash'."),
                     path=str(path))
    kind = _kind(path)
    try:
        send2trash(str(path))
    except Exception as exc:
        return _fail("delete_failed", f"I could not move {path} to the Recycle "
                                      f"Bin: {exc}", path=str(path))
    return _done(f"Moved {path} to the Recycle Bin (recoverable there).",
                 path=str(path), kind=kind, recycle_bin=True)


def _h_zip(args: dict) -> dict:
    raw_paths = args.get("paths") or args.get("path")
    try:
        sources = _split_paths(raw_paths)
    except ValueError as exc:
        return _fail("bad_path", f"I cannot zip that: {exc}.")
    if not sources:
        return _fail("no_paths", "zip needs at least one file or folder to include.")
    missing = [str(p) for p in sources if not p.exists()]
    if missing:
        return _fail("not_found", "These do not exist, so nothing was zipped: "
                                  + ", ".join(missing))
    try:
        target = _path(args.get("save_as") or args.get("destination")
                       or (Path.home() / "Documents"))
    except ValueError as exc:
        return _fail("bad_path", f"I cannot write the archive there: {exc}.")
    if target.suffix.casefold() != ".zip":
        target = target / f"archive-{datetime.now():%Y%m%d-%H%M%S}.zip" \
            if (target.exists() and target.is_dir()) or not target.suffix \
            else target.with_suffix(".zip")
    if target.exists() and not bool(args.get("overwrite")):
        return _fail("destination_exists",
                     f"{target} already exists; I have not overwritten it.",
                     destination=str(target))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sources:
                if source.is_dir():
                    archive.write(source, source.name)
                    count += 1
                    for child in source.rglob("*"):
                        if child.is_file():
                            archive.write(child, child.relative_to(source.parent))
                            count += 1
                else:
                    archive.write(source, source.name)
                    count += 1
    except OSError as exc:
        return _fail("zip_failed", f"I could not write {target}: {exc}")
    except zipfile.BadZipFile as exc:
        return _fail("zip_failed", f"The archive could not be written: {exc}")
    size = target.stat().st_size if target.exists() else 0
    return _done(f"Created {target} with {count} entr(y/ies) "
                 f"({_size_text(size)}).", path=str(target), entries=count,
                 size=size, sources=[str(p) for p in sources])


def _h_unzip(args: dict) -> dict:
    try:
        source = _path(args.get("path"), must_exist=True)
    except ValueError as exc:
        return _fail("bad_path", f"I cannot unzip that: {exc}.")
    except FileNotFoundError as exc:
        return _fail("not_found", f"{exc}.")
    if not zipfile.is_zipfile(source):
        return _fail("not_a_zip", f"{source} is not a zip archive.")
    try:
        destination = _path(args.get("destination") or (source.parent / source.stem))
    except ValueError as exc:
        return _fail("bad_path", f"I cannot extract there: {exc}.")
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            unsafe = []
            for member in members:
                try:
                    target = (destination / member.filename).resolve(strict=False)
                    if not target.is_relative_to(destination.resolve(strict=False)):
                        unsafe.append(member.filename)
                except (OSError, ValueError):
                    unsafe.append(member.filename)
            if unsafe:
                return _fail("unsafe_archive",
                             ("I have not extracted this archive: it contains "
                              "entries that would land outside the destination "
                              "(zip-slip): " + ", ".join(unsafe[:5])),
                             path=str(source), unsafe=unsafe[:20])
            destination.mkdir(parents=True, exist_ok=True)
            archive.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        return _fail("unzip_failed", f"I could not extract {source}: {exc}")
    return _done(f"Extracted {len(members)} entr(y/ies) from {source} into "
                 f"{destination}.", path=str(source), destination=str(destination),
                 entries=len(members))


_HANDLERS = {
    "list": _h_list,
    "find": _h_find,
    "info": _h_info,
    "open": _h_open,
    "mkdir": _h_mkdir,
    "copy": _h_copy,
    "move": _h_move,
    "rename": _h_rename,
    "delete": _h_delete,
    "zip": _h_zip,
    "unzip": _h_unzip,
}


# ── entry points ──────────────────────────────────────────────────────────────

def list_dir(args: dict | None = None) -> dict:
    return _h_list(dict(args or {}))


def find(args: dict | None = None) -> dict:
    return _h_find(dict(args or {}))


def info(args: dict | None = None) -> dict:
    return _h_info(dict(args or {}))


def run(action: str, args: dict) -> dict:
    """Execute one file action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action or "").strip())
    if handler is None:
        return {"ok": False, "controller": "filesystem",
                "detail": f"'{action}' is not a file action."}
    try:
        return handler(dict(args or {})) or {
            "ok": False, "controller": "filesystem", "detail": "No result."}
    except Exception as exc:
        return {"ok": False, "controller": "filesystem", "reason": "crashed",
                "detail": f"File control failed: {type(exc).__name__}: {exc}"}
