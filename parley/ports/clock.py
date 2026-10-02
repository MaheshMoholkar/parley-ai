"""The clock interface. Code asks the clock for the time instead of calling
`datetime.now()`, so tests can run a 30-day case in seconds with a fake clock."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC."""
        ...
