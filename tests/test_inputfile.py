"""Input-file loader/validator tests (spec §18.2)."""

from __future__ import annotations

import pytest

from nextdoor_watcher.inputfile import InputFileError, classify_lines, load_urls


def test_comments_blanks_and_whitespace():
    text = "# header\n\n  https://nextdoor.com/p/abc123  \n\n# trailing\n"
    results = {r.lineno: r for r in classify_lines(text)}
    assert results[1].status == "SKIP"
    assert results[2].status == "SKIP"
    assert results[3].status == "OK"
    assert results[3].url == "https://nextdoor.com/p/abc123"


def test_non_nextdoor_url_is_bad():
    results = classify_lines("https://example.com/p/abc\nhttp://nextdoor.com.evil.com/x")
    assert all(r.status == "BAD" for r in results)


def test_www_host_allowed():
    results = classify_lines("https://www.nextdoor.com/p/abc123")
    assert results[0].status == "OK"


def test_duplicates_after_normalization_are_deduped():
    text = (
        "https://nextdoor.com/p/abc123\n"
        "https://nextdoor.com/p/abc123/\n"
        "https://nextdoor.com/p/abc123?utm_source=x#frag\n"
    )
    results = classify_lines(text)
    statuses = [r.status for r in results]
    assert statuses == ["OK", "DUP", "DUP"]


def test_load_urls_warns_and_dedupes(tmp_path):
    f = tmp_path / "wl.txt"
    f.write_text(
        "https://nextdoor.com/p/abc123\n"
        "https://nextdoor.com/p/abc123/\n"
        "https://example.com/bad\n"
    )
    warnings = []
    urls = load_urls(f, warn=warnings.append)
    assert urls == ["https://nextdoor.com/p/abc123"]
    assert len(warnings) == 2  # one DUP, one BAD


def test_missing_file_raises():
    with pytest.raises(InputFileError):
        load_urls("/no/such/file.txt", warn=lambda m: None)


def test_zero_valid_urls_raises(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("# only comments\n\n")
    with pytest.raises(InputFileError):
        load_urls(f, warn=lambda m: None)
