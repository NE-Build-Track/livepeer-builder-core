"""Path and query ids as the REST routes declare them (#113, C26).

Every id a route reads from its path or query string is stored text: at most 200 characters and no NUL,
as every record keeps it. A bad one is a 422 before any query runs. A NUL reached PostgreSQL, which
refuses it (a 500), and an id longer than any record holds was looked up, then answered 404.
``tests/structure/test_route_params.py`` fails when a route declares a plain ``str`` instead.
"""

from typing import Annotated

from fastapi import Path, Query

from livepeer_builder.contracts.common import StoredText

PathId = Annotated[StoredText, Path()]
QueryId = Annotated[StoredText | None, Query()]  # optional: absent means "not narrowed"
