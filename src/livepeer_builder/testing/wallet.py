"""What the fakes answer for the wallet report (#167): the provider's escrow and the signer's readiness.

Two small parts, each mixed into the fake that plays its side: ``FakeAllocationManager`` reports the
escrow accounts a test gives it (``EscrowSource``), and ``ScriptedTransport`` the readiness a test gives
it (``SignerProbe``). They are here, not in ``fakes.py``, which is at its size limit.
"""

from collections.abc import Sequence

from livepeer_builder.contracts import EscrowAccount, SignerReadiness


class FakeEscrow:
    """The escrow report of the fake provider: ``escrow`` is what it reports, and nothing unless a test
    sets it, as a Batteries whose on-chain listener is off."""

    escrow: Sequence[EscrowAccount] = ()

    def _called(self, call: str) -> None:
        """The fake provider's own, which replaces this: it records the call and raises what a test
        scripted for it (``fail_once``)."""

    async def escrow_accounts(self) -> tuple[EscrowAccount, ...]:  # NOSONAR - async by protocol
        self._called("escrow_accounts")
        return tuple(self.escrow)


class FakeSignerProbe:
    """The signer the fake transport pays through: ready, unless a test sets ``signer``."""

    signer: SignerReadiness = SignerReadiness(ready=True)

    async def signer_readiness(self) -> SignerReadiness:  # NOSONAR - async by protocol
        return self.signer
