"""Test helpers: build comment-thread HTML matching the shipped selectors.

Mirrors the real Nextdoor structure closely enough to exercise extraction:
each comment is a `comment-detail` node carrying a stable id (via
`data-comment-id`, one of the configured id attributes), an author profile
anchor (the first one is an empty-text avatar, like the live page), a
`comment-detail-body`, and optional nested replies.
"""

from __future__ import annotations

from html import escape


def _comment_html(c: dict) -> str:
    cid = escape(c["id"])
    author = escape(c.get("author", "Anon"))
    body = escape(c.get("body", ""))
    ts = escape(c.get("timestamp", "5m ago"))
    edited = c.get("edited")
    edited_html = (
        f'<span data-testid="edited-marker">{escape(str(edited))}</span>'
        if edited else ""
    )
    replies = "".join(_comment_html(r) for r in c.get("replies", []))
    return (
        f'<div data-testid="comment-detail" data-comment-id="{cid}">'
        f'<a href="/profile/avatar"><img alt="avatar"></a>'  # empty-text avatar anchor
        f'<a href="/profile/x?is=feed_commenter">{author}</a>'
        f"<span>{ts}</span>"
        f'<div data-testid="comment-detail-body">{body}</div>'
        f"{edited_html}"
        f"{replies}"
        f"</div>"
    )


def _post_html(post: dict) -> str:
    author = escape(post.get("author", "OP"))
    body = escape(post.get("body", ""))
    pid = escape(post.get("id", "feedItem_1"))
    edited = post.get("edited")
    edited_html = (
        f'<span data-testid="edited-marker">{escape(str(edited))}</span>' if edited else ""
    )
    return (
        f'<div data-testid="feed-item-card" id="{pid}">'
        f'<a href="/profile/op?is=feed_author">{author}</a>'
        f'<span data-testid="post-timestamp">2d</span>'
        f'<div data-testid="post-body">{body}</div>'
        f"{edited_html}"
        f"</div>"
    )


def build_thread_html(comments: list[dict], post: dict | None = None) -> str:
    """`comments` is a list of dicts: {id, author, body, timestamp?, edited?, replies?}.

    `post`, if given, renders the original post as the first feed-item-card.
    """
    post_html = _post_html(post) if post else ""
    nodes = "".join(_comment_html(c) for c in comments)
    return (
        "<html><body><main>"
        '<div data-testid="comment-reply-prompt"></div>'
        f"{post_html}"
        f'<div data-testid="comment-list">{nodes}</div>'
        "</main></body></html>"
    )
