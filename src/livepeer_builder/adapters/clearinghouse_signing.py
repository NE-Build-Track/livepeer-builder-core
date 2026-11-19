"""The signing seam of an engine that uses a payment clearinghouse (D84, #168).

Signing is the fifth payment port, and it has no protocol of its own: it happens inside the runner
transport. With a clearinghouse, the engine will ask the clearinghouse to sign each payment with the
actor's payment key. **That is not built yet**: the payments route's path is not decided (#166).

Until it is, an engine that uses a clearinghouse runs ``ClearinghouseSigningNotBuilt`` as its runner
transport. It holds no signer address, so it cannot sign and cannot charge:

- a runner that asks for no payment is called, and its answer returned;
- a runner that asks for payment (HTTP 402) fails the call as a ``payment`` failure with nothing paid,
  in plain words. The job fails ``payment`` and is not retried on another runner.

The actor's payment key is never sent anywhere from here. There is no fallback to a local signer: an
engine that uses a clearinghouse is refused ``LPB_SIGNER_URL`` when it starts (``config.py``).

**To build signing**, replace this class where ``runtime/payment_defaults.py`` builds it, and nowhere
else: with the SDK's transport pointed at the clearinghouse, if the clearinghouse serves the signer's
two routes, or with a transport that makes the two signer calls itself, if the payments route keeps its
own path.
"""

import contextlib
from collections.abc import Iterator

from livepeer_builder.adapters.runner_call import SdkRunnerStream
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.contracts import JobRequest, Runner, RunnerReply
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.protocols import OnSigned

SIGNING_NOT_BUILT = (
    "the runner asks for payment, and this engine pays through a payment clearinghouse, where signing a "
    "payment is not built yet (#168): nothing was signed, and nothing was sent to the clearinghouse"
)


class ClearinghouseSigningNotBuilt:
    """A runner transport that never pays. ``credential`` is the actor's payment key, and is not used."""

    pays = False  # the engine keeps the selection policy's order: no call here is paid

    def __init__(self, max_payment_challenge_retries: int = 3) -> None:
        self._unpaid = SdkRunnerTransport(None, max_payment_challenge_retries)  # None: no signer to ask

    async def call(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> RunnerReply:
        del credential
        with _payment_asked_for():
            return await self._unpaid.call(runner, request, None, on_signed)

    async def open(
        self, runner: Runner, request: JobRequest, credential: str | None, on_signed: OnSigned | None = None
    ) -> SdkRunnerStream:
        del credential
        with _payment_asked_for():
            return await self._unpaid.open(runner, request, None, on_signed)


@contextlib.contextmanager
def _payment_asked_for() -> Iterator[None]:
    """The runner's 402 as the plain failure: the unpaid call's own error says the call is unpaid, which
    is true of any engine with no signer, and fails over to the next runner, which would ask the same."""
    try:
        yield
    except RunnerCallError as error:
        if error.status_code != 402 or error.payment_sent:
            raise
        raise RunnerCallError(
            "payment", SIGNING_NOT_BUILT, payment_sent=False, status_code=402, timings=error.timings
        ) from error
