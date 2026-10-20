"""Streamed job responses (docs/contracts/engine.md#streaming, D37).

The response headers wait for the runner's first chunk, so any failure before it is an ordinary error
response (504 for a timeout, 502 for a runner error) with the job record, exactly like a buffered job.
After the first chunk, a failure aborts the connection: the body has no clean end, so a client or
proxy sees a cut-off transfer rather than a short but apparently complete body. The engine never
writes anything of its own into the stream, because it does not know the stream's format (D29).
"""

from collections.abc import AsyncGenerator

from fastapi import Response
from fastapi.responses import StreamingResponse
from starlette.types import Send

from livepeer_builder.errors import JobFailed
from livepeer_builder.runtime import JobStream


class _Aborted(Exception):
    """The job failed after the first byte: end the response without its closing message."""


class _AbortableStreamingResponse(StreamingResponse):
    body_iterator: AsyncGenerator[bytes, None]

    async def stream_response(self, send: Send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        try:
            async for chunk in self.body_iterator:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except _Aborted:
            return  # the server closes the connection; the job record says what happened
        finally:
            await self.body_iterator.aclose()  # a disconnect ends the job stream now, not at collection
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def stream_response(stream: JobStream, headers: dict[str, str]) -> Response:
    """Wait for the first chunk (JobFailed before it propagates), then stream the rest."""
    chunks = stream.__aiter__()
    try:
        first = await chunks.__anext__()
    except StopAsyncIteration:  # the runner answered with an empty body
        return Response(b"", status_code=stream.status_code, media_type=stream.content_type, headers=headers)

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            yield first
            async for chunk in chunks:
                yield chunk
        except JobFailed:
            raise _Aborted from None
        finally:
            await chunks.aclose()

    return _AbortableStreamingResponse(
        body(), status_code=stream.status_code, media_type=stream.content_type, headers=headers
    )
