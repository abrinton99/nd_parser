"""Process exit codes (spec §17). Imported everywhere so the numbers stay in one place."""

OK = 0               # all URLs scraped without fatal errors
BAD_INPUT = 1        # input file missing/unreadable/empty, or bad CLI flags
AUTH = 2             # storage_state missing or session expired
SKIPPED = 3          # another invocation holds the lock (normal on overlap)
PARTIAL = 4          # at least one URL failed after retries; others succeeded
RUNTIME = 5          # unrecoverable runtime error (Playwright crash, disk full, ...)
SIGINT = 130         # Ctrl-C mid-run

# Maps exit code -> notification failure class (spec §15). None => no email.
FAILURE_CLASS = {
    BAD_INPUT: "bad_input",
    AUTH: "auth",
    PARTIAL: "partial",
    RUNTIME: "runtime",
}
