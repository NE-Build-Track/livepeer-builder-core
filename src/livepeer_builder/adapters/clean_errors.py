"""Errors that lead to no credential (#168 review).

A request that carries a credential, and an answer that hands one over, are locals of the function that
sends the request and reads the answer. An error raised there keeps both within reach of anything that
walks its traceback's frames, and one raised while the HTTP client's own error is being handled chains
that error, which holds the request. So such a function builds its error and *returns* it, and the
caller passes the outcome to ``raised``, a frame whose only local is the error itself. ``bare`` makes
the error to return from one the HTTP client raised: its kind and its words, and nothing it held.

Used by the wire to a payment clearinghouse and by the discovery reader, which both send the tenant
admin key.
"""


def raised[T](outcome: T | Exception) -> T:
    """``outcome``, unless it is the error a request or an answer came to, which is raised here: from a
    frame whose only local is the error itself."""
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def bare(error: BaseException, words: str | None = None) -> Exception:
    """A new error of ``error``'s kind with its words (or ``words``) and nothing else of it: no request,
    no answer, no traceback and no chain. A kind that cannot be built from its words alone is a
    ``RuntimeError`` that names it."""
    text = str(error) if words is None else words
    try:
        fresh = type(error)(text)
    except Exception:  # a kind whose constructor takes more than its words
        fresh = None
    if not isinstance(fresh, Exception):
        return RuntimeError(f"{type(error).__name__}: {text}")
    return fresh
