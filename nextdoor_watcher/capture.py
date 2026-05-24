"""Sidecar JSON construction for captures (spec §9).

The PNG itself is taken in browser.py (it needs the live element handle); this
module owns the metadata sidecar and the filename conventions so they stay in
lock-step with the screenshot path baked into the diff actions.
"""

from __future__ import annotations

from pathlib import Path

from .diff import Action
from .util import atomic_write_json


def sidecar_path_for(screenshot_rel: str | None, captures_dir: Path, action: Action,
                     run_id: str) -> Path:
    """The JSON sidecar path that pairs with this action's capture.

    For new/edited it mirrors the PNG name; for deleted (no PNG) it is derived
    from the deterministic basename.
    """
    if screenshot_rel:
        return captures_dir / (Path(screenshot_rel).stem + ".json")
    from .util import capture_basename
    return captures_dir / (capture_basename(run_id, action.comment_id, action.cls) + ".json")


def build_sidecar(action: Action, *, post_url: str, post_slug: str, run_id: str,
                  observed_at: str) -> dict:
    """Construct the sidecar dict for any capture class (spec §9)."""
    c = action.comment
    common = {
        "comment_id": action.comment_id,
        "id_source": c.id_source if c else None,
        "post_url": post_url,
        "post_slug": post_slug,
        "class": action.cls,
        "run_id": run_id,
        "observed_at": observed_at,
        "author_display_name": c.author_display_name if c else None,
        "timestamp_text": c.timestamp_text if c else None,
        "edited_marker_text": c.edited_marker_text if c else None,
        "is_reply": c.is_reply if c else None,
        "parent_comment_id": c.parent_comment_id if c else None,
        "body_text": c.body_text if c else None,
        "content_hash": c.content_hash if c else None,
        "screenshot_path": action.screenshot_rel,
    }

    if action.cls == "edited":
        common.update(
            previous_content_hash=action.previous_content_hash,
            previous_body_text=action.previous_body_text,
            previous_observed_at=action.previous_observed_at,
        )
    elif action.cls == "deleted":
        common.update(
            screenshot_path=None,
            body_text=None,
            content_hash=None,
            last_known_content_hash=action.last_known_content_hash,
            last_known_body_text=action.last_known_body_text,
            last_known_observed_at=action.last_known_observed_at,
            first_missing_at=action.first_missing_at,
            confirmed_deleted_at=action.confirmed_deleted_at,
        )
    return common


def write_sidecar(action: Action, *, captures_dir: Path, post_url: str, post_slug: str,
                  run_id: str, observed_at: str) -> Path:
    data = build_sidecar(
        action, post_url=post_url, post_slug=post_slug, run_id=run_id,
        observed_at=observed_at,
    )
    out = sidecar_path_for(action.screenshot_rel, captures_dir, action, run_id)
    atomic_write_json(out, data)
    return out
