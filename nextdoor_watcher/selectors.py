"""CSS selectors for Nextdoor's DOM — treated as configuration, not constants.

Nextdoor's markup changes across releases. Keep selector strings HERE and out
of the extraction logic so they can be updated without touching code. See
SELECTORS.md for how to find new values with Chrome DevTools.

A selector value is a list of candidates. For the *node* selectors
(`comment_container`, `comment_node`) the FIRST candidate that matches anything
wins — candidates are alternatives, not a union, so a wrapper and its inner
element are never both matched. For *field* selectors the first candidate that
yields non-empty text wins.

The leading values were verified against a live, logged-in post in May 2026
(comments render as `<div id="comment_NNN">` wrappers). Older guesses are kept
as fallbacks.
"""

from __future__ import annotations

SELECTORS: dict[str, object] = {
    # Container that holds all comments. `main` is a safe broad fallback.
    "comment_container": [
        '[data-testid="comment-list"]',
        "main",
        "body",
    ],
    # A single comment / reply. Each comment is wrapped in <div id="comment_NNN">,
    # which conveniently also carries the stable id.
    "comment_node": [
        'div[id^="comment_"]',
        '[data-testid="comment-detail"]',
        "article[data-comment-id]",
    ],
    # The post's own "Comment" control. On a /p/ feed view the post's comments
    # are collapsed behind this button (its text is the comment count) and are
    # absent from the DOM until it's clicked; clicking renders them inline inside
    # the post node. Scoped to the main post node, clicked once, and only when no
    # comments are already showing (a second click would collapse them again).
    "comment_expand_toggle": [
        '[data-testid="post-reply-button"]',
        '[aria-label="Comment"]',
    ],
    # Buttons that reveal more comments / replies. Clicked until none remain.
    # `seeMoreButton` is Nextdoor's stable testid for the "See previous comments"
    # / "See previous replies" pagination control — older comments live behind it
    # and won't render until it's clicked (repeatedly, oldest-batch last).
    "expand_buttons": [
        '[data-testid="seeMoreButton"]',
        'button:has-text("See previous comments")',
        'button:has-text("Show more comments")',
        'button:has-text("View more replies")',
        'button:has-text("See previous replies")',
        'button:has-text("more repl")',
        'button:has-text("more comment")',
    ],
    # Within a comment node:
    "comment_body": [
        '[data-testid="comment-detail-body"]',
        '[data-testid="comment-body"]',
        ".comment-body",
    ],
    "comment_author": [
        'a[href*="/profile/"]',
        '[data-testid="author-test"]',
        ".author-name",
    ],
    "comment_timestamp": [
        '[data-testid="post-timestamp"]',
        "time",
    ],
    "comment_edited_marker": [
        '[data-testid="edited-marker"]',
    ],
    # ---- The original post (spec §8a / §9a) --------------------------------
    # The main post is the first feed-item-card that contains a post body; the
    # rest of the feed-item-cards on the page are the "more posts" sidebar.
    # `:has()` is valid CSS (soupsieve + Playwright both support it) — unlike
    # `:has-text()`, which is Playwright-only.
    "post_node": [
        '[data-testid="feed-item-card"]:has([data-testid="post-body"])',
        '[id^="feedItem_"]:has([data-testid="post-body"])',
        '[data-testid="post-body"]',
    ],
    "post_body": [
        '[data-testid="post-body"]',
    ],
    # The "… see more" truncation toggle inside a post body. Clicked (scoped to
    # the main post node) before capture so the full body is screenshotted.
    "post_see_more": [
        "[data-post-see-more]",
        'span:has-text("see more")',
    ],
    "post_author": [
        'a[href*="/profile/"]',
        '[data-testid="author-test"]',
    ],
    "post_timestamp": [
        '[data-testid="post-timestamp"]',
        "time",
    ],
    "post_edited_marker": [
        '[data-testid="edited-marker"]',
    ],
    "post_id_attributes": [
        "id",
        "data-post-id",
    ],
    # Permalink anchor whose ?comment=<id> fragment is the 2nd-choice id source.
    "comment_permalink": [
        'a[href*="comment="]',
        'a[href*="/c/"]',
    ],
    # DOM attributes inspected (in order) for the 1st-choice stable id. The
    # comment wrapper's `id` (e.g. "comment_1605661730") is the primary source.
    "comment_id_attributes": [
        "id",
        "data-comment-id",
        "data-testid-comment-id",
    ],
    # Logged-in / logged-out signals (spec §10).
    "logged_in_signal": [
        '[data-testid="comment-reply-prompt"]',
        '[data-testid="reply-button"]',
        '[data-testid="post-reply-button"]',
        'textarea[placeholder*="omment"]',
    ],
    "logged_out_signal": [
        'a[href*="/signup"]',
        'button:has-text("Sign up")',
    ],
}


def candidates(key: str) -> list[str]:
    """Return the selector(s) for a key as a list of CSS strings."""
    val = SELECTORS.get(key)
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)  # type: ignore[arg-type]
