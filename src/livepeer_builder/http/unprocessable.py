"""The 422 the engine's own API answers for a request it cannot read (#113, C3).

FastAPI's 422 repeats the input it refused. A lone surrogate in that input (JSON's ``"\\ud800"``) cannot be
sent as UTF-8, so the 422 itself became a 500. This handler sends FastAPI's own body with such a character
written as the escape it came in as. Every other 422 is byte for byte what FastAPI sends.

A ``NaN`` or an infinity in that input, which Python's JSON reader takes and JSON cannot write as a number,
made the 422 a 500 the same way (#158). It is written as a string of the token it came in as: ``"NaN"``,
``"Infinity"``, ``"-Infinity"``. The body stays JSON that every client reads.
"""

import json
import math
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

_TOKENS = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}  # Python's name for each: its JSON token


def _writable(value: Any) -> Any:
    """``value`` with each number JSON cannot write as a string of its token."""
    if isinstance(value, float) and not math.isfinite(value):
        return _TOKENS[str(value)]
    if isinstance(value, dict):
        return {key: _writable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_writable(item) for item in value]
    return value


def install_unprocessable_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _unprocessable(_: Request, exc: RequestValidationError) -> Response:
        detail = {"detail": _writable(jsonable_encoder(exc.errors()))}
        body = json.dumps(detail, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return Response(body.encode("utf-8", "backslashreplace"), 422, media_type="application/json")
