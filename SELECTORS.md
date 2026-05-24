# Updating selectors when Nextdoor's DOM changes

Nextdoor is a heavy SPA and its markup is **not stable across releases**. When a
release changes class names or attributes, scrapes start logging:

```
scrape warn url=p_abc123 zero comments (page layout may have changed)
```

All CSS selectors live in one place —
[nextdoor_watcher/selectors.py](nextdoor_watcher/selectors.py) — so you can fix
this without touching any logic. Each entry is either a single CSS string or a
**list of fallbacks tried in order** (first match wins). Add the new selector to
the front of the relevant list; keep the old ones as fallbacks.

## What each key targets

| Key | Should match |
|-----|--------------|
| `comment_container` | The element wrapping all comments on a post page. |
| `comment_node` | A single comment **or** reply. Each match becomes one extracted comment. |
| `expand_buttons` | "Show more comments" / "View more replies" buttons (clicked until gone). |
| `comment_body` | The text body inside a comment node. |
| `comment_author` | The author display name. |
| `comment_timestamp` | The relative time ("5m ago"). |
| `comment_edited_marker` | The "Edited" marker, if shown. |
| `comment_permalink` | An anchor whose `?comment=<id>` is the 2nd-choice comment id. |
| `comment_id_attributes` | DOM attributes inspected (in order) for the 1st-choice stable id. |
| `logged_in_signal` | An element present only when logged in (e.g. the comment composer). |
| `logged_out_signal` | An element present only when logged out (e.g. a "Sign up" banner). |

> **`:has-text()` is Playwright-only.** It works for `expand_buttons`,
> `logged_*_signal`, etc. in the live browser, but the static-HTML parser used by
> the tests ignores any selector containing `:has-text(`. For `comment_node`,
> `comment_body`, and the id sources, prefer plain CSS (attributes, classes,
> tags) so both paths behave identically.

## Finding new selectors with Chrome DevTools

1. Open a watched post in Chrome while logged in.
2. Open DevTools (**F12** or **Ctrl-Shift-I**) → **Elements**.
3. Click the inspector arrow (**Ctrl-Shift-C**) and hover a single comment.
   Find the smallest element that wraps the **whole** comment (avatar + author +
   body). That element's selector is your `comment_node`.
   - Right-click it → **Copy → Copy selector** for a starting point, then
     simplify to a stable attribute (e.g. `[data-testid="comment"]` or
     `[data-comment-id]`) rather than a brittle `:nth-child` chain.
4. Inside that node, repeat for the body, author, and timestamp.
5. **Verify a candidate** in the DevTools **Console** before editing code:
   ```js
   document.querySelectorAll('[data-testid="comment"]').length   // expect ~comment count
   $0.querySelector('[data-testid="comment-body"]')?.innerText    // with a comment selected as $0
   ```
6. **Find the stable id.** With a comment node selected as `$0`:
   ```js
   $0.attributes                                   // look for data-comment-id / id
   $0.querySelector('a[href*="comment="]')?.href   // permalink fallback
   ```
   List the winning attribute name first in `comment_id_attributes`. If no
   stable id exists, the tool falls back to a SHA-256 hash of
   `author + first 200 chars of body` (recorded as `id_source: "hash"` in the
   sidecar) — fragile, so prefer a real id.
7. **Login signals.** Inspect an element that exists only when logged in (the
   comment composer is reliable) and one that exists only when logged out (a
   "Sign up" banner). These drive session-expiry detection.

## After editing

```bash
nextdoor-watcher scrape --input-file watchlist.txt --output-dir ./data --dry-run
```

`--dry-run` runs the full scrape (navigation, expansion, extraction, diff) but
writes nothing — watch the log for non-zero comment counts to confirm your new
selectors match. If the saved test fixture
([tests/fixtures/thread_v1.html](tests/fixtures/thread_v1.html)) no longer
resembles live markup, re-capture and sanitize a fresh snapshot and update it
alongside the selectors.
