"""Tests for per-job progress/cancel registries and the upload size cap."""

import bot.handlers as h
from bot import progress as p
from bot.downloader import get_max_file_size_bytes, is_over_limit


def test_size_cap_defaults_to_2gb():
    assert get_max_file_size_bytes() == 2 * 1024**3
    assert is_over_limit(3 * 1024**3)
    assert not is_over_limit(100)
    assert not is_over_limit(0)
    assert not is_over_limit(None)


def test_size_cap_follows_env(monkeypatch):
    monkeypatch.setenv("MAX_FILE_SIZE_GB", "4")
    assert get_max_file_size_bytes() == 4 * 1024**3
    assert not is_over_limit(3 * 1024**3)
    assert is_over_limit(5 * 1024**3)


def test_size_cap_garbage_env_falls_back(monkeypatch):
    for bad in ("banana", "-1", "0", "nan", "inf"):
        monkeypatch.setenv("MAX_FILE_SIZE_GB", bad)
        assert get_max_file_size_bytes() == 2 * 1024**3


def test_progress_is_tracked_per_job():
    try:
        h._set_progress("download", "A", 10.0, 1.0, 10.0, job="job-a")
        h._set_progress("upload", "B", 50.0, 5.0, 10.0, job="job-b")
        prog_a = p.get_progress("job-a")
        prog_b = p.get_progress("job-b")
        assert prog_a is not None and prog_a["pct"] == 10.0
        assert prog_b is not None and prog_b["phase"] == "upload"
        # Legacy single global still mirrors the last write (/status unchanged).
        legacy = h._progress_state
        assert legacy is not None and legacy["phase"] == "upload"
    finally:
        h._clear_progress("job-a")
        h._clear_progress("job-b")
        h._clear_progress()
    assert p.get_progress("job-a") is None


def test_job_cancel_flags():
    try:
        assert not h._job_cancelled("job-x")
        p.request_cancel("job-x")
        assert h._job_cancelled("job-x")
    finally:
        p.clear_cancel("job-x")
    assert not h._job_cancelled("job-x")
