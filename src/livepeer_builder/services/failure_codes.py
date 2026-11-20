"""Short, fixed codes for a background failure, as ``GET /readyz`` shows it (#113, C7).

``/readyz`` needs no credential, so ``discovery_error`` and ``cost_sync_error`` carry one of these codes
and never an exception's text, which can quote a URL with its query, a provider's own words, or a store
error's SQL and parameters. The text goes to the log, with the code.
"""

from typing import Final

from livepeer_builder.contracts import FailureCode, SourceFailureCode
from livepeer_builder.errors import ProviderError

UNAVAILABLE: Final = "unavailable"  # the source gave no answer, or answered with a status that is not 2xx
INVALID_ANSWER: Final = "invalid_answer"  # the source answered with something the engine could not read
STORE_ERROR: Final[FailureCode] = "store_error"  # cost sync failed after the usage source answered


def source_failure(error: BaseException) -> SourceFailureCode:
    """What a discovery source or a usage source raised, as a code.

    The shipped adapters say which it is where they read the answer: a ``ProviderError`` carries the
    answer's status, and only a 2xx is an answer the engine could not read. A redirect is no answer. The
    exception's type decides for the rest, which is all a supplied source has."""
    if isinstance(error, ProviderError):
        return INVALID_ANSWER if 200 <= error.status_code < 300 else UNAVAILABLE
    # JSON and parse errors are ValueErrors, and a number past its type's range is an ArithmeticError; a
    # timeout, a refused connection or an error status is none of these
    unreadable = isinstance(error, ValueError | KeyError | TypeError | ArithmeticError)
    return INVALID_ANSWER if unreadable else UNAVAILABLE
