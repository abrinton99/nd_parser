"""Lock-file tests (spec §18.3)."""

from __future__ import annotations

import os

import pytest

from nextdoor_watcher import lockfile
from nextdoor_watcher.lockfile import (
    LockHeld,
    acquire_lock,
    read_lock,
    release_lock,
)
from nextdoor_watcher.util import iso, parse_iso
from datetime import timedelta


def test_second_acquire_while_live_raises(tmp_path):
    path = tmp_path / "watcher.lock"
    lock = acquire_lock(path, pid=os.getpid())
    with pytest.raises(LockHeld) as exc:
        acquire_lock(path, pid=os.getpid())
    assert exc.value.pid == os.getpid()
    release_lock(lock)


def test_stale_lock_dead_pid_recovered(tmp_path):
    path = tmp_path / "watcher.lock"
    # Write a lock for a PID that is virtually certain to be dead.
    from nextdoor_watcher.util import atomic_write_json
    atomic_write_json(path, {"pid": 999999, "heartbeat": iso()})
    warnings = []
    lock = acquire_lock(path, pid=os.getpid(), warn=warnings.append)
    assert read_lock(path)["pid"] == os.getpid()
    assert any("stale" in w for w in warnings)
    release_lock(lock)


def test_stale_lock_old_heartbeat_recovered(tmp_path, monkeypatch):
    path = tmp_path / "watcher.lock"
    from nextdoor_watcher.util import atomic_write_json, utc_now
    old = utc_now() - timedelta(hours=lockfile.STALE_THRESHOLD_HOURS + 1)
    # Use our own live PID but an ancient heartbeat -> still considered stale.
    atomic_write_json(path, {"pid": os.getpid(), "heartbeat": iso(old)})
    warnings = []
    lock = acquire_lock(path, pid=os.getpid(), warn=warnings.append)
    assert any("stale" in w for w in warnings)
    release_lock(lock)


def test_release_removes_lock(tmp_path):
    path = tmp_path / "watcher.lock"
    lock = acquire_lock(path, pid=os.getpid())
    assert path.exists()
    release_lock(lock)
    assert not path.exists()


def test_release_only_removes_own_lock(tmp_path):
    path = tmp_path / "watcher.lock"
    lock = acquire_lock(path, pid=os.getpid())
    # Simulate another process having recovered/overwritten the lock.
    from nextdoor_watcher.util import atomic_write_json
    atomic_write_json(path, {"pid": os.getpid() + 1, "heartbeat": iso()})
    release_lock(lock)
    assert path.exists()  # not ours anymore -> left in place
