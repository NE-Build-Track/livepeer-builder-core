"""The request-size limit on the engine's own API (D59).

FastAPI reads a whole body into memory before it validates anything, so the limit sits in front of the
app: a declared ``Content-Length`` over it is refused at once, and a body streamed without one is cut off
when the bytes received pass it. Only ``/v1/`` is limited; routes an operator adds elsewhere, and a
persistent endpoint's proxy, carry traffic whose limits are their own.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024  # an 8 MiB raw_body is about 10.7 MiB as base64 in JSON


class RequestTooLarge(HTTPException):
    """Raised while the body is read. FastAPI turns any other error there into a 400, but re-raises an
    HTTPException, so the engine's handler answers it."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"the request body exceeds {limit} bytes")
        self.limit = limit


def refusal(exc: RequestTooLarge) -> JSONResponse:
    return JSONResponse(
        {"error": exc.detail, "code": "request_too_large", "limit_bytes": exc.limit}, status_code=413
    )


class RequestSizeLimit:
    def __init__(self, app: ASGIApp, *, max_bytes: int, prefix: str = "/v1/") -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await refusal(RequestTooLarge(self.max_bytes))(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestTooLarge(self.max_bytes)
            return message

        await self.app(scope, limited, send)


def install_request_limit(app: FastAPI, max_bytes: int) -> None:
    async def too_large(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestTooLarge)
        return refusal(exc)

    app.add_exception_handler(RequestTooLarge, too_large)
    app.add_middleware(RequestSizeLimit, max_bytes=max_bytes)
