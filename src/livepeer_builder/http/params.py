"""Path and query ids, and query times, as the REST routes declare them (#113, C26).

Every id a route reads from its path or query string is stored text: at most 200 characters and no NUL,
as every record keeps it. A bad one is a 422 before any query runs. A NUL reached PostgreSQL, which
refuses it (a 500), and an id longer than any record holds was looked up, then answered 404.
``tests/structure/test_route_params.py`` fails when a route declares a plain ``str`` instead.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Path, Query
from pydantic import AfterValidator, AwareDatetime

from livepeer_builder.contracts.common import StoredText

PathId = Annotated[StoredText, Path()]
QueryId = Annotated[StoredText | None, Query()]  # optional: absent means "not narrowed"


def _in_utc(value: datetime) -> datetime:
    """The same time in UTC; one with no offset is read as UTC, as the store reads it. An offset can put
    a time outside the years a datetime holds (``0001-01-01T00:00+14:00`` is in the year before 1). The
    store converts every time it is handed, and met that one as an ``OverflowError``: a 500."""
    try:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("must be a time from year 1 to year 9999 in UTC") from error


# Every time a route reads from its query string, a 422 when UTC cannot hold it.
# ``tests/structure/test_route_params.py`` fails when a route declares a plain ``datetime`` instead.
QueryTime = Annotated[Annotated[datetime, AfterValidator(_in_utc)] | None, Query()]
QueryInstant = Annotated[Annotated[AwareDatetime, AfterValidator(_in_utc)] | None, Query()]  # offset required
