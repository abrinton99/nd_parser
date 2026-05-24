"""Single-invocation lock file with stale-lock recovery (spec §7).

The lock holds the running PID and a heartbeat timestamp. A new invocation that
finds a live PID exits 3 (skipped). A dead PID, or a heartbeat older than the
stale threshold, is treated as a crashed run and recovered with a WARN.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .util import atomic_write_json, iso, parse_iso, read_json, utc_now

STALE_THRESHOLD_HOURS = 6


class LockHeld(Exception):
    """Raised when a live lock is held by another invocation."""

    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"another scrape is in progress (pid={pid})")


@dataclass
class Lock:
    path: Path
    pid: int


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    except OSError:
        return False
    return True


def _is_stale(info: dict) -> bool:
    pid = int(info.get("pid", -1))
    if not _pid_alive(pid):
        return True
    hb = info.get("heartbeat")
    if hb:
        try:
            age_h = (utc_now() - parse_iso(hb)).total_seconds() / 3600.0
            if age_h > STALE_THRESHOLD_HOURS:
                return True
        except (ValueError, TypeError):
            return True
    return False


def acquire_lock(path: str | Path, *, pid: int | None = None, warn=lambda m: None) -> Lock:
    """Acquire the lock or raise LockHeld.

    Recovers a stale lock (dead PID or expired heartbeat) with a WARN log.
    """
    path = Path(path)
    pid = pid if pid is not None else os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = read_json(path)
    if isinstance(existing, dict) and "pid" in existing:
        if _is_stale(existing):
            warn(
                f"recovering stale lock (pid={existing.get('pid')}, "
                f"heartbeat={existing.get('heartbeat')})"
            )
        else:
            raise LockHeld(int(existing["pid"]))

    _write_lock(path, pid)
    return Lock(path=path, pid=pid)


def _write_lock(path: Path, pid: int) -> None:
    atomic_write_json(path, {"pid": pid, "heartbeat": iso(), "acquired_at": iso()})


def heartbeat(lock: Lock) -> None:
    """Refresh the heartbeat timestamp; called periodically during a long run."""
    info = read_json(lock.path, {}) or {}
    info.update({"pid": lock.pid, "heartbeat": iso()})
    atomic_write_json(lock.path, info)


def release_lock(lock: Lock | None) -> None:
    if lock is None:
        return
    try:
        # Only remove if it is still ours, to avoid clobbering a recovered lock.
        info = read_json(lock.path)
        if isinstance(info, dict) and int(info.get("pid", -1)) == lock.pid:
            lock.path.unlink(missing_ok=True)
    except OSError:
        pass


def read_lock(path: str | Path) -> dict | None:
    info = read_json(path)
    return info if isinstance(info, dict) else None
