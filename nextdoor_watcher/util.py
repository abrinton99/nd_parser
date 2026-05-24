"""Small pure helpers: timestamps, slugs, hashing, atomic file writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with a trailing Z and second precision, colons intact.

    This is the canonical form stored inside JSON (e.g. "2026-05-21T14:30:05Z").
    """
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fs_stamp(iso_or_dt: str | datetime) -> str:
    """Filesystem-safe form of a timestamp: colons replaced by dashes.

    "2026-05-21T14:30:05Z" -> "2026-05-21T14-30-05Z"
    """
    s = iso_or_dt if isinstance(iso_or_dt, str) else iso(iso_or_dt)
    return s.replace(":", "-")


def parse_iso(s: str) -> datetime:
    """Parse a Z-suffixed ISO timestamp back into an aware datetime."""
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Hashing & text normalization
# --------------------------------------------------------------------------- #
def normalize_body(text: str) -> str:
    """Normalization used for content fingerprints: collapse whitespace,
    trim, lowercase. Picked once and applied consistently (spec §8)."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def content_hash(body_text: str) -> str:
    return hashlib.sha256(normalize_body(body_text).encode("utf-8")).hexdigest()


def fallback_comment_id(author: str | None, body_text: str) -> str:
    """Last-resort comment id: SHA-256 of author + first 200 chars of body.

    Timestamp is deliberately excluded so relative times ("2m ago" -> "1h ago")
    do not change the id and defeat edit detection (spec §8)."""
    basis = f"{author or ''}||{(body_text or '')[:200]}"
    return "h_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# URL normalization & slugs
# --------------------------------------------------------------------------- #
NEXTDOOR_HOST_RE = re.compile(r"^(www\.)?nextdoor\.com$", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Strip query string, fragment and trailing slash for de-duplication."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def is_nextdoor_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(NEXTDOOR_HOST_RE.match(parts.netloc))


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_filename_part(s: str) -> str:
    """Sanitize an arbitrary id for use inside a filename."""
    return _FILENAME_SAFE_RE.sub("_", s).strip("_") or "unknown"


def capture_basename(run_id: str, comment_id: str, cls: str) -> str:
    """`<run_id>__<comment_id>__<class>` with a filesystem-safe run stamp/id."""
    return f"{fs_stamp(run_id)}__{safe_filename_part(comment_id)}__{cls}"


_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")


def post_slug(url: str) -> str:
    """Deterministic per-post folder name.

    https://nextdoor.com/p/abc123 -> p_abc123
    Falls back to h_<sha256[:16]> of the normalized URL when no slug derivable.
    """
    norm = normalize_url(url)
    path = urlsplit(norm).path.strip("/")
    # Take the last two meaningful path segments, e.g. "p/abc123" -> "p_abc123".
    segments = [s for s in path.split("/") if s]
    if segments:
        tail = segments[-2:] if len(segments) >= 2 else segments
        candidate = _SLUG_SAFE_RE.sub("_", "_".join(tail)).strip("_")
        if candidate:
            return candidate
    return "h_" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Atomic JSON writes (spec §12)
# --------------------------------------------------------------------------- #
def atomic_write_json(path: str | os.PathLike, data: Any) -> None:
    """Write JSON atomically: temp file in the same dir, fsync, os.replace.

    A crash mid-write must never leave a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup of the temp file; re-raise the original error.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default
