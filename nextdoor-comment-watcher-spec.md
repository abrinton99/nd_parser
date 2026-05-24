# Nextdoor Comment Watcher — Tool Specification

## 1. Overview

A command-line tool that scrapes **new comments and replies** from a user-defined list of Nextdoor posts.

The tool runs as a **one-shot process**: each invocation reads the input file, polls every URL in it once, captures any new comments as screenshots, persists state, and exits. To run it on a schedule, the user installs it as a **cron job** (Linux/macOS), a **launchd plist** (macOS), or a **Scheduled Task** (Windows). The tool itself does not run a background loop.

This makes the tool:
- **Crash-safe** — a single bad invocation never takes down a long-running daemon.
- **Memory-safe** — every run starts from a clean process; no leaks accumulate.
- **Composable** — you can run it manually, on cron, or from CI without changing modes.
- **Restart-safe** — state is fully persisted to disk between runs.

The list of posts to watch is provided via an **input file** (see §6). The tool only ever scrapes URLs in that file — nothing else. New comments are saved as **screenshots** plus sidecar JSON metadata, organized per-post.

The tool uses a **persisted browser session**: the user logs into Nextdoor once via a real browser window, the session cookies are stored, and every subsequent invocation reuses that session headlessly.

---

## 2. Goals & Non-Goals

### Goals
- Run as a **one-shot, cron-friendly** process: read input, scrape, write output, exit.
- Scrape a list of Nextdoor post URLs provided in an input file (re-read on every invocation).
- Detect comments and replies that are new since the last invocation, **per post**.
- Save a screenshot of each new comment, scoped tightly to the comment DOM element, organized in a per-post output folder.
- Maintain a small local index per post so the same comment is never captured twice across runs.
- Use exit codes correctly so cron/launchd/Task Scheduler can detect failures.
- Use a single lock file so concurrent invocations (e.g. a long run still in progress when cron fires again) don't trample each other.
- Reuse a logged-in browser session without requiring re-login on every run.

### Non-Goals
- Running a background loop or daemon. Scheduling is the OS's job, not the tool's.
- Watching feeds, groups, or neighborhood timelines.
- Discovering posts automatically. The tool **only** scrapes URLs explicitly listed in the input file.
- Modifying, replying to, or reacting to posts.
- Scraping post bodies, reactions, or any non-comment content beyond the screenshot frame.
- Bypassing Nextdoor's authentication, rate limits, or terms of service. The user is responsible for ensuring their use complies with Nextdoor's ToS.

---

## 3. High-Level Architecture

```
        ┌────────────────────────────────┐
        │  OS scheduler (cron/launchd/   │
        │  Task Scheduler) fires every   │
        │  X minutes                     │
        └──────────────┬─────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              nextdoor-watcher scrape (one-shot)             │
├─────────────────────────────────────────────────────────────┤
│  1. Acquire lock file (exit cleanly if already locked)      │
│  2. Load storage_state.json   (exit if missing/expired)     │
│  3. Load + validate input file                              │
│  4. For each URL:                                           │
│       • goto, expand comments, extract IDs                  │
│       • diff against per-post seen.json                     │
│       • screenshot + sidecar JSON for each new comment      │
│       • update per-post state                               │
│  5. Write global run-summary                                │
│  6. Release lock, exit                                      │
└─────────────────────────────────────────────────────────────┘
                       │
                       ▼
        ┌────────────────────────────────┐
        │   Output dir (per-post dirs,   │
        │   captures, logs, run history) │
        └────────────────────────────────┘
```

Other subcommands (`login`, `validate`, `status`, `install-cron`) do **not** scrape — they are administrative.

---

## 4. Tech Stack

