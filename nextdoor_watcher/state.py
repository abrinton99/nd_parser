"""Persistent state: per-post seen.json + post-meta.json, global watcher-state,
and per-invocation run summaries with 90-day pruning (spec §12)."""

from __future__ import annotations

from pathlib import Path

from .util import (
    atomic_write_json,
    fs_stamp,
    iso,
    parse_iso,
    post_slug,
    read_json,
    utc_now,
)

RUN_RETENTION_DAYS = 90


class Paths:
    """Resolves the on-disk layout under a single output directory (spec §9)."""

    def __init__(self, output_dir: str | Path):
        self.root = Path(output_dir)

    # global
    @property
    def watcher_state(self) -> Path:
        return self.root / "watcher-state.json"

    @property
    def lock(self) -> Path:
        return self.root / "watcher.lock"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "watcher.log"

    @property
    def notifications_state(self) -> Path:
        return self.root / "notifications-state.json"

    # per-post
    def post_dir(self, url: str) -> Path:
        return self.root / "posts" / post_slug(url)

    def captures_dir(self, url: str) -> Path:
        return self.post_dir(url) / "captures"

    def seen(self, url: str) -> Path:
        return self.post_dir(url) / "seen.json"

    def post_meta(self, url: str) -> Path:
        return self.post_dir(url) / "post-meta.json"

    def run_summary(self, run_id: str) -> Path:
        return self.runs_dir / f"{fs_stamp(run_id)}.json"


class PostStore:
    """Loads/saves a single post's seen.json + post-meta.json."""

    def __init__(self, paths: Paths, url: str):
        self.paths = paths
        self.url = url
        self.slug = post_slug(url)

    @property
    def is_first_run(self) -> bool:
        return not self.paths.seen(self.url).exists()

    def load_seen(self) -> dict:
        return read_json(self.paths.seen(self.url), {}) or {}

    def save_seen(self, seen: dict) -> None:
        atomic_write_json(self.paths.seen(self.url), seen)

    def load_meta(self) -> dict:
        return read_json(self.paths.post_meta(self.url), {}) or {}

    def load_post_state(self) -> dict | None:
        """The original-post state recorded under post-meta.json's `post` key."""
        return self.load_meta().get("post")

    def update_meta(
        self,
        *,
        result: str,
        new_count: int,
        edited_count: int,
        deleted_count: int,
        seen: dict,
        run_id: str,
        post_state: dict | None = None,
    ) -> None:
        meta = self.load_meta()
        now = iso()
        meta.setdefault("post_url", self.url)
        meta.setdefault("post_slug", self.slug)
        meta.setdefault("first_seen_at", now)
        meta["last_scrape_at"] = now
        meta["last_scrape_result"] = result
        meta["last_scrape_new_count"] = new_count
        meta["last_scrape_edited_count"] = edited_count
        meta["last_scrape_deleted_count"] = deleted_count
        if post_state is not None:
            meta["post"] = post_state
        meta["total_seen"] = len(seen)
        meta["total_currently_live"] = sum(
            1 for e in seen.values() if e.get("status") == "live"
        )
        meta["total_edited_ever"] = meta.get("total_edited_ever", 0) + edited_count
        meta["total_deleted_ever"] = sum(
            1 for e in seen.values() if e.get("status") == "deleted"
        )
        atomic_write_json(self.paths.post_meta(self.url), meta)


def write_run_summary(paths: Paths, summary: dict) -> Path:
    out = paths.run_summary(summary["run_id"])
    atomic_write_json(out, summary)
    return out


def prune_old_runs(paths: Paths, *, retention_days: int = RUN_RETENTION_DAYS) -> int:
    """Delete run summaries older than the retention window. Returns count pruned."""
    runs = paths.runs_dir
    if not runs.exists():
        return 0
    cutoff = utc_now().timestamp() - retention_days * 86400
    pruned = 0
    for f in runs.glob("*.json"):
        try:
            # Prefer the timestamp encoded in the filename; fall back to mtime.
            ts = _stamp_from_filename(f.stem) or f.stat().st_mtime
            if ts < cutoff:
                f.unlink()
                pruned += 1
        except OSError:
            continue
    return pruned


def _stamp_from_filename(stem: str) -> float | None:
    # Filenames look like 2026-05-21T14-30-05Z; convert back to a real time.
    try:
        iso_form = stem  # 2026-05-21T14-30-05Z
        # Split date and time on 'T', restore colons only in the time portion.
        date_part, time_part = iso_form.split("T")
        time_part = time_part.rstrip("Z").replace("-", ":")
        return parse_iso(f"{date_part}T{time_part}Z").timestamp()
    except (ValueError, IndexError):
        return None


def update_watcher_state(paths: Paths, *, input_file: str, summary: dict) -> None:
    state = read_json(paths.watcher_state, {}) or {}
    totals = summary.get("totals", {})
    state["last_input_file"] = input_file
    state["last_run_id"] = summary["run_id"]
    state["last_run_duration_seconds"] = summary.get("duration_seconds")
    state["last_run_url_count"] = len(summary.get("urls", []))
    state["last_run_new_count"] = totals.get("new", 0)
    state["last_run_edited_count"] = totals.get("edited", 0)
    state["last_run_deleted_count"] = totals.get("deleted", 0)
    state["last_run_exit_code"] = summary.get("exit_code")
    state["total_runs"] = state.get("total_runs", 0) + 1
    atomic_write_json(paths.watcher_state, state)
