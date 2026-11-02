"""Where one runner call's time goes before the runner answers (#89)."""

import logging
import time

from livepeer_builder.contracts import CallTimings

_LOG = logging.getLogger(__name__)


class CallTimer:
    """Adds up each step of one call, including repeats after a refreshed challenge."""

    def __init__(self) -> None:
        self._spent: dict[str, float] = {}

    def start(self) -> float:
        return time.monotonic()

    def spent(self, step: str, started: float) -> None:
        self._spent[step] = self._spent.get(step, 0.0) + time.monotonic() - started

    def _ms(self, step: str) -> int | None:
        return round(self._spent[step] * 1000) if step in self._spent else None

    @property
    def timings(self) -> CallTimings:
        return CallTimings(
            challenge_ms=self._ms("challenge"),
            signing_ms=self._ms("signing"),
            response_ms=self._ms("response"),
        )

    def log(self, manifest_ids: tuple[str, ...], paid: bool) -> None:
        _LOG.info(
            "runner call timed",
            extra={
                "event": "runner.call_timings",
                **self.timings.model_dump(),
                "manifest_ids": manifest_ids,
                "paid": paid,
            },
        )
