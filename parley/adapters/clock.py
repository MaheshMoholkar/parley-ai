"""Clock implementations: the real one, and a fake one for tests and simulations."""

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, moment: datetime) -> None:
        self._now = _require_aware(moment)


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("clock times must be timezone-aware")
    return moment.astimezone(UTC)
