"""The ``Content-Type`` a runner answered with, as it sent it (#113, C27)."""

import re
from collections.abc import Iterable

# What a header value may hold (RFC 9110): a tab, a space, visible ASCII and the bytes 0x80 to 0xFF.
# aiohttp's parser lets other control characters through, and an HTTP server may refuse to send one.
_HEADER_VALUE = re.compile(rb"[\t\x20-\x7e\x80-\xff]+")


def content_type_as_sent(raw_headers: Iterable[tuple[bytes, bytes]]) -> str:
    """The runner's ``Content-Type`` as sent (#113, C27): the header's bytes read as Latin-1, as an HTTP
    server writes them back. aiohttp's ``response.headers`` reads them as UTF-8 and writes a byte that is
    not as a lone surrogate, which can be neither sent (a 500 after the job ran) nor read back once
    stored. ``application/octet-stream`` for no header, or for one that holds a control character."""
    for name, value in raw_headers:
        if name.lower() == b"content-type":
            return value.decode("latin-1") if _HEADER_VALUE.fullmatch(value) else "application/octet-stream"
    return "application/octet-stream"
