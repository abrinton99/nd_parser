# nextdoor-watcher

A one-shot, cron-friendly command-line tool that screenshots **new, edited, and
deleted comments** on a user-defined list of Nextdoor posts.

Each invocation reads your watchlist, polls every URL once, captures any changed
comments as element-scoped PNG screenshots plus sidecar JSON, persists state,
and exits. Scheduling is the OS's job — install it as a cron job. There is no
background daemon, so a single bad run can never take down a long-lived process.

> You are responsible for ensuring your use complies with Nextdoor's Terms of
> Service. This tool does not bypass authentication or rate limits.

## How it works

```
OS cron fires  ->  nextdoor-watcher scrape  ->  per-post screenshots + state
```

1. Acquire a lock (skip cleanly if a previous run is still going).
2. Load the saved browser session (`./session/storage_state.json`).
3. Read + validate the watchlist.
4. For each URL: navigate, expand all comments, diff against `seen.json`,
   screenshot anything new/edited, record deletions, persist state.
5. Write a run summary and exit with a meaningful code.

## Install

Requires **Python 3.10+** (3.11+ recommended).

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # downloads the Chromium engine Playwright drives
```

Or install as a console script:

```bash
pip install .
playwright install chromium
nextdoor-watcher --help
```

If you run from a source checkout without installing, use `python -m nextdoor_watcher …`.

## Quick start

```bash
# 1. Log in once (opens a real browser window; finish 2FA/captcha, press Enter).
nextdoor-watcher login

# 2. Create a watchlist (see watchlist.txt for the format).
echo "https://nextdoor.com/p/abc123" > watchlist.txt

# 3. Validate it before scheduling.
nextdoor-watcher validate --input-file watchlist.txt

# 4. Scrape once. The first run "seeds" existing comments without capturing them.
nextdoor-watcher scrape --input-file watchlist.txt --output-dir ./data

# 5. Print a cron line to run it every 15 minutes.
nextdoor-watcher install-cron --input-file watchlist.txt --every 15 --output-dir ./data
```

See [EXAMPLES.md](EXAMPLES.md) for the full set of workflows, including SMTP
notifications and the Gmail app-password caveat.

## Commands

| Command | Purpose |
|---------|---------|
| `login` | Open a browser, sign in, and save the session to `./session/storage_state.json`. |
| `scrape` | The core one-shot command. Reads input, scrapes once, writes captures + state. |
| `validate` | Report `OK` / `SKIP` / `BAD` for each watchlist line. Exit 0 only if all are OK. |
| `status` | Show last run, watched posts, session age, and lock state. |
| `install-cron` | Print a cron line to paste into your crontab (Linux only; does not edit it). |
| `test-notify` | Send a one-off test email to confirm SMTP config (bypasses cooldown). |

### `scrape` flags

- `--input-file PATH` (required) — the watchlist.
- `--output-dir DIR` — base dir for screenshots and state. Default `./nextdoor-watcher-data/`.
- `--first-run-mode {seed,capture-all}` — on a URL's *first ever* scrape, `seed`
  (default) records existing comments without screenshotting them; `capture-all`
  screenshots them too.
- `--dry-run` — do everything except write PNG/JSON/state files (selector + cron testing).
- `--quiet` — suppress stdout; log to file only. Recommended for cron.

## Exit codes

| Code | Meaning | Sends email? |
|-----:|---------|:------------:|
| 0 | Success | no |
| 1 | Bad input (missing/empty watchlist, bad flags) | **yes** |
| 2 | Auth failure (session missing/expired — run `login`) | **yes** |
| 3 | Skipped (another run holds the lock) | no |
| 4 | Partial failure (some URLs failed after retries) | **yes** |
| 5 | Unrecoverable runtime error | **yes** |
| 130 | Interrupted (Ctrl-C); state persisted, lock released | no |

## Output layout

```
<output-dir>/
├── watcher-state.json            # global state
├── posts/<slug>/
│   ├── post-meta.json            # url, totals, last-scrape result
│   ├── seen.json                 # comment id -> status + content hash + revisions
│   └── captures/
│       ├── <run_id>__<comment_id>__new.png + .json
│       ├── <run_id>__<comment_id>__edited.png + .json
│       └── <run_id>__<comment_id>__deleted.json   # JSON only; the comment is gone
├── runs/<run_id>.json            # per-invocation audit (pruned after 90 days)
├── watcher.lock                  # PID + heartbeat
├── notifications-state.json      # cooldown timers + failure streak
└── logs/watcher.log              # rotated daily, 14 days kept
```

Glob `*__<comment_id>__*` to see a single comment's full history.

## Selectors

Nextdoor's DOM changes across releases. All CSS selectors live in
[nextdoor_watcher/selectors.py](nextdoor_watcher/selectors.py); see
[SELECTORS.md](SELECTORS.md) for how to refresh them with Chrome DevTools when
scrapes start returning zero comments.

## Tests

```bash
pip install pytest
pytest
```

Unit tests cover the diff engine (new/edited/deleted/seed/capture-all), the
input loader, the lock file, atomic writes, and notifications — none of them
require a browser. Manual integration recipes are documented in [EXAMPLES.md](EXAMPLES.md).

## Security

`./session/storage_state.json` holds your logged-in cookies. Treat it as a
secret — the tool `chmod 600`s it on save (Unix). Never commit it. The SMTP
password is read **only** from the environment variable named by `password_env`
in the config; it is never stored in config, logs, or state files.
