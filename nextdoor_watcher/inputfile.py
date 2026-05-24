"""Input watchlist parsing & validation (spec §6)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util import is_nextdoor_url, normalize_url


@dataclass
class LineResult:
    lineno: int
    raw: str
    status: str          # "OK" | "SKIP" | "BAD" | "DUP"
    url: str | None = None       # normalized URL when OK
    reason: str | None = None    # populated for BAD / DUP


def classify_lines(text: str) -> list[LineResult]:
    """Classify every line of the input file. Pure; used by both load + validate."""
    results: list[LineResult] = []
    seen_norm: dict[str, int] = {}  # normalized URL -> first lineno

    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            results.append(LineResult(i, raw, "SKIP"))
            continue

        if not is_nextdoor_url(stripped):
            results.append(
                LineResult(
                    i, raw, "BAD",
                    reason="not an https nextdoor.com URL",
                )
            )
            continue

        norm = normalize_url(stripped)
        if norm in seen_norm:
            results.append(
                LineResult(
                    i, raw, "DUP", url=norm,
                    reason=f"duplicate of line {seen_norm[norm]} after normalization",
                )
            )
            continue

        seen_norm[norm] = i
        results.append(LineResult(i, raw, "OK", url=norm))

    return results


class InputFileError(Exception):
    """Raised when the input file is missing, unreadable, or has zero valid URLs."""


def load_urls(path: str | Path, *, warn) -> list[str]:
    """Return the de-duplicated list of normalized URLs to scrape.

    `warn(msg)` is called for every BAD/DUP line. Raises InputFileError on a
    missing/unreadable file or when no valid URLs remain (drives exit code 1).
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InputFileError(f"input file not found: {p}") from exc
    except OSError as exc:
        raise InputFileError(f"input file unreadable: {p}: {exc}") from exc

    urls: list[str] = []
    for r in classify_lines(text):
        if r.status == "OK":
            urls.append(r.url)  # type: ignore[arg-type]
        elif r.status == "BAD":
            warn(f"invalid input line {r.lineno}: {r.raw!r} ({r.reason})")
        elif r.status == "DUP":
            warn(f"duplicate input line {r.lineno}: {r.raw!r} ({r.reason})")

    if not urls:
        raise InputFileError(f"no valid URLs in input file: {p}")
    return urls
