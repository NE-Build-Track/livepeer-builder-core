"""The 422 the engine's own API answers for a request it cannot read (#113, C3).

FastAPI's 422 repeats the input it refused. A lone surrogate in that input (JSON's ``"\\ud800"``) cannot be
sent as UTF-8, so the 422 itself became a 500. This handler sends FastAPI's own body with such a character
written as the escape it came in as. Every other 422 is byte for byte what FastAPI sends.
"""

import json

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError


def install_unprocessable_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _unprocessable(_: Request, exc: RequestValidationError) -> Response:
        detail = {"detail": jsonable_encoder(exc.errors())}
        body = json.dumps(detail, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return Response(body.encode("utf-8", "backslashreplace"), 422, media_type="application/json")
