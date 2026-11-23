"""The window a cost summary covers (docs/contracts/payments.md#cost-classification).

``[start, end)``, half-open. ``end`` defaults to the engine clock's now and ``start`` to 30 days before
``end``. An ``end`` in the first 30 days UTC holds has no such start: the subtraction raised
``OverflowError``, a 500 over REST (#158). It is refused as the service's other checks refuse a request,
with ``InvalidRequest`` (400), for the engine and for ``FakeEngine`` alike.

The window is measured from the instant ``end`` names, as the store reads it: in UTC, and an ``end`` with
no offset is UTC. Measured in ``end``'s own offset, ``0001-01-31T00:00+01:00`` passed and its start was a
time before year 1 in UTC, which overflowed where the window is normalized (review of #187).
"""

from datetime import UTC, datetime, timedelta

from livepeer_builder.errors import InvalidRequest

SUMMARY_WINDOW = timedelta(days=30)  # a summary with no start covers this long before its end


def summary_window(now: datetime, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """The summary's ``[start, end)``: ``end`` defaults to ``now``; ``start`` to ``SUMMARY_WINDOW`` before
    ``end``. The window is half-open, so a job created at ``now`` itself is not in it yet."""
    end = end or now
    if start is not None:
        return start, end
    # The first time UTC holds. An ``end`` is naive when it names no offset, whatever ``tzinfo`` it carries.
    first = datetime.min.replace(tzinfo=None if end.utcoffset() is None else UTC)
    since_first = end - first  # between two instants: no offset makes this overflow
    if since_first < SUMMARY_WINDOW:
        raise InvalidRequest("end: must be 0001-01-31T00:00:00Z or later when no start is given")
    try:
        return end - SUMMARY_WINDOW, end
    except OverflowError:  # behind UTC, the start's own wall time is before year 1: the same instant in UTC
        return first + (since_first - SUMMARY_WINDOW), end