- **Language:** Python 3.11+ (preferred) or Node.js 20+ — implementer's choice. Spec examples below use Python.
- **Browser automation:** [Playwright](https://playwright.dev/) — required. Chromium engine.
  - Rationale: Nextdoor is a heavy SPA with lazy-loaded comments, infinite-scroll replies, and strong bot detection. Playwright handles JS rendering, persistent storage state, and element-scoped screenshots out of the box.
- **CLI framework:** `click` (Python) or `commander` (Node).
- **Scheduling:** in-process loop with `asyncio.sleep` / `setTimeout`. No external cron required.
- **State:** flat files in a working directory (no database).

---

## 5. CLI Interface

```
nextdoor-watcher login
nextdoor-watcher scrape       --input-file <PATH> [--output-dir DIR] [--first-run-mode MODE] [--dry-run] [--quiet]
nextdoor-watcher validate     --input-file <PATH>
nextdoor-watcher status       [--output-dir DIR]
nextdoor-watcher install-cron --input-file <PATH> --every <MINUTES> [--output-dir DIR]
nextdoor-watcher test-notify  [--output-dir DIR]
```

### `login`
1. Launches Chromium in **headed** mode (visible window).
2. Navigates to `https://nextdoor.com/login/`.
3. Waits for the user to complete login (including 2FA, captcha, etc.).
4. Detects login success by polling for a logged-in DOM signal (e.g. presence of the user avatar in the top nav) OR by waiting until the user presses Enter in the terminal — whichever comes first.
5. Saves Playwright's `storage_state` to `./session/storage_state.json`.
6. Closes the browser.

### `scrape` (the core one-shot command)

Required:
- `--input-file` — path to a file listing the Nextdoor post URLs to scrape. Format: see §6.

Optional:
- `--output-dir` — base directory for screenshots and state. Default: `./nextdoor-watcher-data/`.
- `--first-run-mode` — applies to the **first time each URL is ever seen**. One of:
  - `seed` (default) — on a URL's first scrape, record all existing comment IDs as "seen" but do **not** capture screenshots of them. Only capture comments that appear in later runs.
  - `capture-all` — capture every comment found on a URL's first scrape too.
- `--dry-run` — perform everything except writing PNG and JSON files. Useful for testing selectors and cron wiring.
- `--quiet` — suppress stdout; only write to the log file. Recommended for cron to keep mail volume down (or use cron's own redirection).

Behavior (executed exactly once per invocation — see §7 for full sequence):
1. Acquire the lock file (see §7). If already locked, exit with code `3`.
2. Load `storage_state.json`. Exit with code `2` if missing.
3. Load and validate the input file. Exit with code `1` if missing, unreadable, or zero valid URLs.
4. For each URL: navigate, expand comments, diff against `seen.json`, capture new ones.
5. Write a run summary to `runs/<timestamp>.json`.
6. Release the lock and exit `0`.

The process **never loops or sleeps for the user's interval**. Scheduling is done externally (see §14).

### `validate`
Reads the input file and prints, for each line:
- `OK`   — looks like a valid Nextdoor post URL.
- `SKIP` — comment, blank line.
- `BAD`  — malformed URL or wrong host; prints the reason.

Exits `0` if every non-skip line is OK, `1` otherwise. Useful before installing a cron job.

### `status`
Prints:
- The last invocation's timestamp, duration, and outcome.
- The current input file path (from the most recent run).
- The list of posts ever scraped, with per-URL last-scrape time and total-seen count.
- Session age and whether it looks expired.
- Whether a lock file is currently held (and by which PID).

### `install-cron`
Emits a cron line for the user to paste into their crontab. **Linux only.** It does **not** modify the user's crontab directly — only prints what to add. See §14 for examples and rationale.

---

## 6. Input File Format

The input file is a plain text file. Format rules:

- **One URL per line.**
- **UTF-8** encoding.
- Lines beginning with `#` are treated as comments and ignored.
- Blank lines are ignored.
- Whitespace around URLs is trimmed.
- Duplicate URLs (after normalization — strip query string fragments like `?utm_*`, trailing slashes, fragments) are de-duplicated with a WARN log; the first occurrence wins.
- Each URL **must** match `https://(www\.)?nextdoor\.com/...` — any other host is rejected as `BAD`.

### Example: `watchlist.txt`

```
# Active threads — May 2026
https://nextdoor.com/p/abc123
https://nextdoor.com/p/def456

# Watching this one for the HOA discussion
https://nextdoor.com/p/ghi789
```

### Reload behavior
The input file is read fresh on **every invocation**. Since each invocation is a one-shot, this happens automatically — there is no in-memory cache between runs.

- Adding a URL: it will start being scraped on the next scheduled invocation. Subject to `--first-run-mode`.
- Removing a URL: it stops being scraped on the next invocation. Its per-post folder, `seen.json`, and previously captured screenshots are **left in place** (the tool never deletes captures).
- If the file is missing or unreadable at startup, the invocation exits with code `1`. The scheduler will retry on the next interval automatically.

---

## 7. Scrape Execution (per invocation)

```
def scrape():
    lock = acquire_lock(output_dir/"watcher.lock", pid=os.getpid())
    if lock is None:
        log_warn("another scrape is in progress; exiting"); exit(3)

    try:
        session = load_storage_state()        # exit 2 if missing
        urls = load_input_file()              # exit 1 if missing/empty/unreadable
        run_id = utc_now_iso()
        results = []

        with playwright.chromium.launch_persistent(...) as browser:
            context = browser.new_context(storage_state=session)
            page = context.new_page()

            for url in urls:
                result = scrape_one(page, url)   # see below
                results.append(result)
                if result.fatal == "session_expired":
                    break                         # no point continuing
                polite_pause_between_urls()       # see §11

        write_run_summary(run_id, results)        # runs/<run_id>.json

        if any(r.fatal == "session_expired" for r in results):
            exit(2)
        if all(r.ok for r in results):
            exit(0)
        exit(4)                                   # partial failure
    finally:
        release_lock(lock)


def scrape_one(page, url):
    try:
        page.goto(url, wait_until="networkidle")
        ensure_logged_in(page)                # raises SessionExpired
        expand_all_comments(page)             # see §8
        comments = extract_comments(page)     # see §8
        seen = load_seen(url)                 # per-post
        new = [c for c in comments if c.id not in seen]
        for c in new:
            capture_screenshot(page, c, url)  # see §9
            write_metadata(c, url)
            seen.add(c.id)
        persist_seen(url, seen)
        update_per_post_state(url, new_count=len(new))
        return Result(url=url, ok=True, new_count=len(new))
    except SessionExpired:
        return Result(url=url, ok=False, fatal="session_expired")
    except TransientError as e:
        return Result(url=url, ok=False, error=str(e))
```

### Key invariants
- **One-shot only.** No `while True`, no `sleep(interval)`. The scheduler owns timing.
- **Concurrency lock.** A `watcher.lock` file in `<output-dir>/` contains the running PID and a heartbeat timestamp. If a new invocation finds the lock and the PID is alive, it exits `3` (skipped). If the PID is dead (stale lock from a crash), it logs a WARN and proceeds. Stale-lock threshold: 6 hours.
- **Session expiry is fatal for the invocation.** The remaining URLs are not scraped, the summary records why, and the exit code is `2`. The scheduler will keep retrying on its interval, but every run will exit `2` until the user runs `login` again — so the cron wrapper should alert on repeated `2` exits.
- **Transient errors are non-fatal per URL.** A URL that times out gets a single retry pass (up to 3 attempts with exponential backoff inside the invocation), then is skipped. Other URLs are unaffected.
- **Run summary** at `runs/<run_id>.json` lists each URL's outcome (`ok`, `new_count`, `error`) and the overall duration. Old run summaries are pruned after 90 days.

---

## 8. Comment Extraction

Nextdoor's comment DOM is not stable across releases, so the implementer **must** treat selectors as configuration, not hard-coded constants. Put them in a `selectors.py` (or `selectors.json`) module that can be updated without touching logic.

### Required steps

1. **Expand the thread.** Nextdoor collapses long comment chains and hides replies under "View N replies" / "Show more comments" buttons. The implementer must:
   - Repeatedly click every visible "Show more comments" / "View more replies" / "See previous replies" button until none remain.
   - Scroll the comment container if comments are virtualized.
   - Cap the expansion loop at a sane limit (e.g. 50 iterations) to avoid infinite loops on broken pages.

2. **Identify each comment uniquely.** For each comment node, derive a stable `comment_id` by, in order of preference:
   1. A `data-*` attribute or `id` attribute on the comment DOM node (inspect the live page; Nextdoor typically exposes one).
   2. The fragment of any permalink anchor inside the comment (e.g. `?comment=12345`).
   3. As a last-resort fallback: a SHA-256 hash of `(author_name || first 200 chars of body text)` — **note: timestamp is excluded from the fallback hash** because Nextdoor's relative timestamps ("2m ago" → "1h ago") would otherwise change the ID and defeat edit detection. Document this in the metadata as `id_source: "hash"` so users know it is fragile.

3. **Compute a content fingerprint** for each comment. This is what enables edit detection:
   - `content_hash` = SHA-256 of the comment's normalized body text (whitespace collapsed, trimmed, lowercased — pick one normalization and stick to it).
   - If the comment shows an "Edited" marker on the page, also record `edited_marker_text` (e.g. "Edited 5m ago"). Useful as a sanity-check signal.

4. **Extract metadata** (best-effort, never fail the run if a field is missing):
   - `comment_id` (string, required)
   - `id_source` (one of `dom`, `permalink`, `hash`)
   - `content_hash` (string, required)
   - `body_text` (string — the comment's text content, used for edit diffs)
   - `author_display_name` (string, optional)
   - `timestamp_text` (string as shown on page, e.g. "2h ago", optional)
   - `edited_marker_text` (string, optional)
   - `is_reply` (bool — true if nested under another comment)
   - `parent_comment_id` (string, optional — only if `is_reply`)
   - `observed_at` (ISO-8601 UTC timestamp of this observation)

### Diff against `seen.json`

For each comment currently visible on the page, classify it as one of:

| Class      | Condition                                                                 |
|------------|---------------------------------------------------------------------------|
| `new`      | `comment_id` is not in `seen.json`.                                       |
| `edited`   | `comment_id` is in `seen.json` AND `content_hash` differs from the stored one. |
| `unchanged`| `comment_id` is in `seen.json` AND `content_hash` matches.                 |

Then detect deletions by comparing:

| Class      | Condition                                                                 |
|------------|---------------------------------------------------------------------------|
| `deleted`  | `comment_id` is in `seen.json` with `status: "live"` but is **not** present in the current page extract. |

**Deletion caveat.** A comment may go missing for reasons other than deletion: Nextdoor failed to render it, expansion stopped short, the user scrolled past a virtualized region, etc. To reduce false positives, only mark `deleted` if the comment was missing on **two consecutive invocations**. The first miss flips an in-memory `missing_streak` counter on the seen entry; the second confirms and marks it deleted. Once deleted, the entry is left in place forever (no resurrection logic — if Nextdoor un-deletes it, it'll just be re-recorded as `new`).

### Order of capture

Process captures in this order within a single URL: `new` (chronological, oldest first), then `edited`, then `deleted`. This keeps the output folder readable.

---

## 9. Screenshot Capture & Output

### Capture behavior by class

| Class      | Action                                                                                                                                  |
|------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| `new`      | Take an element-scoped PNG of the comment, including its **avatar**. Write a sidecar JSON. Add a new entry to `seen.json`.              |
| `edited`   | Take a fresh element-scoped PNG of the comment in its current (edited) state. Write a sidecar JSON. Update the entry in `seen.json` with the new `content_hash` and append a revision record. |
| `deleted`  | Do **not** take a screenshot (the comment is gone from the page). Write a sidecar JSON noting the deletion. Update the entry in `seen.json` to `status: "deleted"`.  |
| `unchanged`| Do nothing.                                                                                                                             |

### Screenshot mechanics (for `new` and `edited`)

1. Scroll the comment element into view (`element.scroll_into_view_if_needed()`).
2. Wait briefly for any lazy-loaded avatars/images inside the comment to settle (e.g. 500ms or wait for `img` elements within the comment to have `complete === true`).
3. Take an **element-scoped screenshot** of the comment DOM node — not a full-page screenshot. Playwright: `await comment_element.screenshot(path=...)`. The element bounding box includes the avatar.
4. Save as PNG. **PNG only — no JPEG/WebP.**

### Filename convention

```
<run_id>__<comment_id>__<class>.png
<run_id>__<comment_id>__<class>.json
```

Examples:
- `2026-05-21T14-32-10Z__c_aaa__new.png`
- `2026-05-22T09-15-00Z__c_aaa__edited.png`        ← same comment, later edit
- `2026-05-23T11-00-00Z__c_aaa__deleted.json`      ← deletion, JSON only

This makes the full history of any one comment trivially findable by globbing `*__c_aaa__*`.

### Output layout

Each watched URL gets its own folder, named by a deterministic **post slug** derived from the URL path (e.g. `https://nextdoor.com/p/abc123` → `p_abc123`). If a slug cannot be derived, fall back to a SHA-256 hash of the normalized URL, prefixed with `h_`.

```
<output-dir>/
├── watcher-state.json               # global state (see §12)
├── posts/
│   ├── p_abc123/
│   │   ├── post-meta.json           # url, first_seen_at, last_scrape_at, totals
│   │   ├── seen.json                # comment ID → status + content_hash (see §12)
│   │   └── captures/
│   │       ├── 2026-05-21T14-32-10Z__c_aaa__new.png
│   │       ├── 2026-05-21T14-32-10Z__c_aaa__new.json
│   │       ├── 2026-05-22T09-15-00Z__c_aaa__edited.png
│   │       ├── 2026-05-22T09-15-00Z__c_aaa__edited.json
│   │       └── 2026-05-23T11-00-00Z__c_aaa__deleted.json
│   ├── p_def456/
│   │   └── ...
│   └── p_ghi789/
│       └── ...
├── runs/
│   ├── 2026-05-21T14-30-05Z.json    # per-invocation summary (see §12)
│   └── 2026-05-21T14-45-00Z.json
├── watcher.lock                     # PID + heartbeat (see §7)
└── logs/
    └── watcher.log                  # global watcher log
```

### Sidecar JSON format

Common fields for all capture types:

```json
{
  "comment_id": "c_aaa",
  "id_source": "dom",
  "post_url": "https://nextdoor.com/p/abc123",
  "post_slug": "p_abc123",
  "class": "new",
  "run_id": "2026-05-21T14:30:05Z",
  "observed_at": "2026-05-21T14:32:10Z",
  "author_display_name": "Jane D.",
  "timestamp_text": "5m ago",
  "edited_marker_text": null,
  "is_reply": false,
  "parent_comment_id": null,
  "body_text": "We should petition the city about the new development...",
  "content_hash": "5f3a...",
  "screenshot_path": "captures/2026-05-21T14-32-10Z__c_aaa__new.png"
}
```

Class-specific additions:

- **`class: "edited"`** — adds:
  ```json
  "previous_content_hash": "9c12...",
  "previous_body_text": "We should petition the city...",
  "previous_observed_at": "2026-05-21T14:32:10Z"
  ```
- **`class: "deleted"`** — `screenshot_path` is `null`, and adds:
  ```json
  "screenshot_path": null,
  "last_known_content_hash": "5f3a...",
  "last_known_body_text": "We should petition the city...",
  "last_known_observed_at": "2026-05-22T09:15:00Z",
  "first_missing_at": "2026-05-23T05:00:00Z",
  "confirmed_deleted_at": "2026-05-23T11:00:00Z"
  ```

---

## 10. Session Management

Nextdoor sessions can and will expire. The watcher must handle this gracefully.

### Detecting login state
On every poll, after `page.goto(post_url)`, the implementer must check **at least one** of:
- The current URL — if it has been redirected to `/login` or contains `?next=`, the session is dead.
- Presence of a known logged-in-only element (e.g. the post's comment composer).
- Absence of a known logged-out-only element (e.g. a "Sign up" hero banner).

If logged out:
- Log a clear `ERROR: session expired — please re-run \`nextdoor-watcher login\`` message.
- Exit with code `2`.
- Do **not** attempt to re-authenticate automatically (no credentials are stored).

### Storage location
- `./session/storage_state.json` — Playwright's `storage_state()` output. Contains cookies and localStorage. **Treat as a secret.** The tool should `chmod 600` this file on save (Unix) and warn on Windows that it should not be shared.

---

## 11. Rate Limiting, Politeness & Resilience

- **Minimum scheduling interval:** 2 minutes. The `install-cron` helper rejects anything lower; users who hand-write their crontab can shoot themselves in the foot at their own risk.
- **Per-URL pause:** between consecutive URLs within a single invocation, sleep for a randomized 5–15 seconds. This avoids hammering Nextdoor with a burst of `goto`s and keeps the traffic shape more human.
- **Schedule jitter:** the `install-cron` helper emits a cron line that uses a small random offset within the chosen interval (or, equivalently, advises adding a `sleep $((RANDOM \% 60))` prefix). Avoids making your traffic look exactly metronomic.
- **Realistic browser fingerprint:** use Playwright's default Chromium; do **not** set obvious automation flags. Set a normal desktop viewport (e.g. 1440×900) and a real `User-Agent`.
- **Backoff on transient failures (within an invocation):** on network errors, timeouts, or HTTP 5xx for a given URL, retry with exponential backoff (e.g. 30s, 60s, 120s, max 3 attempts) before giving up on that URL for this invocation. A failed URL does not abort the rest of the run.
- **Hard stop on auth failure:** as in §10, the invocation exits `2`. No retry storms against the login page.
- **Resource cleanup:** each invocation is a fresh process, so resource leaks are bounded by definition. The Playwright context lives only for the duration of the run.

---

## 12. Persistent State

State is split into a global file, one per post, and a per-invocation run summary.

### Global `watcher-state.json`
Updated at the end of every invocation.
```json
{
  "last_input_file": "/abs/path/to/watchlist.txt",
  "last_run_id": "2026-05-21T14:30:05Z",
  "last_run_duration_seconds": 73,
  "last_run_url_count": 3,
  "last_run_new_count": 5,
  "last_run_edited_count": 1,
  "last_run_deleted_count": 0,
  "last_run_exit_code": 0,
  "total_runs": 142
}
```

### Per-post `posts/<slug>/post-meta.json`
```json
{
  "post_url": "https://nextdoor.com/p/abc123",
  "post_slug": "p_abc123",
  "first_seen_at": "2026-05-21T14:00:00Z",
  "last_scrape_at": "2026-05-21T14:30:05Z",
  "last_scrape_result": "ok",
  "last_scrape_new_count": 2,
  "last_scrape_edited_count": 0,
  "last_scrape_deleted_count": 0,
  "total_seen": 47,
  "total_currently_live": 45,
  "total_edited_ever": 3,
  "total_deleted_ever": 2
}
```

### Per-post `posts/<slug>/seen.json`

A JSON object keyed by `comment_id`. Each entry tracks the comment's current status, current content hash, and a revision history of edits and deletion. Loaded at the start of each invocation, written back after each successful per-URL scrape. Append-only at the entry level — entries are never removed, only updated.

```json
{
  "c_aaa": {
    "status": "live",
    "id_source": "dom",
    "first_seen_at": "2026-05-21T14:32:10Z",
    "last_seen_at": "2026-05-22T09:15:00Z",
    "current_content_hash": "5f3a...",
    "current_body_text": "We should petition the city...",
    "missing_streak": 0,
    "revisions": [
      {
        "observed_at": "2026-05-21T14:32:10Z",
        "run_id": "2026-05-21T14:30:05Z",
        "class": "new",
        "content_hash": "9c12...",
        "body_text": "We should petition the city about new development",
        "screenshot": "captures/2026-05-21T14-32-10Z__c_aaa__new.png"
      },
      {
        "observed_at": "2026-05-22T09:15:00Z",
        "run_id": "2026-05-22T09:15:00Z",
        "class": "edited",
        "content_hash": "5f3a...",
        "body_text": "We should petition the city...",
        "screenshot": "captures/2026-05-22T09-15-00Z__c_aaa__edited.png"
      }
    ]
  },
  "c_bbb": {
    "status": "deleted",
    "id_source": "dom",
    "first_seen_at": "2026-05-20T10:00:00Z",
    "last_seen_at": "2026-05-22T20:00:00Z",
    "current_content_hash": "ee01...",
    "current_body_text": "Last known text before deletion",
    "missing_streak": 2,
    "first_missing_at": "2026-05-23T05:00:00Z",
    "confirmed_deleted_at": "2026-05-23T11:00:00Z",
    "revisions": [
      { "observed_at": "2026-05-20T10:00:00Z", "run_id": "...", "class": "new", "content_hash": "ee01...", "body_text": "...", "screenshot": "..." },
      { "observed_at": "2026-05-23T11:00:00Z", "run_id": "...", "class": "deleted", "content_hash": null, "body_text": null, "screenshot": null }
    ]
  }
}
```

#### Status values
- `live` — present on most recent scrape.
- `missing` — absent on the most recent scrape but `missing_streak < 2`. Not yet confirmed as deleted. Stored only in memory between successive runs via `missing_streak` and `first_missing_at`.
- `deleted` — absent on two consecutive scrapes. Terminal status (no automatic recovery; see §8 deletion caveat).

#### Atomic writes
`seen.json` must be written atomically: write to `seen.json.tmp` in the same directory, `fsync`, then `os.replace()` over `seen.json`. A crash mid-write must never leave a half-written index. The same rule applies to `post-meta.json` and `watcher-state.json`.

### Per-invocation `runs/<run_id>.json`
A small audit record per invocation. Kept for 90 days, then pruned during normal runs.
```json
{
  "run_id": "2026-05-21T14:30:05Z",
  "started_at": "2026-05-21T14:30:05Z",
  "ended_at": "2026-05-21T14:31:18Z",
  "duration_seconds": 73,
  "exit_code": 0,
  "urls": [
    {"url": "https://nextdoor.com/p/abc123", "ok": true,  "new": 2, "edited": 1, "deleted": 0, "took_seconds": 21},
    {"url": "https://nextdoor.com/p/def456", "ok": true,  "new": 0, "edited": 0, "deleted": 0, "took_seconds": 14},
    {"url": "https://nextdoor.com/p/ghi789", "ok": false, "error": "timeout after 3 retries"}
  ]
}
```

---

## 13. Logging

- Plain text log at `<output-dir>/logs/watcher.log`, rotated daily (keep last 14 days).
- One **invocation start** line: `2026-05-21T14:30:05Z run start run_id=... urls=3 input=/abs/path/watchlist.txt`.
- One line **per URL scraped** at INFO level: `2026-05-21T14:30:09Z scrape ok url=p_abc123 new=2 edited=1 deleted=0 total=47 took=4.2s`.
- One **invocation end** line: `2026-05-21T14:31:18Z run end run_id=... new=5 edited=1 deleted=0 took=73s exit=0`.
- WARN for transient failures and retries, stale-lock recoveries, and invalid lines in the input file.
- ERROR for fatal conditions (auth, unrecoverable Playwright crash, input file unreadable).
- Stdout mirrors the same lines unless `--quiet` is passed. Cron normally emails stdout, so `--quiet` is the recommended way to keep cron mail signal-only.

---

## 14. Scheduling (cron, Linux)

The tool does not schedule itself. On Linux, install it as a cron job. Other platforms are out of scope for this version.

### Cron line

Every 15 minutes, log to a rolling file:

```cron
*/15 * * * * cd /home/me/nextdoor && /usr/local/bin/nextdoor-watcher scrape \
    --input-file ./watchlist.txt \
    --output-dir  ./data \
    --quiet \
    >> ./data/logs/cron.out 2>&1
```

The `install-cron` subcommand prints exactly this line (parameterized by your `--input-file`, `--every`, and `--output-dir`) so you can paste it into `crontab -e`.

### Recommended hardening

- Use **absolute paths** for the binary, input file, and output dir. Cron has a minimal `PATH`.
- Set `MAILTO=you@example.com` at the top of the crontab so non-zero exits trigger email.
- Wrap the command in `flock` for belt-and-suspenders concurrency safety on top of the tool's own lock:
  ```cron
  */15 * * * * flock -n /tmp/nextdoor.flock -c '/usr/local/bin/nextdoor-watcher scrape --input-file /home/me/nextdoor/watchlist.txt --output-dir /home/me/nextdoor/data --quiet'
  ```

### Choosing an interval

| Watchlist size | Recommended minimum interval |
|----------------|-----------------------------:|
| 1–5 URLs       | 5 minutes                    |
| 6–15 URLs      | 10 minutes                   |
| 16–30 URLs     | 15 minutes                   |
| 31–60 URLs     | 30 minutes                   |

Rule of thumb: a single URL scrape takes roughly 5–20s including the polite pause. If your interval × 60 is less than `(avg_seconds_per_url × url_count)`, runs will start overlapping. The lock file will prevent damage, but you'll see steady `exit=3` (skipped) entries — bump your interval.

### Monitoring

The cron job's stderr/stdout file is your first line of defense. The watcher also includes a built-in SMTP email notifier for hard failures — see §15. For more sophisticated monitoring, the user can additionally:
- Periodically check `nextdoor-watcher status` to confirm everything is healthy.
- Wire the cron job into an external monitor (e.g. healthchecks.io ping on success).

---

## 15. Notifications (SMTP email)

The watcher can send an email when an invocation ends in a hard failure. This is the only built-in alerting channel.

### What triggers a notification

| Exit code | Sends email? | Reason                                                              |
|----------:|-------------:|---------------------------------------------------------------------|
| `0`       | No           | Success.                                                            |
| `1`       | **Yes**      | Bad input — config or watchlist is broken; user must fix.           |
| `2`       | **Yes**      | Auth failure — user must re-run `login`.                            |
| `3`       | No           | Skipped (previous run still in flight). Expected, self-correcting.  |
| `4`       | **Yes**      | Partial failure — at least one URL failed after retries.            |
| `5`       | **Yes**      | Unrecoverable runtime error.                                        |

In addition, a notification is sent if the watcher has had **N consecutive non-`0` non-`3` exits** (default `N = 3`, configurable) even if each individual run already emailed — this is the "you are still broken" reminder.

### Cooldown / throttling

A cron job that fires every 15 minutes will produce 96 emails per day if a session expires overnight. To prevent that:

- Each distinct **failure class** (one of `bad_input`, `auth`, `partial`, `runtime`) has its own cooldown timer, default **6 hours**, configurable per class.
- If an email for that class was sent within the cooldown window, the current invocation **does not send another email** but does log `notification suppressed: cooldown active, last_sent=... class=...`.
- The cooldown is recorded in a tiny state file at `<output-dir>/notifications-state.json`:
  ```json
  {
    "last_sent": {
      "auth":      "2026-05-22T03:14:00Z",
      "partial":   "2026-05-21T22:00:00Z",
      "bad_input": null,
      "runtime":   null
    },
    "consecutive_failure_streak": 7,
    "last_streak_email_at": "2026-05-22T03:14:00Z"
  }
  ```
- A successful run (`exit=0`) resets `consecutive_failure_streak` to 0 and triggers an optional **recovery email** ("watcher is healthy again") if `notify.send_recovery = true` in config. Recovery emails ignore cooldown.

### Email content

**Subject** (single line, machine-parseable prefix):
```
[nextdoor-watcher] FAIL exit=2 host=watcher-01 — session expired
```

**Body** (plain text):
```
Nextdoor Watcher hard failure.

Host:        watcher-01.local
Run ID:      2026-05-22T03:14:00Z
Exit code:   2 (auth failure)
Started at:  2026-05-22T03:14:00Z
Ended at:    2026-05-22T03:14:08Z
Duration:    8s
Input file:  /home/me/nextdoor/watchlist.txt
Output dir:  /home/me/nextdoor/data

Summary:
  Session expired. Storage state at /home/me/nextdoor/session/storage_state.json
  no longer authenticates against Nextdoor.

Action required:
  Run `nextdoor-watcher login` on the host to refresh the session.

Recent log tail (last 20 lines):
  2026-05-22T03:14:01Z run start run_id=...
  2026-05-22T03:14:07Z ERROR session expired url=https://nextdoor.com/p/abc123
  2026-05-22T03:14:08Z run end exit=2

This is an automated message from nextdoor-watcher on watcher-01.
Cooldown: no further `auth` notifications will be sent before 2026-05-22T09:14:00Z.
```

Recovery emails follow the same shape with subject prefix `[nextdoor-watcher] OK recovered` and a body that names how long the failure streak lasted.

### Configuration

SMTP settings live in `nextdoor-watcher.toml`. The **password is read from an environment variable only** — never from the config file, never from the CLI:

```toml
[notify]
enabled         = true
to              = ["you@example.com", "ops@example.com"]
from            = "watcher@example.com"
subject_prefix  = "[nextdoor-watcher]"      # default
send_recovery   = true                       # default false
consecutive_failure_threshold = 3            # default 3

[notify.smtp]
host            = "smtp.gmail.com"
port            = 587
username        = "watcher@example.com"
password_env    = "NEXTDOOR_WATCHER_SMTP_PASSWORD"   # name of the env var holding the password
use_starttls    = true                       # default true (port 587)
use_ssl         = false                      # default false (set true for port 465)
timeout_seconds = 15                         # default 15

[notify.cooldown_hours]
auth      = 6     # default 6
bad_input = 6
partial   = 1     # transient failures may resolve themselves; shorter cooldown
runtime   = 6
```

Resolution order for the password, in order:
1. The environment variable named by `password_env`. If set and non-empty, use it.
2. Otherwise, do not authenticate (anonymous SMTP). This is rare but valid for some relays.

The watcher **never** logs the password and never echoes it to stdout.

### Behavior when SMTP itself fails

If the watcher can't send the email (SMTP timeout, auth rejection, DNS failure):
- Log an ERROR line: `notification send failed: <reason>`.
- Do **not** change the watcher's exit code on account of SMTP failure — the email is a side channel.
- Do **not** retry within the same invocation. The next failed run will try again, subject to cooldown.
- Increment a counter in `notifications-state.json` (`smtp_send_failures: N`) so `status` can surface it.

If `notify.enabled = false` or the `[notify]` block is missing, all notification logic is a no-op.

### `test-notify` subcommand

Sends a one-off test email to confirm SMTP is configured correctly. Bypasses cooldown.

```
nextdoor-watcher test-notify
```

Output on stdout: `sent test email to you@example.com, ops@example.com` (exit `0`) or `SMTP error: <reason>` (exit `5`). Use this immediately after editing config; do not wait for a real failure to find out your SMTP is wrong.

### Security notes

- The config file path and the env var name are not secrets, but the env var **value** is. Document in the README:
  - Set the env var in the cron environment via `~/.profile`, a systemd `EnvironmentFile`, or by inlining it in the crontab line: `NEXTDOOR_WATCHER_SMTP_PASSWORD=… */15 * * * * …`.
  - Inlining in crontab is **not recommended** — anyone who can read `/var/spool/cron/crontabs/<user>` will see it. Prefer an env file with `chmod 600`.
- For Gmail / Google Workspace, the password must be an **app password**, not the account password. Document this in `EXAMPLES.md`.
- `notifications-state.json` is not sensitive (timestamps only) but should still live inside `<output-dir>/`.

---

## 16. Configuration File (optional)

Support a `nextdoor-watcher.toml` in the working directory so users can avoid long CLI invocations. Notification settings (see §15) live in the same file.

```toml
input_file     = "./watchlist.txt"
output_dir     = "./watch-data"
first_run_mode = "seed"
quiet          = true

# Notification settings — see §15 for full schema
[notify]
enabled        = true
to             = ["you@example.com"]
from           = "watcher@example.com"
send_recovery  = true

[notify.smtp]
host         = "smtp.gmail.com"
port         = 587
username     = "watcher@example.com"
password_env = "NEXTDOOR_WATCHER_SMTP_PASSWORD"
use_starttls = true
```

CLI flags override config file values. The SMTP password is **never** in the config file — only the name of the env var that holds it.

---

## 17. Error Handling & Exit Codes

| Exit code | Meaning                            | When                                                                              |
|----------:|------------------------------------|-----------------------------------------------------------------------------------|
| `0`       | Success                            | All URLs scraped without fatal errors. Some may have had zero new comments.       |
| `1`       | Bad input                          | Input file missing, unreadable, or has zero valid URLs. Invalid CLI flags.        |
| `2`       | Auth failure                       | `storage_state.json` missing or session expired. User must run `login`.           |
| `3`       | Skipped (already running)          | Another invocation holds the lock. Normal, expected outcome on overlap.           |
| `4`       | Partial failure                    | At least one URL failed with a transient error after retries. Others succeeded.   |
| `5`       | Unrecoverable runtime error        | Playwright crash, disk full, permission error. Investigate the log.               |

Per-URL conditions handled **within** an invocation (do not abort the run):

| Condition                          | Behavior                                                |
|------------------------------------|---------------------------------------------------------|
| Network timeout / HTTP 5xx         | Retry with backoff (3 attempts), then skip the URL.     |
| Selector returned zero comments    | Log WARN ("page layout may have changed"), skip URL.    |
| Page redirected unexpectedly       | Log ERROR for that URL, skip URL, continue.             |
| Invalid URL line in input file     | Log WARN, skip that line, continue with the rest.       |
| `Ctrl-C` / SIGINT mid-run          | Finish current capture, persist state, release lock, exit `130` (standard SIGINT). |

---

## 18. Testing Requirements

The implementer must deliver:

1. **Unit tests** for the diff engine using fixture HTML files (saved snapshots of real comment threads, sanitized). Tests should cover:
   - First-run seed mode records IDs and content hashes without capturing.
   - First-run capture-all mode captures everything.
   - Second invocation with no changes produces zero captures.
   - Second invocation with N new comments produces exactly N `new` captures.
   - Second invocation where one comment's body has changed produces exactly one `edited` capture, and `seen.json` records the previous revision.
   - A comment missing on one scrape stays `live` with `missing_streak = 1` and no `deleted` capture is written.
   - A comment missing on **two consecutive** scrapes produces exactly one `deleted` capture and the entry transitions to `status: "deleted"`.
   - A previously-deleted comment that reappears is recorded as a fresh `new` capture (no resurrection logic).
   - Replies are distinguished from top-level comments.
2. **Unit tests** for the input file loader:
   - Comments, blank lines, and whitespace are handled correctly.
   - Non-Nextdoor URLs are rejected.
   - Duplicates after normalization are de-duped with a warning.
   - Missing file is reported clearly.
3. **Unit tests** for the lock file:
   - A second invocation while the first is running exits `3`.
   - A stale lock (dead PID, old heartbeat) is recovered with a WARN.
   - The lock is released on normal exit, on `Ctrl-C`, and on unexpected exceptions.
4. **Unit tests** for atomic state writes:
   - `seen.json` is never observed half-written, even when the process is killed mid-write.
5. **Unit tests** for notifications (see §15):
   - Each non-zero, non-`3` exit code triggers an email; `0` and `3` do not.
   - Cooldown suppresses a second email of the same class within the window, and the suppression is logged.
   - Distinct failure classes have independent cooldowns (an `auth` email does not suppress a `partial` email).
   - After N consecutive failures (threshold from config), a "still broken" reminder is sent even if the per-class cooldown would otherwise suppress it.
   - A recovery email is sent on transition from a failure streak to `exit=0`, but only when `send_recovery = true`.
   - The SMTP password is resolved from the env var named by `password_env`; the watcher never writes the password to any log, the run summary, or the notification state file.
   - When SMTP itself fails, the watcher logs an ERROR, increments `smtp_send_failures`, and does **not** change its own exit code.
   - When `notify.enabled = false` or `[notify]` is missing, no email logic runs at all.
6. **Integration test** (manual, documented in README): a recipe that
   1. Runs `login`, then `scrape` once to seed a test post.
   2. Posts a new comment from a second account → next `scrape` captures it as `new`.
   3. Edits that comment from the second account → next `scrape` captures it as `edited`.
   4. Deletes the comment → next two `scrape` runs confirm it as `deleted`.
7. **Notifications smoke test** (manual): set `notify.enabled = true`, set the env var, run `nextdoor-watcher test-notify`, confirm receipt. Then intentionally rename `storage_state.json` and run `scrape` — confirm exactly one `auth` failure email arrives and that a second `scrape` within the cooldown window does **not** trigger a second email.
8. **Cron smoke test** (manual, Linux): install the cron line emitted by `install-cron`, wait two intervals, and confirm two run-summary files appear under `runs/`.
9. **A `--dry-run` flag on `scrape`** that performs everything except writing PNG and JSON files — useful for testing selectors and cron wiring without polluting the output dir.

---

## 19. Deliverables

- Source code in a single repo with a clear `README.md`.
- `requirements.txt` / `package.json` pinning Playwright version.
- A `playwright install chromium` step documented in setup.
- An `EXAMPLES.md` showing the canonical workflows: `login`, `validate`, `scrape` (manual), installing the cron job, `status`, adding/removing URLs between runs, and configuring SMTP notifications (including the Gmail app-password caveat and how to set the `NEXTDOOR_WATCHER_SMTP_PASSWORD` env var for a cron environment).
- A `SELECTORS.md` explaining how to update CSS selectors when Nextdoor's DOM changes — including which Chrome DevTools steps to use to find the new ones.
- A sample `watchlist.txt` in the repo.
- A sample `nextdoor-watcher.toml` covering the SMTP block.
- A sample cron snippet in `EXAMPLES.md`.

---

## 20. Resolved Design Decisions

The following decisions are locked in for this version:

1. **Screenshots include the avatar.** Element-scoped capture of the full comment node, including the author's avatar.
2. **Edits are captured.** A change in a comment's normalized body text (detected via `content_hash` change) produces a new `edited` PNG + JSON pair and a revision entry in `seen.json`.
3. **Deletions are tracked.** A comment missing on two consecutive scrapes transitions to `status: "deleted"` in `seen.json`. A JSON-only "deleted" sidecar is written (no PNG, since the comment is gone). No automatic resurrection.
4. **Image format: PNG only.** No JPEG, no WebP.
5. **`install-cron` is Linux only and print-only.** It emits a cron line for the user to paste; it does not modify the crontab itself. macOS and Windows are out of scope for this version.
6. **Run summaries are pruned implicitly at 90 days.** No dedicated `prune` subcommand — pruning happens at the start of normal `scrape` runs.
