"""Comment extraction (spec §8).

This module turns HTML into a list of `ExtractedComment` objects. The same
field-extraction logic feeds two callers:

  * the live Playwright path (browser.py), which hands us each comment node's
    outerHTML plus an element handle for screenshots;
  * the static-HTML path used by the unit tests, which parses a saved fixture.

Keeping the parsing in pure functions over HTML strings is what makes the diff
engine testable without a real browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from . import selectors as sel
from .util import content_hash, fallback_comment_id


@dataclass
class ExtractedComment:
    comment_id: str
    id_source: str            # "dom" | "permalink" | "hash"
    content_hash: str
    body_text: str
    author_display_name: str | None = None
    timestamp_text: str | None = None
    edited_marker_text: str | None = None
    is_reply: bool = False
    parent_comment_id: str | None = None


def _css_only(key: str) -> list[str]:
    """Selector candidates usable by soupsieve (drops Playwright :has-text())."""
    return [c for c in sel.candidates(key) if ":has-text(" not in c]


def _first_text(node: Tag, key: str) -> str | None:
    """First non-empty text among all matches, trying candidates in order.

    Scanning all matches (not just the first) matters for e.g. the author: the
    avatar's profile anchor has no text, while the name's profile anchor does.
    """
    for css in _css_only(key):
        try:
            matches = node.select(css)
        except Exception:
            continue
        for found in matches:
            text = found.get_text(" ", strip=True)
            if text:
                return text
    return None


def _derive_id(node: Tag, body_text: str, author: str | None) -> tuple[str, str]:
    """Return (comment_id, id_source) following the spec's preference order."""
    # 1. A data-* / id attribute on the comment node.
    for attr in sel.candidates("comment_id_attributes"):
        val = node.get(attr)
        if val:
            return str(val), "dom"

    # 2. The ?comment=<id> (or /comment/<id>) fragment of a permalink anchor.
    for css in _css_only("comment_permalink"):
        try:
            anchor = node.select_one(css)
        except Exception:
            anchor = None
        if anchor is not None and anchor.get("href"):
            href = str(anchor["href"])
            qs = parse_qs(urlsplit(href).query)
            if "comment" in qs and qs["comment"]:
                return f"c_{qs['comment'][0]}", "permalink"
            parts = [p for p in urlsplit(href).path.split("/") if p]
            if "comment" in parts:
                idx = parts.index("comment")
                if idx + 1 < len(parts):
                    return f"c_{parts[idx + 1]}", "permalink"

    # 3. Last-resort hash of author + body (timestamp excluded on purpose).
    return fallback_comment_id(author, body_text), "hash"


def parse_comment_node(node: Tag, *, is_reply: bool, parent_comment_id: str | None) -> ExtractedComment:
    """Extract one comment's fields from its DOM node. Best-effort: a missing
    optional field never raises."""
    body_text = _first_text(node, "comment_body") or ""
    author = _first_text(node, "comment_author")
    timestamp = _first_text(node, "comment_timestamp")
    edited = _first_text(node, "comment_edited_marker")

    comment_id, id_source = _derive_id(node, body_text, author)
    return ExtractedComment(
        comment_id=comment_id,
        id_source=id_source,
        content_hash=content_hash(body_text),
        body_text=body_text,
        author_display_name=author,
        timestamp_text=timestamp,
        edited_marker_text=edited,
        is_reply=is_reply,
        parent_comment_id=parent_comment_id,
    )


def extract_from_html(html: str) -> list[ExtractedComment]:
    """Parse a full page (or comment container) HTML string into comments.

    Reply nesting: a comment node that has an ancestor comment node is a reply,
    and that ancestor supplies the parent_comment_id.
    """
    soup = BeautifulSoup(html, "html.parser")

    container = None
    for css in _css_only("comment_container"):
        container = soup.select_one(css)
        if container is not None:
            break
    scope = container or soup

    # First candidate that matches anything wins (alternatives, not a union) so
    # a wrapper and its inner element are never both treated as comments.
    ordered_nodes: list[Tag] = []
    for css in _css_only("comment_node"):
        found = scope.select(css)
        if found:
            ordered_nodes = found
            break

    node_set = {id(n) for n in ordered_nodes}
    comments: list[ExtractedComment] = []
    parsed_by_node: dict[int, ExtractedComment] = {}

    for n in ordered_nodes:
        parent = _enclosing_comment(n, node_set)
        parent_id = None
        is_reply = parent is not None
        if parent is not None and id(parent) in parsed_by_node:
            parent_id = parsed_by_node[id(parent)].comment_id
        c = parse_comment_node(n, is_reply=is_reply, parent_comment_id=parent_id)
        parsed_by_node[id(n)] = c
        comments.append(c)

    return comments


def _enclosing_comment(node: Tag, node_set: set[int]) -> Tag | None:
    """Nearest ancestor that is itself an extracted comment node, if any."""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, Tag) and id(parent) in node_set:
            return parent
        parent = parent.parent
    return None
