"""Short, fixed codes for a background failure, as ``GET /readyz`` shows it (#113, C7).

``/readyz`` needs no credential, so ``discovery_error`` and ``cost_sync_error`` carry one of these codes
and never an exception's text, which can quote a URL with its query, a provider's own words, or a store
error's SQL and parameters. The text goes to the log, with the code.
"""

from livepeer_builder.errors import ProviderError

UNAVAILABLE = "unavailable"  # the source gave no answer, or answered with an error status
INVALID_ANSWER = "invalid_answer"  # the source answered with something the engine could not read
STORE_ERROR = "store_error"  # cost sync failed after the usage source answered: the engine's store


def source_failure(error: BaseException) -> str:
    """What a discovery source or a usage source raised, as a code."""
    if isinstance(error, ProviderError):  # the adapter's status 200: an answer it could not read
        return UNAVAILABLE if error.status_code >= 400 else INVALID_ANSWER
    # JSON and parse errors are ValueErrors; a timeout, a refused connection or an error status is not
    return INVALID_ANSWER if isinstance(error, ValueError | KeyError | TypeError) else UNAVAILABLE
