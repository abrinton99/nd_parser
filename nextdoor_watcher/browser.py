"""Playwright-driven browser work: login, navigation, comment expansion,
live extraction, and element-scoped screenshots (spec §8–§10).

Imported lazily by the scrape/login commands so the rest of the package (and
the unit tests) does not require Playwright to be installed.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

from bs4 import BeautifulSoup

from . import selectors as sel
from .extract import (
    ExtractedComment,
    ExtractedPost,
    extract_from_html,
    extract_post_from_html,
    parse_comment_node,
)

LOGIN_URL = "https://nextdoor.com/login/"
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_EXPAND_ITERATIONS = 50


class SessionExpired(Exception):
    """Raised when the page indicates we are not logged in (spec §10)."""


class TransientError(Exception):
    """Network timeout / 5xx / unexpected redirect — retryable per URL."""


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def do_login(storage_state_path: str | Path, *, log) -> None:
    """Headed login flow. Saves storage_state once a logged-in signal appears
    or the user presses Enter, whichever comes first (spec §10)."""
    from playwright.sync_api import sync_playwright

    storage_state_path = Path(storage_state_path)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    enter_pressed = threading.Event()

    def _wait_enter():
        try:
            input()
        except EOFError:
            pass
        enter_pressed.set()

    threading.Thread(target=_wait_enter, daemon=True).start()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport=DEFAULT_VIEWPORT, user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(LOGIN_URL)
        log.info("login: complete sign-in in the browser window (2FA/captcha ok), "
                 "then press Enter here when done.")

        deadline = time.time() + 600  # 10 minute safety cap
        while time.time() < deadline and not enter_pressed.is_set():
            if _logged_in(page):
                log.info("login: detected logged-in signal.")
                break
            time.sleep(1.0)

        context.storage_state(path=str(storage_state_path))
        _chmod_600(storage_state_path, log=log)
        log.info(f"login: saved session to {storage_state_path}")
        context.close()
        browser.close()


def _chmod_600(path: Path, *, log) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        log.warning(f"{path} contains session secrets; do not share it.")
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as exc:  # pragma: no cover
        log.warning(f"could not chmod 600 {path}: {exc}")


# --------------------------------------------------------------------------- #
# Login-state detection
# --------------------------------------------------------------------------- #
def _query_first(page, key: str):
    for css in sel.candidates(key):
        try:
            loc = page.locator(css).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _logged_in(page) -> bool:
    return _query_first(page, "logged_in_signal") is not None


def ensure_logged_in(page) -> None:
    """Raise SessionExpired if the current page looks logged out (spec §10)."""
    url = (page.url or "").lower()
    if "/login" in url or "next=" in url:
        raise SessionExpired(f"redirected to login: {page.url}")
    if _query_first(page, "logged_out_signal") is not None and not _logged_in(page):
        raise SessionExpired("logged-out signal present on page")
    # If we can find a positive logged-in signal, great; if neither signal is
    # present we proceed (the comment extraction will simply find nothing).


# --------------------------------------------------------------------------- #
# Comment expansion + extraction
# --------------------------------------------------------------------------- #
def wait_for_post_loaded(page, *, timeout_ms: int = 20000) -> None:
    """After navigation, wait for the post's content to actually render.

    Nextdoor is a heavy SPA that effectively never reaches `networkidle`, so we
    navigate with `domcontentloaded` and then wait here for a meaningful signal
    (the comment container, a logged-in signal, or — if we got bounced — a
    logged-out signal) instead of blocking on network silence.
    """
    selectors_to_try = (
        sel.candidates("comment_container")
        + sel.candidates("comment_node")
        + sel.candidates("logged_in_signal")
        + sel.candidates("logged_out_signal")
    )
    combined = ", ".join(s for s in selectors_to_try if ":has-text(" not in s)
    if combined:
        try:
            page.wait_for_selector(combined, timeout=timeout_ms, state="attached")
            return
        except Exception:
            pass
    # Fall back to a best-effort load-state wait so we don't proceed on a blank page.
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        pass


def expand_all_comments(page) -> None:
    """Click every 'show more' / 'view replies' button until none remain,
    scrolling to coax virtualized content. Capped to avoid infinite loops."""
    for _ in range(MAX_EXPAND_ITERATIONS):
        clicked = False
        for css in sel.candidates("expand_buttons"):
            try:
                buttons = page.locator(css)
                count = buttons.count()
            except Exception:
                continue
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        clicked = True
                        page.wait_for_timeout(300)
                except Exception:
                    continue
        try:
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(300)
        except Exception:
            pass
        if not clicked:
            break


def _best_node_locator(page):
    """First comment-node candidate that matches anything (alternatives, not a
    union) — mirrors extract_from_html so both agree on what a comment is."""
    for css in (c for c in sel.candidates("comment_node") if ":has-text(" not in c):
        try:
            loc = page.locator(css)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def extract_live(page) -> tuple[list[ExtractedComment], dict]:
    """Extract comments from the live page and return them alongside a map of
    comment_id -> Playwright element handle (for screenshots).

    The authoritative comment list (with reply nesting) comes from parsing the
    container HTML; handles are matched back by re-deriving each node's id.
    """
    container_html = _container_html(page)
    comments = extract_from_html(container_html)

    handles: dict = {}
    locator = _best_node_locator(page)
    if locator is not None:
        try:
            count = locator.count()
        except Exception:
            count = 0
        for i in range(count):
            node = locator.nth(i)
            try:
                outer = node.evaluate("el => el.outerHTML")
            except Exception:
                continue
            soup = BeautifulSoup(outer, "html.parser")
            tag = soup.find(True)
            if tag is None:
                continue
            parsed = parse_comment_node(tag, is_reply=False, parent_comment_id=None)
            handles.setdefault(parsed.comment_id, node.element_handle())
    return comments, handles


def _expand_post_see_more(post_loc) -> None:
    """Click the post's "… see more" toggle(s), scoped to the given post node,
    so the full body is rendered before we read/screenshot it. Capped to avoid
    loops on broken pages."""
    for _ in range(5):
        clicked = False
        for css in sel.candidates("post_see_more"):
            try:
                buttons = post_loc.locator(css)
                count = buttons.count()
            except Exception:
                continue
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        clicked = True
                        post_loc.page.wait_for_timeout(300)
                except Exception:
                    continue
        if not clicked:
            break


def extract_post_live(page) -> tuple[ExtractedPost | None, object]:
    """Extract the original post and return it with its element handle.

    Expands the post's "see more" toggle first, then parses the post node's own
    (now-full) outerHTML so the parsed fields and the handle used for the
    screenshot always refer to the same, fully-expanded element.
    """
    for css in (c for c in sel.candidates("post_node") if ":has-text(" not in c):
        try:
            loc = page.locator(css).first
            if loc.count() == 0:
                continue
            _expand_post_see_more(loc)
            # Re-acquire the handle after expansion in case the node re-rendered.
            handle = loc.element_handle()
            outer = handle.evaluate("el => el.outerHTML")
        except Exception:
            continue
        post = extract_post_from_html(outer)
        if post is not None:
            return post, handle
    return None, None


def _container_html(page) -> str:
    for css in sel.candidates("comment_container"):
        try:
            loc = page.locator(css).first
            if loc.count() > 0:
                return loc.evaluate("el => el.outerHTML")
        except Exception:
            continue
    # Fall back to the whole document.
    try:
        return page.content()
    except Exception:
        return ""


def take_element_screenshot(handle, path: str | Path) -> bool:
    """Element-scoped PNG including the avatar (spec §9). Returns success."""
    if handle is None:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    # Let lazy avatars/images settle.
    try:
        handle.wait_for_element_state("stable", timeout=2000)
    except Exception:
        pass
    try:
        handle.evaluate(
            "el => Promise.all(Array.from(el.querySelectorAll('img'))"
            ".filter(i => !i.complete).map(i => new Promise(r => {"
            "i.addEventListener('load', r); i.addEventListener('error', r);})))"
        )
    except Exception:
        pass
    try:
        handle.screenshot(path=str(path))
        return True
    except Exception:
        return False
