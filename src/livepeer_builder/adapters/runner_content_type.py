"""A runner's response headers as it sent them: the ``Content-Type``, and every pair (#113, C27)."""

from collections.abc import Iterable

from livepeer_builder.contracts.http import OCTET_STREAM, response_content_type


def content_type_as_sent(raw_headers: Iterable[tuple[bytes, bytes]]) -> str:
    """The runner's ``Content-Type`` as sent (#113, C27): the header's bytes read as Latin-1, as an HTTP
    server writes them back. aiohttp's ``response.headers`` reads them as UTF-8 and writes a byte that is
    not as a lone surrogate, which can be neither sent (a 500 after the job ran) nor read back once
    stored. ``application/octet-stream`` for no header, or for one that holds a control character:
    aiohttp's parser lets those through, and an HTTP server may refuse to send one. What a header value
    may hold is one rule for every transport (``contracts.http.response_content_type``)."""
    for name, value in raw_headers:
        if name.lower() == b"content-type":
            return response_content_type(value.decode("latin-1"))
    return OCTET_STREAM


def headers_as_sent(raw_headers: Iterable[tuple[bytes, bytes]]) -> tuple[tuple[str, str], ...]:
    """Every header pair in the order sent, repeated names included, the bytes read as Latin-1 for the
    reason ``content_type_as_sent`` gives: no value holds a lone surrogate, so a caller can log, store or
    send each one on. Nothing is left out or replaced. The engine sends none of these on itself, and a
    control character is the application's to filter, as every header here is."""
    return tuple((name.decode("latin-1"), value.decode("latin-1")) for name, value in raw_headers)
