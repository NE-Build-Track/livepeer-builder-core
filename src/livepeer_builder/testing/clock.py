"""``FixedClock``: the clock a test moves by hand. It is here, not in ``fakes.py``, which is at its size
limit."""

from datetime import UTC, datetime, timedelta


class FixedClock:
    """A controllable clock. ``advance`` moves it forward."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
