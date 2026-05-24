# EXAMPLES

Canonical workflows for nextdoor-watcher. Commands assume the console script
`nextdoor-watcher` is on your PATH; from a source checkout substitute
`python -m nextdoor_watcher`.

## 1. Log in (once)

```bash
nextdoor-watcher login
```

Opens a visible Chromium window at `https://nextdoor.com/login/`. Complete
sign-in including 2FA/captcha. The tool detects the logged-in state
automatically; you can also press **Enter** in the terminal when done. The
session is saved to `./session/storage_state.json` and `chmod 600`'d.

Re-run `login` whenever scrapes start exiting with code `2`.

## 2. Validate the watchlist

```bash
nextdoor-watcher validate --input-file watchlist.txt
```

```
OK   line 5: https://nextdoor.com/p/abc123
SKIP line 6: duplicate (duplicate of line 5 after normalization)
BAD  line 7: https://example.com/x — not an https nextdoor.com URL
```

Exits `0` only if every non-skip line is `OK`. Run this before installing cron.

## 3. Scrape manually

```bash
# First run: seed existing comments (no screenshots of pre-existing ones).
nextdoor-watcher scrape --input-file watchlist.txt --output-dir ./data

# Or capture everything already on the page on the first run:
nextdoor-watcher scrape --input-file watchlist.txt --output-dir ./data --first-run-mode capture-all

# Test selectors / cron wiring without writing any files:
nextdoor-watcher scrape --input-file watchlist.txt --output-dir ./data --dry-run
```

## 4. Install the cron job (Linux)

```bash
nextdoor-watcher install-cron --input-file ./watchlist.txt --every 15 --output-dir ./data
```

This **prints** a line; it does not edit your crontab. Paste it into `crontab -e`:

```cron
MAILTO=you@example.com
*/15 * * * * sleep $((RANDOM \% 60)); /path/to/nextdoor-watcher scrape \
    --input-file /abs/watchlist.txt --output-dir /abs/data --quiet \
    >> /abs/data/logs/cron.out 2>&1
```

Notes:
- Minimum interval is **2 minutes**; `install-cron` rejects anything lower.
- Use **absolute paths** — cron has a minimal PATH.
- The `sleep $((RANDOM \% 60))` jitter keeps your traffic from looking metronomic.
- Suggested intervals: 1–5 URLs → 5 min, 6–15 → 10 min, 16–30 → 15 min, 31–60 → 30 min.

## 5. Check status

```bash
nextdoor-watcher status --output-dir ./data
```

Shows the last run's outcome, every watched post with its last-scrape time and
seen counts, the session age (flagged if it looks expired), whether the lock is
held, and any SMTP send failures.

## 6. Add / remove URLs between runs

The watchlist is re-read on **every** invocation — just edit the file.

- **Add** a URL: it starts being scraped next run (subject to `--first-run-mode`).
- **Remove** a URL: it stops being scraped next run. Its folder, `seen.json`,
  and captures are **left in place** — the tool never deletes captures.

## 7. SMTP email notifications

The watcher emails you on hard failures (exit 1/2/4/5). Configure it in
`nextdoor-watcher.toml`:

```toml
[notify]
enabled        = true
to             = ["you@example.com"]
from           = "watcher@example.com"
send_recovery  = true              # also email when it recovers

[notify.smtp]
host         = "smtp.gmail.com"
port         = 587
username     = "watcher@example.com"
password_env = "NEXTDOOR_WATCHER_SMTP_PASSWORD"   # NAME of the env var, not the password
use_starttls = true

[notify.cooldown_hours]
auth      = 6
partial   = 1
```

The **password is read only from the environment variable** named by
`password_env` — never from the config file, CLI, or logs.

### Gmail / Google Workspace caveat

You must use an **app password**, not your account password:
1. Enable 2-Step Verification on the Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. Export it as the env var below.

### Setting the env var for cron

Cron does not inherit your shell environment. Pick one:

```bash
# (a) An env file with chmod 600, sourced by the crontab line (recommended):
echo 'NEXTDOOR_WATCHER_SMTP_PASSWORD=your-app-password' > ~/.nextdoor-watcher.env
chmod 600 ~/.nextdoor-watcher.env
# crontab line:
*/15 * * * * . ~/.nextdoor-watcher.env; nextdoor-watcher scrape --input-file /abs/wl.txt --output-dir /abs/data --quiet

# (b) systemd EnvironmentFile=, if you wrap the run in a service/timer.

# (c) Inline in the crontab (NOT recommended — readable by anyone with crontab access):
NEXTDOOR_WATCHER_SMTP_PASSWORD=... 
```

### Confirm SMTP works

```bash
export NEXTDOOR_WATCHER_SMTP_PASSWORD='your-app-password'
nextdoor-watcher test-notify --output-dir ./data
# -> sent test email to you@example.com
```

## 8. Manual integration test (real Nextdoor, two accounts)

1. `nextdoor-watcher login`, then `scrape` once to seed a test post.
2. From a second account, **post a new comment** → next `scrape` captures it as `new`.
3. **Edit** that comment → next `scrape` captures it as `edited` (with the previous revision in `seen.json`).
4. **Delete** the comment → the next **two** `scrape` runs confirm it as `deleted` (JSON-only sidecar).

## 9. Notifications smoke test

1. Set `notify.enabled = true`, export the password env var, run `test-notify`, confirm receipt.
2. `mv session/storage_state.json session/storage_state.json.bak`, run `scrape` → confirm exactly **one** `auth` failure email.
3. Run `scrape` again within the cooldown window → confirm **no** second email (look for `notification suppressed: cooldown active` in the log).
4. Restore the session file.

## 10. Cron smoke test

Install the line from `install-cron`, wait two intervals, and confirm two new
files appear under `data/runs/`.
