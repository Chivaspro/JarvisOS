"""Structured browser automation through Playwright.

One J.A.R.V.I.S.-owned browser session, driven by semantic selectors — role,
label, placeholder, test id, visible text, CSS — and never by screen
coordinates. That is the whole point of this layer: when the page can describe
an element, the click is deterministic and the visual layer is not involved.

Two details shape the implementation:

* Playwright's synchronous objects belong to the thread that created them, and
  main.py dispatches tools on a thread pool where consecutive calls can land on
  different threads. The session therefore lives on one dedicated owner thread
  (``jarvis-playwright``) and every action is executed there through a queue.
* A persistent profile under the project's automation directory keeps site
  logins between sessions. The user's installed Chrome is used when present and
  the bundled Chromium otherwise — the choice is reported, never assumed.
* One action (``browser_capture``) exists for reverse-engineering: it records
  the DOM, a screenshot, computed landmark styles and the page's own network
  traffic into a folder, so a clean remake can be synthesised from evidence.
  Credential and session headers are stripped before anything touches the disk.

Nothing here pretends: a missing Playwright, a missing browser binary, or a
selector that matches nothing all come back as an honest failure with the reason.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path

_CALL_TIMEOUT_S = 120.0

#: One capture legitimately runs longer than a normal action: the page load,
#: the bounded wait for a quiet network and a full-page screenshot add up.
_CAPTURE_TIMEOUT_S = 180.0

#: Header values that never belong in a saved capture. Credentials and session
#: cookies are removed by name before any file is written.
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "www-authenticate", "x-api-key", "x-auth-token",
})

_lock = threading.RLock()
_thread: threading.Thread | None = None
_requests: "queue.Queue[tuple | None]" = queue.Queue()
#: Owner-thread state: playwright, context, page (touched only by that thread).
_state: dict = {"ready": False, "pw": None, "context": None, "page": None,
                "channel": "", "profile": "", "error": "", "started_at": 0.0}


# ── availability ──────────────────────────────────────────────────────────────

def installed() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def browsers_installed() -> bool:
    """True when a Playwright browser binary exists in the shared cache."""
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                or Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    try:
        return root.is_dir() and any(root.glob("chromium*"))
    except OSError:
        return False


def status() -> dict:
    """Live state for diagnostics and Settings (never starts the browser)."""
    with _lock:
        running = bool(_thread and _thread.is_alive())
        channel = _state.get("channel") or ""
        error = _state.get("error") or ""
    have = installed()
    ready = have and browsers_installed()
    return {
        "installed": have,
        "browsers": browsers_installed(),
        "running": running,
        "available": ready,
        "channel": channel,
        "detail": (error if error else
                   ("running on " + channel if running and channel else
                    "installed, not started" if ready else
                    "playwright is not installed" if not have else
                    "no Playwright browser is installed (run 'playwright install chromium')")),
    }


def _chrome_channel() -> str:
    """The configured channel, or the first browser actually present."""
    try:
        from capabilities import config
        wanted = (config.browser_channel() or "").strip()
    except Exception:
        wanted = ""
    if wanted and wanted not in ("chromium", "bundled"):
        return wanted
    for name in ("chrome", "msedge"):
        if _find_chrome(name):
            return name
    return ""


def _find_chrome(channel: str) -> str:
    import shutil
    candidates = {
        "chrome": ("chrome.exe", "chrome"),
        "msedge": ("msedge.exe", "msedge"),
    }.get(channel, ())
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    for base in (os.environ.get("PROGRAMFILES", ""),
                 os.environ.get("PROGRAMFILES(X86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        exe = {"chrome": Path(base) / "Google/Chrome/Application/chrome.exe",
               "msedge": Path(base) / "Microsoft/Edge/Application/msedge.exe"}.get(channel)
        if exe and exe.exists():
            return str(exe)
    return ""


def _launch(pw) -> dict:
    """Start the persistent context on the owner thread. Owner-thread only."""
    from capabilities import config
    profile = Path(config.browser_profile_dir())
    downloads = Path(config.downloads_dir())
    try:
        profile.mkdir(parents=True, exist_ok=True)
        downloads.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    channel = _chrome_channel()
    kwargs: dict = {
        "user_data_dir": str(profile),
        "accept_downloads": True,
        "downloads_path": str(downloads),
        "headless": bool(config.browser_headless()),
        "viewport": None,
        "args": ["--no-first-run", "--no-default-browser-check"],
    }
    if channel:
        kwargs["channel"] = channel
    try:
        context = pw.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:
        # A channel that will not launch (moved install, blocked binary) must
        # not take the whole layer down: fall back to the bundled Chromium and
        # say so.
        if "channel" in kwargs:
            failed = kwargs.pop("channel")
            try:
                context = pw.chromium.launch_persistent_context(**kwargs)
            except Exception as exc2:
                return {"error": f"{failed} and the bundled Chromium both failed "
                                 f"to start: {exc2}"}
            channel = ""
        else:
            return {"error": f"the browser could not start: {exc}"}
    context.set_default_timeout(config.browser_timeout() * 1000)
    page = context.pages[0] if context.pages else context.new_page()
    return {"context": context, "page": page, "channel": channel or "chromium",
            "profile": str(profile)}


def _owner_loop() -> None:
    """The session thread: owns every Playwright object, serves one request at a time."""
    global _thread
    pw = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        started = _launch(pw)
    except Exception as exc:
        with _lock:
            _state.update({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        _drain_with_error(_state["error"])
        with _lock:
            _thread = None
        return

    if "error" in started:
        with _lock:
            _state.update({"ready": False, "error": started["error"]})
        try:
            pw.stop()
        except Exception:
            pass
        _drain_with_error(started["error"])
        with _lock:
            _thread = None
        return

    with _lock:
        _state.update({"ready": True, "pw": pw, "context": started["context"],
                       "page": started["page"], "channel": started["channel"],
                       "profile": started["profile"], "error": "",
                       "started_at": time.time()})
    _log(f"browser ready on {started['channel']} (profile {started['profile']})")

    while True:
        item = _requests.get()
        if item is None:
            break
        fn, args, future = item
        if future.set_running_or_notify_cancel() is False:
            continue
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)

    _teardown()


def _drain_with_error(message: str) -> None:
    while True:
        try:
            item = _requests.get_nowait()
        except queue.Empty:
            return
        if item is None:
            continue
        fn, args, future = item
        if not future.cancelled():
            future.set_result({"ok": False, "controller": "playwright",
                               "detail": message, "evidence": {}})


def _teardown() -> None:
    with _lock:
        context, pw = _state.get("context"), _state.get("pw")
        _state.update({"ready": False, "context": None, "page": None, "pw": None})
    for closeable in (context, pw):
        try:
            if closeable is not None:
                closeable.close() if closeable is context else closeable.stop()
        except Exception:
            pass
    _log("browser session closed")


def _log(message: str) -> None:
    try:
        from observability.logger import log_event
        log_event("INFO", "capabilities", "browser_playwright", detail=message[:400])
        return
    except Exception:
        pass
    try:
        print(f"[Playwright] {message}")
    except Exception:
        pass


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_owner_loop, name="jarvis-playwright",
                                   daemon=True)
        _thread.start()


def _call(fn, *args, timeout_s: float = _CALL_TIMEOUT_S) -> dict:
    """Run *fn* on the session thread and return its result (or an honest error)."""
    if not installed():
        return {"ok": False, "controller": "playwright",
                "detail": "Playwright is not installed, so the browser layer is "
                          "unavailable. No other browser engine was substituted.",
                "evidence": {}}
    _ensure_thread()
    future: Future = Future()
    _requests.put((fn, args, future))
    try:
        return future.result(timeout=timeout_s)
    except Exception as exc:
        detail = str(exc)
        with _lock:
            stored = _state.get("error") or ""
        return {"ok": False, "controller": "playwright",
                "detail": detail or stored or "The browser action failed.",
                "evidence": {}}


def close(reason: str = "shutdown") -> bool:
    """Stop the session thread and the browser (idempotent)."""
    global _thread
    with _lock:
        alive = bool(_thread and _thread.is_alive())
        _thread = None
    if not alive:
        return False
    _requests.put(None)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        with _lock:
            if not _state.get("ready"):
                break
        time.sleep(0.1)
    _log(f"close requested ({reason})")
    return True


# ── page helpers (owner thread only) ──────────────────────────────────────────

def _dom():
    return _state.get("context"), _state.get("page")


def _page():
    """The active page, creating one when a tab was closed (owner thread)."""
    context, page = _dom()
    if context is None:
        raise RuntimeError("the browser session is not running")
    try:
        if page is None or page.is_closed():
            raise RuntimeError("no")
        return page
    except Exception:
        page = context.new_page()
        _state["page"] = page
        return page


def _ok(detail: str, **evidence) -> dict:
    return {"ok": True, "controller": "playwright", "detail": detail,
            "evidence": {k: v for k, v in evidence.items() if v is not None}}


def _fail(detail: str, **evidence) -> dict:
    return {"ok": False, "controller": "playwright", "detail": detail,
            "evidence": {k: v for k, v in evidence.items() if v is not None}}


#: Engine-prefixed selectors Playwright understands directly.
_ENGINE_PREFIXES = ("role=", "text=", "label=", "placeholder=", "testid=",
                    "test-id=", "alt=", "title=", "css=", "xpath=", "id=",
                    "data-testid=")


def _candidates(page, target: str) -> list[tuple[str, object]]:
    """Ordered locator strategies for a free-text element name.

    Most semantic first: a form label, a placeholder, a test id, an accessible
    role+name, then visible text, then DOM attributes. CSS and explicit engine
    selectors are used as given; screen coordinates are never used here.
    """
    return [
        ("label", lambda: page.get_by_label(target)),
        ("placeholder", lambda: page.get_by_placeholder(target)),
        ("testid", lambda: page.get_by_test_id(target)),
        ("role-name", lambda: page.get_by_role("button", name=target, exact=True)),
        ("role-link", lambda: page.get_by_role("link", name=target, exact=True)),
        ("text-exact", lambda: page.get_by_text(target, exact=True)),
        ("alt", lambda: page.get_by_alt_text(target)),
        ("title", lambda: page.get_by_title(target)),
        ("text", lambda: page.get_by_text(target)),
        ("css-name", lambda: page.locator(f'[name="{target}"]')),
        ("css-aria", lambda: page.locator(f'[aria-label="{target}"]')),
        ("text-css", lambda: page.locator(f"text={target}")),
    ]


def _explicit(page, args: dict):
    """A selector the caller supplied verbatim ('' when none was given)."""
    explicit = str(args.get("selector") or "").strip()
    if explicit:
        return page.locator(explicit), "selector"
    target = str(args.get("target") or "").strip()
    if target and target.lower().startswith(_ENGINE_PREFIXES):
        return page.locator(target), target.split("=", 1)[0] + "="
    return None, ""


def _locator(page, args: dict):
    """Resolve the requested element, preferring semantic strategies.

    Returns ``(locator, strategy, count)``. The strategy is reported back so the
    model (and the user) can see whether a label, a test id or a plain CSS
    selector carried the click — coordinates are never used here.
    """
    explicit_loc, explicit_name = _explicit(page, args)
    if explicit_loc is not None:
        return explicit_loc, explicit_name, explicit_loc.count()
    target = str(args.get("target") or "").strip()
    if not target:
        raise ValueError("no element was named (give 'target' or 'selector')")

    attempts = _candidates(page, target)
    found: list[tuple[str, object, int]] = []
    for name, build in attempts:
        try:
            loc = build()
            count = loc.count()
        except Exception:
            continue
        if count:
            found.append((name, loc, count))
        if count == 1:
            break
    if not found:
        raise LookupError(f"nothing on the page matched '{target}'")
    name, loc, count = found[0]
    if count > 1:
        loc = loc.first
    return loc, name, count


def _normalise_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise ValueError("no URL was given")
    if "://" not in text and not text.startswith(("about:", "data:")):
        text = "https://" + text.lstrip("/")
    return text


def _page_state(page) -> dict:
    try:
        return {"page_url": page.url, "page_title": (page.title() or "")[:160]}
    except Exception:
        return {}


def _ms(args: dict, default_s: float) -> int:
    try:
        given = float(args.get("timeout_s") or 0)
    except (TypeError, ValueError):
        given = 0.0
    return int(max(1.0, given or default_s) * 1000)


# ── action handlers (owner thread only) ───────────────────────────────────────

def _a_open(args: dict) -> dict:
    page = _page()
    url = _normalise_url(args.get("url"))
    page.goto(url, wait_until="domcontentloaded")
    return _ok(f"Opened {page.url}", **_page_state(page))


def _a_back(args: dict) -> dict:
    page = _page()
    page.go_back(wait_until="domcontentloaded")
    return _ok(f"Went back to {page.url}", **_page_state(page))


def _a_forward(args: dict) -> dict:
    page = _page()
    page.go_forward(wait_until="domcontentloaded")
    return _ok(f"Went forward to {page.url}", **_page_state(page))


def _a_new_tab(args: dict) -> dict:
    context, _page_current = _dom()
    if context is None:
        raise RuntimeError("the browser session is not running")
    page = context.new_page()
    _state["page"] = page
    url = str(args.get("url") or "").strip()
    if url:
        page.goto(_normalise_url(url), wait_until="domcontentloaded")
    return _ok(f"Opened a new tab ({len(context.pages)} open)", **_page_state(page))


def _match_tab(context, want: str):
    text = str(want or "").strip()
    if not text:
        return _state.get("page")
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(context.pages):
            return context.pages[index]
        raise LookupError(f"there is no tab {text} (only {len(context.pages)} open)")
    low = text.lower()
    for page in context.pages:
        try:
            if low in (page.url or "").lower() or low in (page.title() or "").lower():
                return page
        except Exception:
            continue
    raise LookupError(f"no open tab matched '{text}'")


def _a_switch_tab(args: dict) -> dict:
    context, _current = _dom()
    if context is None:
        raise RuntimeError("the browser session is not running")
    page = _match_tab(context, args.get("tab"))
    if page is None:
        raise LookupError("there is no tab to switch to")
    page.bring_to_front()
    _state["page"] = page
    return _ok(f"Switched to {page.url}", tabs=len(context.pages), **_page_state(page))


def _a_close_tab(args: dict) -> dict:
    context, _current = _dom()
    if context is None:
        raise RuntimeError("the browser session is not running")
    page = _match_tab(context, args.get("tab")) or _state.get("page")
    if page is None:
        return _fail("There was no tab to close.")
    try:
        page.close()
    except Exception as exc:
        return _fail(f"The tab would not close: {exc}")
    remaining = context.pages
    _state["page"] = remaining[-1] if remaining else context.new_page()
    return _ok(f"Closed the tab ({len(remaining)} still open)",
               tabs=len(remaining), **_page_state(_state["page"]))


def _a_click(args: dict) -> dict:
    page = _page()
    loc, strategy, count = _locator(page, args)
    button = str(args.get("button") or "left").strip().lower()
    loc.click(button=button, timeout=_ms(args, 30.0))
    return _ok(f"Clicked the element matched by {strategy}"
               + (f" ({count} matched, used the first)" if count > 1 else ""),
               strategy=strategy, matches=count, **_page_state(page))


def _a_fill(args: dict) -> dict:
    page = _page()
    loc, strategy, count = _locator(page, args)
    text = str(args.get("text") or "")
    if args.get("clear") is False:
        loc.type(text, timeout=_ms(args, 30.0))
    else:
        loc.fill(text, timeout=_ms(args, 30.0))
    shown = text if len(text) <= 60 else text[:59] + "…"
    return _ok(f"Filled the field matched by {strategy} with: {shown}",
               strategy=strategy, matches=count, chars=len(text), **_page_state(page))


def _a_press(args: dict) -> dict:
    page = _page()
    key = str(args.get("key") or "").strip()
    if not key:
        return _fail("browser_press needs a key.")
    if args.get("target") or args.get("selector"):
        loc, strategy, count = _locator(page, args)
        loc.press(key, timeout=_ms(args, 30.0))
        return _ok(f"Pressed {key} in the element matched by {strategy}",
                   strategy=strategy, key=key, **_page_state(page))
    page.keyboard.press(key)
    return _ok(f"Pressed {key}", key=key, **_page_state(page))


def _a_select(args: dict) -> dict:
    page = _page()
    loc, strategy, count = _locator(page, args)
    want = str(args.get("text") or "")
    try:
        loc.select_option(label=want, timeout=_ms(args, 20.0))
        how = "label"
    except Exception:
        loc.select_option(value=want, timeout=_ms(args, 20.0))
        how = "value"
    return _ok(f"Selected '{want}' by {how} in the element matched by {strategy}",
               strategy=strategy, matches=count, option=want, how=how,
               **_page_state(page))


def _a_scroll(args: dict) -> dict:
    page = _page()
    direction = str(args.get("direction") or "").strip().lower()
    if direction not in ("up", "down", "left", "right"):
        return _fail("scroll needs a direction of up, down, left or right.")
    try:
        amount = int(args.get("amount") or 3)
    except (TypeError, ValueError):
        amount = 3
    if args.get("target") or args.get("selector"):
        loc, strategy, count = _locator(page, args)
        loc.scroll_into_view_if_needed(timeout=_ms(args, 20.0))
        return _ok(f"Scrolled the element matched by {strategy} into view",
                   strategy=strategy, matches=count, **_page_state(page))
    step = max(1, abs(amount)) * 400
    dx = step if direction == "right" else -step if direction == "left" else 0
    dy = step if direction == "down" else -step if direction == "up" else 0
    page.mouse.wheel(dx, dy)
    return _ok(f"Scrolled {direction}", direction=direction, amount=amount,
               **_page_state(page))


def _a_read(args: dict) -> dict:
    page = _page()
    if args.get("target") or args.get("selector"):
        loc, strategy, count = _locator(page, args)
        text = loc.inner_text(timeout=_ms(args, 20.0))
        return _ok(text.strip()[:4000] or "(the element was empty)",
                   strategy=strategy, matches=count, **_page_state(page))
    text = page.inner_text("body")
    clean = " ".join(text.split())
    return _ok(clean[:4000] or "(the page had no readable text)",
               chars=len(clean), **_page_state(page))


def _wait_for_element(page, args: dict, timeout_ms: int):
    """Wait for an element that need not exist yet.

    Unlike the strict resolution used by click/fill, this does NOT fail when
    nothing matches right now — that is the whole point of a wait. Strategies
    are tried in order, each given the remaining time, so the total wait stays
    within the requested timeout.
    """
    explicit_loc, explicit_name = _explicit(page, args)
    if explicit_loc is not None:
        explicit_loc.first.wait_for(state="visible", timeout=timeout_ms)
        return explicit_loc, explicit_name
    target = str(args.get("target") or "").strip()
    if not target:
        raise ValueError("no element or URL was named to wait for")
    # Poll cheaply across every strategy until one matches. Handing the whole
    # budget to the first strategy would break waits for text that only a later
    # one can find (a label lookup never matches a plain link).
    deadline = time.monotonic() + timeout_ms / 1000.0
    candidates = _candidates(page, target)
    while True:
        for name, build in candidates:
            try:
                loc = build().first
                if loc.count() <= 0:
                    continue
                remaining = int(max(0.5, deadline - time.monotonic()) * 1000)
                loc.wait_for(state="visible", timeout=min(remaining, 4000))
                return loc, name
            except Exception:
                continue
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(150)
    raise LookupError(f"nothing appeared on the page matching '{target}' "
                      f"within {timeout_ms / 1000.0:.0f}s")


def _a_wait(args: dict) -> dict:
    page = _page()
    timeout_ms = _ms(args, 15.0)
    if args.get("target") or args.get("selector"):
        _loc, strategy = _wait_for_element(page, args, timeout_ms)
        return _ok(f"The element matched by {strategy} is visible",
                   strategy=strategy, **_page_state(page))
    url = str(args.get("url") or "").strip()
    if url:
        page.wait_for_url(f"**{url}**", timeout=timeout_ms)
        return _ok(f"The page reached {url}", **_page_state(page))
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    return _ok("The page finished loading", **_page_state(page))


def _a_upload(args: dict) -> dict:
    page = _page()
    path = str(args.get("path") or "").strip()
    if not path or not Path(path).exists():
        return _fail(f"The file to upload was not found: {path or '(none given)'}")
    if args.get("target") or args.get("selector"):
        loc, strategy, count = _locator(page, args)
    else:
        loc, strategy, count = page.locator('input[type="file"]').first, "file-input", 1
    loc.set_input_files(path, timeout=_ms(args, 30.0))
    return _ok(f"Uploaded {Path(path).name}", strategy=strategy, matches=count,
               file=Path(path).name, **_page_state(page))


def _a_download(args: dict) -> dict:
    from capabilities import config
    page = _page()
    loc, strategy, count = _locator(page, args)
    folder = Path(str(args.get("path") or "").strip() or config.downloads_dir())
    folder.mkdir(parents=True, exist_ok=True)
    with page.expect_download(timeout=_ms(args, 60.0)) as info:
        loc.click(timeout=_ms(args, 30.0))
    download = info.value
    name = download.suggested_filename or "download"
    target = folder / name
    download.save_as(str(target))
    return _ok(f"Downloaded {name} to {folder}", path=str(target), file=name,
               strategy=strategy, matches=count, **_page_state(page))


def _a_screenshot(args: dict) -> dict:
    from capabilities import desktop
    page = _page()
    path = str(args.get("path") or "").strip() or desktop.default_artifact_path("page")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=path)
    return _ok(f"Captured the page to {path}", path=path, image=path,
               **_page_state(page))


# ── capture (the reverse-engineering evidence recorder) ───────────────────────
# One action that writes everything a remake needs from a page: the DOM, a
# screenshot, the computed styles of its landmark elements, and the network
# traffic its own load produced (as a HAR 1.2 document). The listeners are
# attached BEFORE the navigation or reload so the whole load is recorded, and
# removed afterwards so a later action never inherits them.

#: Reads the computed styles and geometry of the visible landmark elements.
#: Bounded (60 elements, short text) so the artifact stays small and readable.
_STYLE_PROBE_JS = """() => {
  const nodes = document.querySelectorAll(
    'header,nav,main,section,article,footer,h1,h2,h3,button,a,input,textarea,select,form,ul,li,img');
  const elements = [];
  for (const el of nodes) {
    if (elements.length >= 60) break;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    const cs = getComputedStyle(el);
    elements.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      classes: (typeof el.className === 'string' ? el.className : '').slice(0, 120),
      text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
      rect: {x: Math.round(rect.x), y: Math.round(rect.y),
             width: Math.round(rect.width), height: Math.round(rect.height)},
      styles: {
        color: cs.color,
        backgroundColor: cs.backgroundColor,
        fontFamily: cs.fontFamily,
        fontSize: cs.fontSize,
        fontWeight: cs.fontWeight,
        lineHeight: cs.lineHeight,
        display: cs.display,
        position: cs.position,
        textAlign: cs.textAlign,
        padding: cs.padding,
        margin: cs.margin,
        borderRadius: cs.borderRadius,
        gap: cs.gap,
      },
    });
  }
  return {title: document.title || '', url: location.href,
          viewport: {width: innerWidth, height: innerHeight},
          elements: elements};
}"""


def _safe_headers(headers) -> dict:
    """The response/request headers with credentials removed by name."""
    try:
        return {str(name): str(value)
                for name, value in dict(headers or {}).items()
                if str(name).lower() not in _SENSITIVE_HEADERS}
    except Exception:
        return {}


def _har_from_records(records: list[dict]) -> dict:
    """A minimal, standard HAR 1.2 document built from the recorded traffic."""
    entries = []
    for record in records:
        started = float(record.get("started") or 0.0)
        ended = float(record.get("ended") or started)
        if not started:
            started = time.time() * 1000.0
            ended = ended or started
        url = str(record.get("url") or "")
        request_headers = record.get("headers") or {}
        response_headers = record.get("response_headers") or {}
        content_type = ""
        for name, value in response_headers.items():
            if str(name).lower() == "content-type":
                content_type = str(value)
                break
        entries.append({
            "startedDateTime": (datetime.fromtimestamp(started / 1000.0,
                                                       tz=timezone.utc)
                                .isoformat().replace("+00:00", "Z")),
            "time": max(0.0, ended - started),
            "request": {
                "method": str(record.get("method") or "GET"),
                "url": url,
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": [{"name": n, "value": v}
                            for n, v in request_headers.items()],
                "queryString": [],
                "headersSize": -1,
                "bodySize": -1,
            },
            "response": {
                "status": int(record.get("status") or 0),
                "statusText": str(record.get("status_text") or ""),
                "httpVersion": "HTTP/1.1",
                "cookies": [],
                "headers": [{"name": n, "value": v}
                            for n, v in response_headers.items()],
                "content": {"size": -1, "mimeType": content_type},
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": -1,
            },
            "cache": {},
            "timings": {"send": 0, "wait": max(0.0, ended - started),
                        "receive": 0},
            "_resourceType": str(record.get("resource_type") or ""),
        })
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "jarvis-browser-capture", "version": "1.0"},
            "pages": [],
            "entries": entries,
        }
    }


def _a_capture(args: dict) -> dict:
    """Record a page as evidence: DOM, screenshot, styles and network (HAR)."""
    from capabilities import desktop
    page = _page()
    url = str(args.get("url") or "").strip()
    folder = Path(str(args.get("path") or "").strip()
                  or Path(desktop.default_artifact_path("capture")).with_suffix(""))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail(f"The capture folder could not be created: {exc}")

    by_request: dict[int, dict] = {}

    def _on_request(request):
        try:
            by_request[id(request)] = {
                # Keeping the wrapper alive fixes id() as the stable key that
                # the matching response event finds again.
                "request": request,
                "started": time.time() * 1000.0,
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "headers": _safe_headers(request.headers),
            }
        except Exception:
            pass

    def _on_response(response):
        try:
            record = by_request.get(id(response.request))
            if record is None:
                return
            record["status"] = response.status
            record["status_text"] = response.status_text
            record["response_headers"] = _safe_headers(response.headers)
            record["ended"] = time.time() * 1000.0
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)
    notes: list[str] = []
    try:
        try:
            if url:
                page.goto(_normalise_url(url), wait_until="domcontentloaded")
            else:
                page.reload(wait_until="domcontentloaded")
        except Exception as exc:
            return _fail(f"The page could not be loaded for capture: {exc}")
        # The network is never guaranteed to go quiet; the wait is bounded and
        # a timeout is not a capture failure — the records collected so far are
        # still real evidence.
        try:
            page.wait_for_load_state("networkidle", timeout=_ms(args, 15.0))
        except Exception:
            notes.append("the network did not go fully quiet before the capture")

        try:
            html = page.content()
        except Exception as exc:
            return _fail(f"The DOM could not be read: {exc}")
        html_path = folder / "dom.html"
        styles_path = folder / "styles.json"
        har_path = folder / "network.har"
        image_path = folder / "page.png"
        try:
            html_path.write_text(html, encoding="utf-8")
        except OSError as exc:
            return _fail(f"The DOM could not be written: {exc}")

        try:
            page.screenshot(path=str(image_path), full_page=True)
        except Exception:
            try:
                page.screenshot(path=str(image_path))
                notes.append("only the visible area was captured (full-page "
                             "screenshot failed)")
            except Exception as exc:
                image_path = None
                notes.append(f"the screenshot failed: {exc}")

        try:
            styles = page.evaluate(_STYLE_PROBE_JS)
        except Exception as exc:
            styles = {"title": "", "url": page.url, "elements": [],
                      "error": f"{type(exc).__name__}: {exc}"}
            notes.append("the computed styles could not be read")
        try:
            styles_path.write_text(json.dumps(styles, indent=2), encoding="utf-8")
        except OSError as exc:
            # The styles are supplementary evidence; the DOM and the traffic
            # are what the remake depends on, so a failure here is a note.
            styles_path = None
            notes.append(f"the styles could not be written: {exc}")

        records = sorted(by_request.values(), key=lambda r: r.get("started", 0.0))
        har = _har_from_records(records)
        try:
            har_path.write_text(json.dumps(har, indent=2), encoding="utf-8")
        except OSError as exc:
            return _fail(f"The network capture could not be written: {exc}")
    finally:
        for event, handler in (("request", _on_request),
                               ("response", _on_response)):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass

    count = len(har["log"]["entries"])
    elements = len(styles.get("elements") or []) if isinstance(styles, dict) else 0
    detail = (f"Captured {page.url or 'the page'} — {count} network record(s), "
              f"DOM {len(html)} chars, styles for {elements} element(s)")
    if notes:
        detail += " (" + "; ".join(notes) + ")"
    return _ok(detail, path=str(folder), html_file=str(html_path),
               styles_file=str(styles_path) if styles_path else None,
               network_file=str(har_path),
               requests=count, style_elements=elements,
               image=str(image_path) if image_path else None,
               **_page_state(page))


_HANDLERS = {
    "browser_open": _a_open,
    "browser_back": _a_back,
    "browser_forward": _a_forward,
    "browser_new_tab": _a_new_tab,
    "browser_switch_tab": _a_switch_tab,
    "browser_close_tab": _a_close_tab,
    "browser_click": _a_click,
    "browser_fill": _a_fill,
    "browser_press": _a_press,
    "browser_select": _a_select,
    "browser_scroll": _a_scroll,
    "browser_read": _a_read,
    "browser_wait": _a_wait,
    "browser_upload": _a_upload,
    "browser_download": _a_download,
    "browser_screenshot": _a_screenshot,
    "browser_capture": _a_capture,
}


def run(action_name: str, args: dict) -> dict:
    """Execute one browser action on the session thread. Never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip())
    if handler is None:
        return _fail(f"'{action_name}' is not a browser action.")
    args = dict(args or {})
    timeout_s = _CALL_TIMEOUT_S
    if str(action_name or "").strip() == "browser_capture":
        # A capture waits through a page load and a bounded quiet-network
        # window, so it gets more than a normal action's deadline.
        timeout_s = max(_CALL_TIMEOUT_S, _ms(args, _CAPTURE_TIMEOUT_S) / 1000.0)
    started = time.monotonic()
    result = _call(handler, args, timeout_s=timeout_s)
    result.setdefault("evidence", {})
    result["evidence"]["elapsed_s"] = round(time.monotonic() - started, 2)
    return result


__all__ = ["browsers_installed", "close", "installed", "run", "status"]
