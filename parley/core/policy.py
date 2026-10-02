"""Per-tenant collection policy, and the rules for when a customer may be contacted.

A tenant stores only the settings it changes; everything else uses the defaults
below. Pydantic checks the values when they are loaded, so a bad setting fails
loudly instead of producing odd behaviour later.
"""

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

CONTACT_WINDOW = timedelta(days=7)  # the "week" in max_contacts_per_week


class QuietHours(BaseModel):
    """Times when no outbound contact is allowed, in the tenant's timezone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: time = time(20, 0)
    end: time = time(9, 0)
    # Whole days with no contact. Monday is 0 and Sunday is 6.
    days: frozenset[int] = frozenset({6})

    def is_quiet(self, local: datetime) -> bool:
        if local.weekday() in self.days:
            return True
        now = local.time()
        if self.start > self.end:  # window crosses midnight, e.g. 20:00 to 09:00
            return now >= self.start or now < self.end
        return self.start <= now < self.end


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    first_reminder_delay_days: int = Field(default=3, ge=0)
    reminder_gap_days: int = Field(default=5, ge=1)
    max_reminders: int = Field(default=4, ge=1)
    max_contacts_per_week: int = Field(default=2, ge=1)
    quiet_hours: QuietHours = QuietHours()
    promise_grace_days: int = Field(default=1, ge=0)
    max_promise_window_days: int = Field(default=30, ge=1)
    approval_mode: Literal["all", "above_threshold", "none"] = "all"
    approval_threshold: int | None = Field(default=None, ge=0)  # minor units
    tone_steps: tuple[str, ...] = Field(default=("friendly", "firm", "final"), min_length=1)

    def needs_approval(self, total_amount: int) -> bool:
        """Must a person approve this message before it is sent?"""
        if self.approval_mode == "all":
            return True
        if self.approval_mode == "above_threshold":
            return self.approval_threshold is None or total_amount > self.approval_threshold
        return False

    def tone_for(self, reminder_number: int) -> str:
        """Reminder 1 uses the first tone, 2 the second, and so on; the last tone repeats."""
        index = min(max(reminder_number, 1), len(self.tone_steps)) - 1
        return self.tone_steps[index]


def next_time_outside_quiet_hours(moment: datetime, tz: ZoneInfo, quiet: QuietHours) -> datetime:
    """Return `moment` itself if contact is allowed then, otherwise the next allowed time."""
    local = moment.astimezone(tz)
    # Each step jumps to the end of a quiet period, so a week plus one day of
    # steps is always enough unless every day is quiet.
    for _ in range(9):
        if not quiet.is_quiet(local):
            return local.astimezone(moment.tzinfo)
        if local.weekday() in quiet.days:
            next_day = local.date() + timedelta(days=1)
            local = datetime.combine(next_day, quiet.end, tzinfo=tz)
        else:
            end_today = datetime.combine(local.date(), quiet.end, tzinfo=tz)
            local = end_today if end_today > local else end_today + timedelta(days=1)
    raise ValueError("quiet hours leave no time for contact")


def contact_allowed_at(
    now: datetime,
    tz: ZoneInfo,
    policy: Policy,
    recent_contacts: Sequence[datetime],
) -> datetime:
    """The earliest time a new message may go to a customer.

    `recent_contacts` are the times of outbound messages to this customer in the
    last seven days. If the weekly cap is used up, contact waits until the oldest
    of those leaves the window. The result is then pushed out of quiet hours.
    """
    in_window = sorted(t for t in recent_contacts if t > now - CONTACT_WINDOW)
    earliest = now
    over_cap = len(in_window) - policy.max_contacts_per_week
    if over_cap >= 0:
        # The message that has to age out before one more may be sent.
        earliest = max(now, in_window[over_cap] + CONTACT_WINDOW)
    return next_time_outside_quiet_hours(earliest, tz, policy.quiet_hours)
