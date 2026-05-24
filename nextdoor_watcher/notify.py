"""SMTP failure notifications (spec §15).

Design notes:
  * The password is read ONLY from the env var named by `password_env`, never
    from config, CLI, logs, or the notification-state file.
  * Each failure class (auth/bad_input/partial/runtime) has an independent
    cooldown. A "still broken" streak reminder bypasses per-class cooldown.
  * SMTP failures never change the watcher's exit code; they bump a counter so
    `status` can surface them.
"""

from __future__ import annotations

import os
import smtplib
import socket
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from .config import NotifyConfig
from .exit_codes import FAILURE_CLASS
from .util import atomic_write_json, iso, parse_iso, read_json, utc_now

FAILURE_CLASSES = ("auth", "bad_input", "partial", "runtime")
EXIT_CODE_LABEL = {
    1: "bad input",
    2: "auth failure",
    4: "partial failure",
    5: "runtime error",
}
STREAK_REMINDER_COOLDOWN_HOURS = 6.0


@dataclass
class RunContext:
    run_id: str
    exit_code: int
    started_at: str
    ended_at: str
    duration_seconds: int
    input_file: str
    output_dir: str
    summary_text: str = ""
    log_tail: str = ""
    host: str = field(default_factory=socket.gethostname)


def _empty_state() -> dict:
    return {
        "last_sent": {c: None for c in FAILURE_CLASSES},
        "consecutive_failure_streak": 0,
        "last_streak_email_at": None,
        "smtp_send_failures": 0,
    }


