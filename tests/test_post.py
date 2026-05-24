"""Original-post extraction + capture tests (spec §8a/§9a)."""

from __future__ import annotations

from nextdoor_watcher.diff import diff_post
from nextdoor_watcher.extract import extract_post_from_html

from .helpers import build_thread_html

RUN1 = "2026-05-21T14:30:05Z"
RUN2 = "2026-05-22T09:15:00Z"
RUN3 = "2026-05-23T10:00:00Z"

POST = {"id": "feedItem_479470223", "author": "Tracey Kessler",
        "body": "Neighbors, here is the original post body."}


def extract_post(post, comments=None):
    html = build_thread_html(comments or [{"id": "comment_a", "body": "a comment"}], post=post)
    return extract_post_from_html(html)


def test_post_extracted_distinct_from_comments():
    p = extract_post(POST)
    assert p is not None
    assert p.author_display_name == "Tracey Kessler"
    assert p.body_text == "Neighbors, here is the original post body."
    assert p.post_node_id == "feedItem_479470223"
    assert p.content_hash


def test_no_post_returns_none():
    html = build_thread_html([{"id": "comment_a", "body": "x"}], post=None)
    assert extract_post_from_html(html) is None


def test_first_scrape_captures_post_as_new():
    p = extract_post(POST)
    action, state, cls = diff_post(p, None, run_id=RUN1, observed_at=RUN1)
    assert cls == "new"
    assert action is not None and action.is_post is True
    assert action.comment_id == "post"
    assert action.screenshot_rel.endswith("__post__new.png")
    assert state["current_content_hash"] == p.content_hash
    assert [r["class"] for r in state["revisions"]] == ["new"]


def test_unchanged_post_not_recaptured():
    p = extract_post(POST)
    _, state, _ = diff_post(p, None, run_id=RUN1, observed_at=RUN1)
    action, state2, cls = diff_post(p, state, run_id=RUN2, observed_at=RUN2)
    assert cls == "unchanged"
    assert action is None
    assert state2["last_seen_at"] == RUN2


def test_edited_post_recaptured_with_previous():
    p1 = extract_post(POST)
    _, state, _ = diff_post(p1, None, run_id=RUN1, observed_at=RUN1)
    edited = {**POST, "body": "Neighbors, I have UPDATED the original post."}
    p2 = extract_post(edited)
    action, state2, cls = diff_post(p2, state, run_id=RUN2, observed_at=RUN2)
    assert cls == "edited"
    assert action.cls == "edited" and action.is_post is True
    assert action.previous_body_text == "Neighbors, here is the original post body."
    assert action.screenshot_rel.endswith("__post__edited.png")
    assert [r["class"] for r in state2["revisions"]] == ["new", "edited"]
    assert state2["current_body_text"] == "Neighbors, I have UPDATED the original post."


def test_missing_post_reported_without_action():
    action, state, cls = diff_post(None, None, run_id=RUN1, observed_at=RUN1)
    assert cls == "missing"
    assert action is None
    assert state is None
