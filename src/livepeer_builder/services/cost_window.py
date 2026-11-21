"""The window a cost summary covers (docs/contracts/payments.md#cost-classification).

``[start, end)``, half-open. ``end`` defaults to the engine clock's now and ``start`` to 30 days before
``end``. An ``end`` in the first 30 days a datetime holds has no such start: the subtraction raised
``OverflowError``, a 500 over REST (#158). It is refused as the service's other checks refuse a request,
with ``InvalidRequest`` (400), for the engine and for ``FakeEngine`` alike.
"""

from datetime import datetime, timedelta

from livepeer_builder.errors import InvalidRequest

SUMMARY_WINDOW = timedelta(days=30)  # a summary with no start covers this long before its end


def summary_window(now: datetime, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """The summary's ``[start, end)``: ``end`` defaults to ``now``; ``start`` to ``SUMMARY_WINDOW`` before
    ``end``. The window is half-open, so a job created at ``now`` itself is not in it yet."""
    end = end or now
    if start is not None:
        return start, end
    first = datetime.min.replace(tzinfo=end.tzinfo)  # the first time a datetime holds, at ``end``'s offset
    if end - first < SUMMARY_WINDOW:
        raise InvalidRequest("end: must be 0001-01-31T00:00:00 or later when no start is given")
    return end - SUMMARY_WINDOW, end