class Notifier:
    def __init__(self, config: NotifyConfig, state_path: str | Path, logger):
        self.cfg = config
        self.state_path = Path(state_path)
        self.log = logger

    # ----- state -----------------------------------------------------------
    def _load_state(self) -> dict:
        state = read_json(self.state_path, None)
        if not isinstance(state, dict):
            return _empty_state()
        base = _empty_state()
        base.update(state)
        base["last_sent"] = {**_empty_state()["last_sent"], **(state.get("last_sent") or {})}
        return base

    def _save_state(self, state: dict) -> None:
        atomic_write_json(self.state_path, state)

    # ----- password (env only) --------------------------------------------
    def _password(self) -> str | None:
        env_name = self.cfg.smtp.password_env
        if not env_name:
            return None
        val = os.environ.get(env_name)
        return val or None

    # ----- SMTP send (monkeypatch target in tests) -------------------------
    def _send_email(self, subject: str, body: str) -> None:
        """Raises on failure. Never logs the password."""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.to)
        msg.set_content(body)

        smtp = self.cfg.smtp
        if smtp.use_ssl:
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=smtp.timeout_seconds)
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout_seconds)
        try:
            if smtp.use_starttls and not smtp.use_ssl:
                server.starttls()
            password = self._password()
            if smtp.username and password:
                server.login(smtp.username, password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def _try_send(self, subject: str, body: str, state: dict) -> bool:
        """Attempt a send; on failure log + bump counter, never raise."""
        try:
            self._send_email(subject, body)
            return True
        except Exception as exc:  # SMTP timeout, auth reject, DNS, ...
            self.log.error(f"notification send failed: {exc}")
            state["smtp_send_failures"] = int(state.get("smtp_send_failures", 0)) + 1
            return False

    # ----- public API ------------------------------------------------------
    def notify_run(self, ctx: RunContext) -> None:
        """Called at the end of every invocation. No-op if disabled."""
        if not self.cfg.enabled:
            return

        state = self._load_state()
        cls = FAILURE_CLASS.get(ctx.exit_code)

        if ctx.exit_code == 0:
            self._handle_success(ctx, state)
        elif cls is None:
            # exit 3 (skipped) and 130 (sigint) do not alert; leave streak intact.
            pass
        else:
            self._handle_failure(ctx, state, cls)

        self._save_state(state)

    def _handle_success(self, ctx: RunContext, state: dict) -> None:
        prior_streak = int(state.get("consecutive_failure_streak", 0))
        state["consecutive_failure_streak"] = 0
        if prior_streak > 0 and self.cfg.send_recovery:
            subject, body = build_recovery_email(self.cfg, ctx, prior_streak)
            if self._try_send(subject, body, state):  # recovery ignores cooldown
                self.log.info(f"recovery notification sent (streak was {prior_streak})")

    def _handle_failure(self, ctx: RunContext, state: dict, cls: str) -> None:
        state["consecutive_failure_streak"] = int(state.get("consecutive_failure_streak", 0)) + 1
        streak = state["consecutive_failure_streak"]

        sent_class_email = False
        if self._cooldown_ok(state, cls):
            subject, body = build_failure_email(self.cfg, ctx, cls)
            if self._try_send(subject, body, state):
                state["last_sent"][cls] = iso()
                self.log.info(f"notification sent class={cls} exit={ctx.exit_code}")
                sent_class_email = True
        else:
            last = state["last_sent"].get(cls)
            self.log.warning(
                f"notification suppressed: cooldown active, last_sent={last} class={cls}"
            )

        # "You are still broken" reminder — bypasses per-class cooldown.
        if (
            not sent_class_email
            and streak >= self.cfg.consecutive_failure_threshold
            and self._streak_cooldown_ok(state)
        ):
            subject, body = build_streak_email(self.cfg, ctx, streak)
            if self._try_send(subject, body, state):
                state["last_streak_email_at"] = iso()
                self.log.info(f"streak reminder sent (streak={streak})")

    def _cooldown_ok(self, state: dict, cls: str) -> bool:
        last = state["last_sent"].get(cls)
        if not last:
            return True
        hours = self.cfg.cooldown_hours.get(cls, 6.0)
        age_h = (utc_now() - parse_iso(last)).total_seconds() / 3600.0
        return age_h >= hours

    def _streak_cooldown_ok(self, state: dict) -> bool:
        last = state.get("last_streak_email_at")
        if not last:
            return True
        age_h = (utc_now() - parse_iso(last)).total_seconds() / 3600.0
        return age_h >= STREAK_REMINDER_COOLDOWN_HOURS

    def send_test(self) -> tuple[bool, str]:
        """test-notify: send a one-off email bypassing cooldown. Returns (ok, detail)."""
        host = socket.gethostname()
        subject = f"{self.cfg.subject_prefix} TEST — configuration check from {host}"
        body = (
            "This is a test email from nextdoor-watcher.\n\n"
            f"Host: {host}\n"
            "If you received this, your SMTP settings are correct.\n"
        )
        try:
            self._send_email(subject, body)
            return True, ", ".join(self.cfg.to)
        except Exception as exc:
            return False, str(exc)


# --------------------------------------------------------------------------- #
# Email builders (pure; unit-testable)
# --------------------------------------------------------------------------- #
def _cooldown_footer(cfg: NotifyConfig, cls: str) -> str:
    hours = cfg.cooldown_hours.get(cls, 6.0)
    return (
        f"Cooldown: no further `{cls}` notifications will be sent for "
        f"approximately {hours:g}h."
    )


def build_failure_email(cfg: NotifyConfig, ctx: RunContext, cls: str) -> tuple[str, str]:
    label = EXIT_CODE_LABEL.get(ctx.exit_code, "failure")
    short = {
        "auth": "session expired",
        "bad_input": "bad input",
        "partial": "partial failure",
        "runtime": "runtime error",
    }.get(cls, "failure")
    subject = (
        f"{cfg.subject_prefix} FAIL exit={ctx.exit_code} host={ctx.host} — {short}"
    )
    body = (
        "Nextdoor Watcher hard failure.\n\n"
        f"Host:        {ctx.host}\n"
        f"Run ID:      {ctx.run_id}\n"
        f"Exit code:   {ctx.exit_code} ({label})\n"
        f"Started at:  {ctx.started_at}\n"
        f"Ended at:    {ctx.ended_at}\n"
        f"Duration:    {ctx.duration_seconds}s\n"
        f"Input file:  {ctx.input_file}\n"
        f"Output dir:  {ctx.output_dir}\n\n"
        "Summary:\n"
        f"  {ctx.summary_text}\n\n"
        "Recent log tail (last 20 lines):\n"
        f"{_indent(ctx.log_tail)}\n\n"
        f"This is an automated message from nextdoor-watcher on {ctx.host}.\n"
        f"{_cooldown_footer(cfg, cls)}\n"
    )
    return subject, body


def build_streak_email(cfg: NotifyConfig, ctx: RunContext, streak: int) -> tuple[str, str]:
    subject = (
        f"{cfg.subject_prefix} STILL FAILING streak={streak} host={ctx.host} "
        f"exit={ctx.exit_code}"
    )
    body = (
        f"Nextdoor Watcher has now failed {streak} consecutive runs.\n\n"
        f"Most recent exit code: {ctx.exit_code}\n"
        f"Run ID:                {ctx.run_id}\n"
        f"Input file:            {ctx.input_file}\n\n"
        "Summary:\n"
        f"  {ctx.summary_text}\n\n"
        "Recent log tail (last 20 lines):\n"
        f"{_indent(ctx.log_tail)}\n"
    )
    return subject, body


def build_recovery_email(cfg: NotifyConfig, ctx: RunContext, prior_streak: int) -> tuple[str, str]:
    subject = f"{cfg.subject_prefix} OK recovered host={ctx.host}"
    body = (
        "Nextdoor Watcher is healthy again.\n\n"
        f"Host:            {ctx.host}\n"
        f"Run ID:          {ctx.run_id}\n"
        f"Failure streak:  {prior_streak} run(s) before this success.\n"
        f"Ended at:        {ctx.ended_at}\n"
    )
    return subject, body


def _indent(text: str, prefix: str = "  ") -> str:
    if not text:
        return prefix + "(no log lines)"
    return "\n".join(prefix + line for line in text.splitlines())
