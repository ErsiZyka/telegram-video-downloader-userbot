"""Per-job progress and cancellation registries.

The worker handles one item at a time, but progress/cancel state is keyed by
job (the item URL) so a second consumer (local API, future website, parallel
workers) never overwrites or cancels the wrong job.

The legacy single-job mirrors (``_progress_state`` / ``_cancel_requested`` in
``bot.handlers``) are kept as views of the *current* job so the existing
Telegram commands (``/status``, ``/stop``) behave exactly as before.
"""

from __future__ import annotations

import time

_progress_by_job: dict[str, dict] = {}
_cancel_flags: set[str] = set()
_current_job: str | None = None


def set_current_job(job: str | None) -> None:
    """Mark which job the worker is currently processing (None = idle)."""
    global _current_job
    _current_job = job


def get_current_job() -> str | None:
    return _current_job


def set_progress(
    job: str | None,
    phase: str,
    title: str | None,
    pct: float,
    received: float,
    total: float,
    speed: float = 0.0,
    eta: float = 0.0,
) -> dict:
    """Record progress for one job. ``job=None`` falls back to current job."""
    state = {
        "phase": phase,
        "title": title,
        "pct": pct,
        "received": received,
        "total": total,
        "speed": speed,
        "eta": eta,
        "ts": time.time(),
    }
    key = job or _current_job
    if key:
        _progress_by_job[key] = state
    return state


def get_progress(job: str) -> dict | None:
    return _progress_by_job.get(job)


def all_progress(limit: int = 50) -> dict[str, dict]:
    """Snapshot of tracked jobs (oldest first, capped)."""
    items = list(_progress_by_job.items())[-limit:]
    return dict(items)


def clear_progress(job: str | None = None) -> None:
    key = job or _current_job
    if key:
        _progress_by_job.pop(key, None)


def request_cancel(job: str) -> None:
    """Ask for cancellation of one job (checked cooperatively by workers)."""
    _cancel_flags.add(job)


def is_cancelled(job: str | None) -> bool:
    return bool(job) and job in _cancel_flags


def clear_cancel(job: str | None) -> None:
    if job:
        _cancel_flags.discard(job)


def reset() -> None:
    """Clear all registries (test isolation only)."""
    _progress_by_job.clear()
    _cancel_flags.clear()
    set_current_job(None)
