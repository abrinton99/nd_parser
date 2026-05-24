"""Notification tests (spec §18.5)."""

from __future__ import annotations

import logging

import pytest

from nextdoor_watcher.config import NotifyConfig, SmtpConfig
from nextdoor_watcher.notify import Notifier, RunContext
from nextdoor_watcher.util import read_json


def make_config(**kw) -> NotifyConfig:
    cfg = NotifyConfig(
        enabled=kw.get("enabled", True),
        to=["you@example.com"],
        sender="watcher@example.com",
        send_recovery=kw.get("send_recovery", False),
        consecutive_failure_threshold=kw.get("threshold", 3),
        smtp=SmtpConfig(host="localhost", username="u", password_env="TEST_PW_ENV"),
    )
    cfg.cooldown_hours = {"auth": 6.0, "bad_input": 6.0, "partial": 1.0, "runtime": 6.0}
    return cfg


class RecordingNotifier(Notifier):
    """Captures sends instead of talking to SMTP; can be told to fail."""

    def __init__(self, *a, fail=False, **kw):
        super().__init__(*a, **kw)
        self.sent = []
        self.fail = fail

    def _send_email(self, subject, body):
        if self.fail:
            raise OSError("simulated smtp failure")
        self.sent.append((subject, body))


def ctx(exit_code: int) -> RunContext:
    return RunContext(
        run_id="2026-05-22T03:14:00Z", exit_code=exit_code,
        started_at="2026-05-22T03:14:00Z", ended_at="2026-05-22T03:14:08Z",
        duration_seconds=8, input_file="/tmp/wl.txt", output_dir="/tmp/data",
        summary_text="something broke", log_tail="line1\nline2", host="watcher-01",
    )


def logger():
    lg = logging.getLogger("test_notify")
    lg.addHandler(logging.NullHandler())
    return lg


@pytest.mark.parametrize("code,should_send", [(0, False), (1, True), (2, True),
                                              (3, False), (4, True), (5, True)])
def test_exit_codes_trigger_email(tmp_path, code, should_send):
    n = RecordingNotifier(make_config(), tmp_path / "ns.json", logger())
    n.notify_run(ctx(code))
    assert bool(n.sent) == should_send


def test_cooldown_suppresses_second_same_class(tmp_path):
    n = RecordingNotifier(make_config(), tmp_path / "ns.json", logger())
    n.notify_run(ctx(2))   # auth -> sends
    n.notify_run(ctx(2))   # auth again, within cooldown -> suppressed
    assert len(n.sent) == 1


def test_distinct_classes_have_independent_cooldowns(tmp_path):
    n = RecordingNotifier(make_config(), tmp_path / "ns.json", logger())
    n.notify_run(ctx(2))   # auth
    n.notify_run(ctx(4))   # partial — different class, should still send
    subjects = " ".join(s for s, _ in n.sent)
    assert "exit=2" in subjects and "exit=4" in subjects


def test_streak_reminder_after_threshold(tmp_path):
    n = RecordingNotifier(make_config(threshold=3), tmp_path / "ns.json", logger())
    n.notify_run(ctx(2))   # 1: auth email
    n.notify_run(ctx(2))   # 2: suppressed by cooldown, streak=2
    n.notify_run(ctx(2))   # 3: suppressed, streak=3 -> reminder fires
    subjects = [s for s, _ in n.sent]
    assert any("STILL FAILING" in s for s in subjects)


def test_recovery_email_only_when_enabled(tmp_path):
    n = RecordingNotifier(make_config(send_recovery=True), tmp_path / "ns.json", logger())
    n.notify_run(ctx(2))   # failure, streak=1
    n.notify_run(ctx(0))   # success -> recovery
    assert any("recovered" in s for s, _ in n.sent)

    n2 = RecordingNotifier(make_config(send_recovery=False), tmp_path / "ns2.json", logger())
    n2.notify_run(ctx(2))
    n2.notify_run(ctx(0))
    assert not any("recovered" in s for s, _ in n2.sent)


def test_smtp_failure_increments_counter_no_raise(tmp_path):
    state_path = tmp_path / "ns.json"
    n = RecordingNotifier(make_config(), state_path, logger(), fail=True)
    n.notify_run(ctx(2))   # tries to send, fails
    state = read_json(state_path)
    assert state["smtp_send_failures"] == 1


def test_disabled_is_noop(tmp_path):
    state_path = tmp_path / "ns.json"
    n = RecordingNotifier(make_config(enabled=False), state_path, logger())
    n.notify_run(ctx(2))
    assert n.sent == []
    assert not state_path.exists()


def test_password_from_env_only_and_never_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PW_ENV", "s3cret-app-password")
    state_path = tmp_path / "ns.json"
    n = RecordingNotifier(make_config(), state_path, logger())
    assert n._password() == "s3cret-app-password"
    n.notify_run(ctx(2))
    # The password must never appear in the notification-state file.
    assert "s3cret-app-password" not in state_path.read_text()
