"""Per-project locks so only one agent run uses a workspace at a time."""

from __future__ import annotations

import threading

_locks_guard = threading.Lock()
_project_locks: dict[str, threading.Lock] = {}


def _lock_for(project_id: str | int) -> threading.Lock:
    key = str(project_id)
    with _locks_guard:
        lock = _project_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _project_locks[key] = lock
        return lock


def try_acquire_project(project_id: str | int) -> bool:
    """Non-blocking acquire; returns False if another run holds the project."""
    return _lock_for(project_id).acquire(blocking=False)


def release_project(project_id: str | int) -> None:
    lock = _lock_for(project_id)
    if lock.locked():
        lock.release()
