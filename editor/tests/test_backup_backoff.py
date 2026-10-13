"""Auto-backup failure backoff.

Orgs without a backup passcode fail every attempt with "not configured". The
scheduler must NOT re-attempt (and re-warn) every 60s loop tick — that flooded
prod logs and burned CPU once a minute forever. On failure the loop records a
`last_check` that defers the next attempt, and `_backoff_last_check` computes
that timestamp. These tests pin its arithmetic (the part that decides how soon
a failed org is retried) without needing a running scheduler.
"""
from datetime import datetime, timedelta

import pytest

import org_backup


def _elapsed_until_due(interval_hours):
    """Given a fresh failure at t0, return how long after t0 the org next
    becomes 'due' again — mirroring the loop's own due check
    `(now - last)/3600 >= interval_hours`."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    last = org_backup._backoff_last_check(now, interval_hours)
    # The loop treats an org as due when (t - last) >= interval_hours.
    due_at = last + timedelta(hours=interval_hours)
    return (due_at - now).total_seconds() / 3600.0


def test_default_interval_backs_off_by_one_hour():
    # 24h interval → a failed org retries ~1h later, not every 60s.
    assert _elapsed_until_due(24) == pytest.approx(org_backup._BACKUP_FAIL_BACKOFF_HOURS)


def test_backoff_never_exceeds_interval():
    # A sub-backoff interval must not push the retry into the future / past
    # the interval (which would strand the org forever).
    for interval in (0.25, 0.5, 1, 6, 24):
        elapsed = _elapsed_until_due(interval)
        assert 0 < elapsed <= interval + 1e-9


def test_sub_hour_interval_retries_within_interval():
    # 30-min interval, 1h backoff clamp → next retry at the full 30-min mark.
    assert _elapsed_until_due(0.5) == pytest.approx(0.5)
