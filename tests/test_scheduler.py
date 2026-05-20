from datetime import datetime, timezone

from scheduler import NightlySchedule, should_run_nightly


def test_should_run_nightly_when_none_yet():
    now = datetime(2026, 4, 18, 7, 0, tzinfo=timezone.utc)
    assert should_run_nightly(now, None, NightlySchedule(hour=2, minute=0))


def test_should_not_run_before_target_time():
    now = datetime(2026, 4, 18, 1, 0, tzinfo=timezone.utc)
    assert not should_run_nightly(now, None, NightlySchedule(hour=2, minute=0))


def test_should_not_run_if_already_succeeded_after_target():
    now = datetime(2026, 4, 18, 7, 0, tzinfo=timezone.utc)
    last = datetime(2026, 4, 18, 6, 0, tzinfo=timezone.utc)
    assert not should_run_nightly(now, last, NightlySchedule(hour=2, minute=0))
