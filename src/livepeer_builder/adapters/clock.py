"""System clock: the default Clock. Tests use livepeer_builder.testing.FixedClock."""

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
