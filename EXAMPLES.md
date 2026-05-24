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

## 4. Schedule it (WSL + Windows Task Scheduler)

WSL has no reliable always-on cron (its `cron` daemon only runs while a WSL
session is open), so scheduling is owned by **Windows Task Scheduler**, which
launches a generated wrapper script inside WSL via `wsl.exe`.

Run this **inside WSL**:

```bash
nextdoor-watcher install-schedule --input-file ./watchlist.txt --every 15 --output-dir ./data
```

It does two things:

1. **Writes** `./data/run-watcher.sh` — a wrapper that sources your SMTP env
   file, adds jitter, and runs one scrape with absolute paths. (Re-run
   `install-schedule` if you move the venv, watchlist, or output dir.)
2. **Prints** two ways to register the task. Run **one** of them in a **Windows**
   terminal (cmd or PowerShell), not WSL. The distro name is auto-filled from
   `$WSL_DISTRO_NAME`; if it shows `<your-distro>`, run `wsl -l -q` to find it.

```bat
:: Option A — schtasks (cmd.exe or PowerShell)
schtasks /Create /TN "NextdoorWatcher" /SC MINUTE /MO 15 /F ^
  /TR "wsl.exe -d Ubuntu -- bash /home/me/nd/data/run-watcher.sh"
```

```powershell
# Option B — PowerShell (sub-daily repetition, survives reboots)
$a = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument '-d Ubuntu -- bash /home/me/nd/data/run-watcher.sh'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName 'NextdoorWatcher' -Action $a -Trigger $t
```

Notes:
- Minimum interval is **2 minutes**; `install-schedule` rejects anything lower.
- In Task Scheduler properties, set the task to run **whether the user is logged
  on or not** (and to **wake the computer** if you need overnight coverage).
- Task Scheduler's **Last Run Result** reflects the watcher's exit code (`0x0` = success).
- Suggested intervals: 1–5 URLs → 5 min, 6–15 → 10 min, 16–30 → 15 min, 31–60 → 30 min.
- Remove it later: `schtasks /Delete /TN "NextdoorWatcher" /F` (in Windows).

> You can test exactly what the scheduler runs, from inside WSL, with:
> `bash ./data/run-watcher.sh`

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

### Setting the env var for the scheduled run

Windows Task Scheduler launches the wrapper with a minimal environment, so the
wrapper sources `~/.nextdoor-watcher.env` (inside WSL). Put the password there —
this is the **only** place it should live (never in the Windows task or a
`wsl.exe` command line, which a Windows process listing could expose):

```bash
# Create the env file with strict perms (run inside WSL):
umask 077
printf 'NEXTDOOR_WATCHER_SMTP_PASSWORD=your-app-password\n' > ~/.nextdoor-watcher.env
chmod 600 ~/.nextdoor-watcher.env
```

The wrapper written by `install-schedule` already begins with:

```bash
if [ -f "$HOME/.nextdoor-watcher.env" ]; then . "$HOME/.nextdoor-watcher.env"; fi
```

so the scheduled scrape picks the password up automatically. For a manual run in
your own shell, just `export NEXTDOOR_WATCHER_SMTP_PASSWORD=...` first.

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

## 10. Schedule smoke test

Run `install-schedule`, register the task with the emitted `schtasks`/PowerShell
command, wait two intervals, and confirm two new files appear under
`data/runs/` (and that Task Scheduler shows **Last Run Result** `0x0`). To dry-run
the exact command the scheduler uses, from inside WSL: `bash ./data/run-watcher.sh`.
