"""Scrape orchestration — the core one-shot command (spec §7, §11).

One invocation: acquire lock, load session + input, scrape each URL once
(with per-URL retry/backoff), write captures + state, write a run summary,
fire notifications, release lock, exit.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .config import Config
from .diff import diff
from .logging_setup import get_logger
from .state import Paths, PostStore
from .util import iso, post_slug

# Per-URL retry/backoff (spec §11). Overridable for tests.
RETRY_BACKOFF_SECONDS = [30, 60, 120]
PAUSE_BETWEEN_URLS = (5, 15)


@dataclass
class UrlResult:
    url: str
    ok: bool = False
    new: int = 0
    edited: int = 0
    deleted: int = 0
    took_seconds: float = 0.0
    error: str | None = None
    fatal: str | None = None  # "session_expired"
    layout_warning: bool = False
    post_class: str = "unchanged"  # new | edited | unchanged | missing


@dataclass
class ScrapeOptions:
    dry_run: bool = False
    backoff_seconds: list = field(default_factory=lambda: list(RETRY_BACKOFF_SECONDS))
    pause_between_urls: tuple = PAUSE_BETWEEN_URLS
    headless: bool = True


def _polite_pause(opts: ScrapeOptions) -> None:
    lo, hi = opts.pause_between_urls
    if hi > 0:
        time.sleep(random.uniform(lo, hi))


def scrape_one(page, url: str, cfg: Config, paths: Paths, run_id: str,
               opts: ScrapeOptions) -> UrlResult:
    """Scrape a single URL: navigate, expand, extract, diff, capture, persist.

    Retries transient failures with exponential backoff; SessionExpired is fatal
    for the whole invocation (caller stops the loop)."""
    from .browser import (
        SessionExpired,
        ensure_logged_in,
        expand_all_comments,
        extract_live,
        extract_post_live,
        take_element_screenshot,
        wait_for_post_loaded,
    )
    from .capture import write_sidecar
    from .diff import diff_post

    log = get_logger()
    started = time.monotonic()
    slug = post_slug(url)
    attempts = len(opts.backoff_seconds) + 1
    last_err: str | None = None

    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            wait_for_post_loaded(page)
            ensure_logged_in(page)
            expand_all_comments(page)
            comments, handles = extract_live(page)
            post, post_handle = extract_post_live(page)

            if not comments:
                log.warning(f"scrape warn url={slug} zero comments (page layout may have changed)")
            if post is None:
                log.warning(f"scrape warn url={slug} post body not found (page layout may have changed)")

            store = PostStore(paths, url)
            is_first = store.is_first_run
            seen = store.load_seen()
            observed_at = iso()

            result = diff(
                comments, seen,
                run_id=run_id, observed_at=observed_at,
                is_first_run=is_first, first_run_mode=cfg.first_run_mode,
            )
            post_action, next_post_state, post_class = diff_post(
                post, store.load_post_state(), run_id=run_id, observed_at=observed_at,
            )

            if not opts.dry_run:
                captures_dir = paths.captures_dir(url)
                # Capture the original post first (context), then the comments.
                if post_action is not None:
                    if post_action.screenshot_rel:
                        png_path = paths.post_dir(url) / post_action.screenshot_rel
                        if not take_element_screenshot(post_handle, png_path):
                            log.warning(f"screenshot failed url={slug} post class={post_action.cls}")
                    write_sidecar(
                        post_action, captures_dir=captures_dir, post_url=url,
                        post_slug=slug, run_id=run_id, observed_at=observed_at,
                    )
                for action in result.actions:
                    if action.cls in ("new", "edited") and action.screenshot_rel:
                        png_path = paths.post_dir(url) / action.screenshot_rel
                        ok = take_element_screenshot(handles.get(action.comment_id), png_path)
                        if not ok:
                            log.warning(
                                f"screenshot failed url={slug} comment={action.comment_id} "
                                f"class={action.cls}"
                            )
                    write_sidecar(
                        action, captures_dir=captures_dir, post_url=url,
                        post_slug=slug, run_id=run_id, observed_at=observed_at,
                    )
                store.save_seen(result.next_seen)
                store.update_meta(
                    result="ok",
                    new_count=result.new_count,
                    edited_count=result.edited_count,
                    deleted_count=result.deleted_count,
                    seen=result.next_seen,
                    run_id=run_id,
                    post_state=next_post_state,
                )

            took = time.monotonic() - started
            total = len(result.next_seen)
            log.info(
                f"scrape ok url={slug} post={post_class} new={result.new_count} "
                f"edited={result.edited_count} deleted={result.deleted_count} "
                f"total={total} took={took:.1f}s"
            )
            return UrlResult(
                url=url, ok=True, new=result.new_count, edited=result.edited_count,
                deleted=result.deleted_count, took_seconds=round(took, 1),
                layout_warning=not comments, post_class=post_class,
            )

        except SessionExpired as exc:
            log.error(f"ERROR session expired url={url} — please re-run `nextdoor-watcher login` ({exc})")
            return UrlResult(url=url, ok=False, fatal="session_expired", error=str(exc),
                             took_seconds=round(time.monotonic() - started, 1))
        except Exception as exc:  # transient: timeout, 5xx, nav errors
            last_err = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            if attempt < attempts - 1:
                delay = opts.backoff_seconds[attempt]
                log.warning(f"scrape retry url={slug} attempt={attempt + 1} err={last_err} backoff={delay}s")
                time.sleep(delay)
            else:
                log.warning(f"scrape fail url={slug} err={last_err} after {attempts} attempts")

    return UrlResult(url=url, ok=False, error=f"{last_err} after {attempts} attempts",
                     took_seconds=round(time.monotonic() - started, 1))


def _summarize(run_id: str, started_iso: str, results: list[UrlResult],
               duration: float, exit_code: int) -> dict:
    urls = []
    for r in results:
        item = {"url": r.url, "ok": r.ok}
        if r.ok:
            item.update(new=r.new, edited=r.edited, deleted=r.deleted,
                        post=r.post_class, took_seconds=r.took_seconds)
        else:
            item["error"] = r.error or r.fatal or "unknown"
        urls.append(item)
    return {
        "run_id": run_id,
        "started_at": started_iso,
        "ended_at": iso(),
        "duration_seconds": int(round(duration)),
        "exit_code": exit_code,
        "totals": {
            "new": sum(r.new for r in results),
            "edited": sum(r.edited for r in results),
            "deleted": sum(r.deleted for r in results),
        },
        "urls": urls,
    }


def run_urls(urls: list[str], cfg: Config, paths: Paths, run_id: str,
             opts: ScrapeOptions):
    """Drive the browser over every URL. Returns (results, fatal_session_expired)."""
    from .browser import DEFAULT_VIEWPORT, USER_AGENT
    from playwright.sync_api import sync_playwright

    results: list[UrlResult] = []
    fatal = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=opts.headless)
        context = browser.new_context(
            storage_state=str(cfg.storage_state_path),
            viewport=DEFAULT_VIEWPORT,
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        try:
            for i, url in enumerate(urls):
                result = scrape_one(page, url, cfg, paths, run_id, opts)
                results.append(result)
                if result.fatal == "session_expired":
                    fatal = True
                    break
                if i < len(urls) - 1:
                    _polite_pause(opts)
        finally:
            context.close()
            browser.close()

    return results, fatal
