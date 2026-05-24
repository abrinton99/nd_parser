"""Pure diff engine (spec §8).

Given the comments currently visible on a post and the persisted `seen` index,
classify each as new / edited / unchanged / deleted, and produce the next state
of the index. No I/O, no browser — this is the unit-tested core.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .extract import ExtractedComment, ExtractedPost
from .util import capture_basename


@dataclass
class Action:
    """A capture the orchestration must perform (new/edited/deleted)."""

    cls: str                                  # "new" | "edited" | "deleted"
    comment_id: str
    comment: ExtractedComment | None = None   # present for new/edited
    screenshot_rel: str | None = None         # captures/<base>.png, None for deleted
    is_post: bool = False                     # True for the original-post capture
    post_node_id: str | None = None           # the post's DOM id, when is_post
    # edited extras
    previous_content_hash: str | None = None
    previous_body_text: str | None = None
    previous_observed_at: str | None = None
    # deleted extras
    last_known_content_hash: str | None = None
    last_known_body_text: str | None = None
    last_known_observed_at: str | None = None
    first_missing_at: str | None = None
    confirmed_deleted_at: str | None = None


@dataclass
class DiffResult:
    actions: list[Action] = field(default_factory=list)
    next_seen: dict = field(default_factory=dict)
    new_count: int = 0
    edited_count: int = 0
    deleted_count: int = 0
    unchanged_count: int = 0


def _rel_screenshot(run_id: str, comment_id: str, cls: str) -> str:
    return f"captures/{capture_basename(run_id, comment_id, cls)}.png"


def _new_revision(observed_at, run_id, cls, content_hash, body_text, screenshot):
    return {
        "observed_at": observed_at,
        "run_id": run_id,
        "class": cls,
        "content_hash": content_hash,
        "body_text": body_text,
        "screenshot": screenshot,
    }


def diff(
    extracted: list[ExtractedComment],
    seen: dict,
    *,
    run_id: str,
    observed_at: str,
    is_first_run: bool,
    first_run_mode: str = "seed",
) -> DiffResult:
    """Classify `extracted` against `seen` and build the next index.

    `seen` is not mutated; a deep copy is returned in `DiffResult.next_seen`.
    Deterministic screenshot paths are baked into both the actions and the
    revision records, so the orchestration only needs to write the files.
    """
    next_seen = copy.deepcopy(seen)
    res = DiffResult(next_seen=next_seen)

    present_ids = {c.comment_id for c in extracted}

    # ---- first time we ever scrape this URL --------------------------------
    if is_first_run:
        capture = first_run_mode == "capture-all"
        for c in extracted:
            cls_label = "new" if capture else "seed"
            screenshot = _rel_screenshot(run_id, c.comment_id, "new") if capture else None
            next_seen[c.comment_id] = {
                "status": "live",
                "id_source": c.id_source,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "current_content_hash": c.content_hash,
                "current_body_text": c.body_text,
                "missing_streak": 0,
                "revisions": [
                    _new_revision(
                        observed_at, run_id, cls_label, c.content_hash,
                        c.body_text, screenshot,
                    )
                ],
            }
            if capture:
                res.actions.append(
                    Action(cls="new", comment_id=c.comment_id, comment=c,
                           screenshot_rel=screenshot)
                )
                res.new_count += 1
        return res

    # ---- present comments: new / edited / unchanged ------------------------
    new_actions: list[Action] = []
    edited_actions: list[Action] = []

    for c in extracted:
        entry = next_seen.get(c.comment_id)
        resurrected = entry is not None and entry.get("status") == "deleted"

        if entry is None or resurrected:
            screenshot = _rel_screenshot(run_id, c.comment_id, "new")
            rev = _new_revision(
                observed_at, run_id, "new", c.content_hash, c.body_text, screenshot
            )
            if entry is None:
                next_seen[c.comment_id] = {
                    "status": "live",
                    "id_source": c.id_source,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "current_content_hash": c.content_hash,
                    "current_body_text": c.body_text,
                    "missing_streak": 0,
                    "revisions": [rev],
                }
            else:  # resurrected: keep history, re-record as fresh "new"
                entry["status"] = "live"
                entry["last_seen_at"] = observed_at
                entry["current_content_hash"] = c.content_hash
                entry["current_body_text"] = c.body_text
                entry["missing_streak"] = 0
                entry.pop("first_missing_at", None)
                entry.pop("confirmed_deleted_at", None)
                entry["revisions"].append(rev)
            new_actions.append(
                Action(cls="new", comment_id=c.comment_id, comment=c,
                       screenshot_rel=screenshot)
            )
            continue

        # Present and previously known: reset any missing bookkeeping.
        entry["status"] = "live"
        entry["last_seen_at"] = observed_at
        entry["missing_streak"] = 0
        entry.pop("first_missing_at", None)

        if entry.get("current_content_hash") != c.content_hash:
            screenshot = _rel_screenshot(run_id, c.comment_id, "edited")
            prev_hash = entry.get("current_content_hash")
            prev_body = entry.get("current_body_text")
            prev_observed = entry["revisions"][-1]["observed_at"] if entry.get("revisions") else None
            entry["current_content_hash"] = c.content_hash
            entry["current_body_text"] = c.body_text
            entry["revisions"].append(
                _new_revision(
                    observed_at, run_id, "edited", c.content_hash, c.body_text, screenshot
                )
            )
            edited_actions.append(
                Action(
                    cls="edited", comment_id=c.comment_id, comment=c,
                    screenshot_rel=screenshot,
                    previous_content_hash=prev_hash,
                    previous_body_text=prev_body,
                    previous_observed_at=prev_observed,
                )
            )
        else:
            res.unchanged_count += 1

    # ---- absent comments: missing-streak -> deleted ------------------------
    deleted_actions: list[Action] = []
    for cid, entry in next_seen.items():
        if cid in present_ids:
            continue
        if entry.get("status") == "deleted":
            continue
        streak = int(entry.get("missing_streak", 0)) + 1
        if streak >= 2:
            first_missing = entry.get("first_missing_at") or observed_at
            entry["status"] = "deleted"
            entry["missing_streak"] = streak
            entry["first_missing_at"] = first_missing
            entry["confirmed_deleted_at"] = observed_at
            entry["revisions"].append(
                _new_revision(observed_at, run_id, "deleted", None, None, None)
            )
            deleted_actions.append(
                Action(
                    cls="deleted", comment_id=cid,
                    last_known_content_hash=entry.get("current_content_hash"),
                    last_known_body_text=entry.get("current_body_text"),
                    last_known_observed_at=entry.get("last_seen_at"),
                    first_missing_at=first_missing,
                    confirmed_deleted_at=observed_at,
                )
            )
        else:
            # First miss: stays live, record the streak for next time.
            entry["missing_streak"] = streak
            entry.setdefault("first_missing_at", observed_at)

    # Capture order: new (chronological, oldest first), then edited, then deleted.
    res.actions = new_actions + edited_actions + deleted_actions
    res.new_count = len(new_actions)
    res.edited_count = len(edited_actions)
    res.deleted_count = len(deleted_actions)
    return res


def _post_as_comment(post: ExtractedPost) -> ExtractedComment:
    """Adapt an ExtractedPost to the comment shape the capture pipeline expects."""
    return ExtractedComment(
        comment_id="post",
        id_source="post",
        content_hash=post.content_hash,
        body_text=post.body_text,
        author_display_name=post.author_display_name,
        timestamp_text=post.timestamp_text,
        edited_marker_text=post.edited_marker_text,
        is_reply=False,
        parent_comment_id=None,
    )


def diff_post(
    post: ExtractedPost | None,
    post_state: dict | None,
    *,
    run_id: str,
    observed_at: str,
) -> tuple[Action | None, dict | None, str]:
    """Classify the original post: capture once on first scrape, then on edits.

    Returns (action_or_None, next_post_state, class). `class` is one of
    "new", "edited", "unchanged", or "missing" (post not found on the page).
    Unlike comments, the post is captured on the first scrape regardless of
    --first-run-mode (it is a single context shot, not the comment backlog).
    """
    if post is None:
        return None, post_state, "missing"

    if post_state is not None and post_state.get("current_content_hash") == post.content_hash:
        ns = dict(post_state)
        ns["last_seen_at"] = observed_at
        return None, ns, "unchanged"

    cls = "new" if post_state is None else "edited"
    screenshot = f"captures/{capture_basename(run_id, 'post', cls)}.png"

    prev_hash = prev_body = prev_observed = None
    if post_state is None:
        ns = {
            "id_source": "post",
            "post_node_id": post.post_node_id,
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
            "current_content_hash": post.content_hash,
            "current_body_text": post.body_text,
            "revisions": [],
        }
    else:
        ns = copy.deepcopy(post_state)
        prev_hash = ns.get("current_content_hash")
        prev_body = ns.get("current_body_text")
        prev_observed = ns["revisions"][-1]["observed_at"] if ns.get("revisions") else None
        ns["last_seen_at"] = observed_at
        ns["current_content_hash"] = post.content_hash
        ns["current_body_text"] = post.body_text
        if post.post_node_id:
            ns["post_node_id"] = post.post_node_id

    ns.setdefault("revisions", []).append(
        _new_revision(observed_at, run_id, cls, post.content_hash, post.body_text, screenshot)
    )

    action = Action(
        cls=cls, comment_id="post", comment=_post_as_comment(post),
        screenshot_rel=screenshot, is_post=True, post_node_id=post.post_node_id,
    )
    if cls == "edited":
        action.previous_content_hash = prev_hash
        action.previous_body_text = prev_body
        action.previous_observed_at = prev_observed
    return action, ns, cls
