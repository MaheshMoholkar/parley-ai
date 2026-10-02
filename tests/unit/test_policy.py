from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from parley.core.policy import Policy, QuietHours, contact_allowed_at, next_time_outside_quiet_hours

IST = ZoneInfo("Asia/Kolkata")
POLICY = Policy()


def ist(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


# 2026-01-05 is a Monday; 2026-01-11 is a Sunday.


def test_daytime_is_allowed() -> None:
    moment = ist(2026, 1, 5, 10, 0)
    assert next_time_outside_quiet_hours(moment, IST, POLICY.quiet_hours) == moment


def test_late_evening_moves_to_next_morning() -> None:
    result = next_time_outside_quiet_hours(ist(2026, 1, 5, 21, 30), IST, POLICY.quiet_hours)
    assert result == ist(2026, 1, 6, 9, 0)


def test_early_morning_moves_to_nine() -> None:
    result = next_time_outside_quiet_hours(ist(2026, 1, 6, 7, 0), IST, POLICY.quiet_hours)
    assert result == ist(2026, 1, 6, 9, 0)


def test_saturday_evening_skips_sunday() -> None:
    result = next_time_outside_quiet_hours(ist(2026, 1, 10, 20, 0), IST, POLICY.quiet_hours)
    assert result == ist(2026, 1, 12, 9, 0)


def test_result_keeps_the_input_timezone() -> None:
    moment = ist(2026, 1, 11, 12, 0).astimezone(UTC)
    result = next_time_outside_quiet_hours(moment, IST, POLICY.quiet_hours)
    assert result.tzinfo == UTC
    assert result == ist(2026, 1, 12, 9, 0)


def test_daytime_quiet_window() -> None:
    lunch = QuietHours(start=time(13, 0), end=time(14, 0), days=frozenset())
    assert next_time_outside_quiet_hours(ist(2026, 1, 5, 13, 30), IST, lunch) == ist(
        2026, 1, 5, 14, 0
    )


def test_weekly_cap_allows_when_under_the_cap() -> None:
    now = ist(2026, 1, 7, 10, 0)
    assert contact_allowed_at(now, IST, POLICY, [now - timedelta(days=2)]) == now


def test_weekly_cap_waits_for_the_oldest_contact_to_age_out() -> None:
    now = ist(2026, 1, 8, 10, 0)
    recent = [ist(2026, 1, 5, 11, 0), ist(2026, 1, 7, 10, 0)]
    assert contact_allowed_at(now, IST, POLICY, recent) == ist(2026, 1, 12, 11, 0)


def test_weekly_cap_result_respects_quiet_hours() -> None:
    now = ist(2026, 1, 8, 10, 0)
    recent = [ist(2026, 1, 3, 22, 0), ist(2026, 1, 7, 10, 0)]  # ages out Saturday 22:00
    assert contact_allowed_at(now, IST, POLICY, recent) == ist(2026, 1, 12, 9, 0)


def test_contacts_older_than_a_week_do_not_count() -> None:
    now = ist(2026, 1, 15, 10, 0)
    recent = [ist(2026, 1, 5, 10, 0), ist(2026, 1, 6, 10, 0)]
    assert contact_allowed_at(now, IST, POLICY, recent) == now


def test_tone_steps() -> None:
    assert [POLICY.tone_for(n) for n in (1, 2, 3, 4)] == ["friendly", "firm", "final", "final"]


def test_bad_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Policy.model_validate({"max_reminders": 0})
    with pytest.raises(ValidationError):
        Policy.model_validate({"no_such_setting": 1})


def test_partial_overrides_keep_defaults() -> None:
    policy = Policy.model_validate({"reminder_gap_days": 7})
    assert policy.reminder_gap_days == 7
    assert policy.max_reminders == 4
