"""The default adapter of each payment port (D84, #168): Batteries for self-hosted payments, or a
payment clearinghouse, and the signer either way.

``default_parts`` asks here for each payment part the caller did not supply, so a supplied part is never
built. The engine's role chooses (``LPB_ROLE``, #169): in its ``work`` role ``AllocationManager``,
``AllowanceReader`` and ``UsageSource`` reach the clearinghouse that ``LPB_CLEARINGHOUSE_URL`` names (with
the tenant admin key and the tenant's id), and nothing of Batteries is built; in its ``both`` and its
``clearinghouse`` role they reach Batteries. The settings refuse a role without what it needs, and a mix
of the two (``config.py``).

**Signing and the key** are the engine's own either way (D85 point 1 and D87 point 1, as amended). The
runner transport is the gateway SDK's, at ``LPB_SIGNER_URL``: the engine's own signer, or the one a
clearinghouse publishes. The credential is the actor's allocation key from the engine's own secret
store: minted by its Batteries, or handed over once by the clearinghouse at allotment and rotation. So
``transport`` and ``credentials`` are one answer for both configurations (``_OwnSigning``), and nothing
of a clearinghouse's engine is in the signing path. An engine in its ``clearinghouse`` role runs no job,
so its transport cannot pay at all: it asks the signer's identity, for the wallet report, and no more.

The adapters are imported inside the methods, not at module level, so importing the package loads no
HTTP client or SDK (tests/structure/test_import_weight.py).
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from livepeer_builder.config import EngineSettings
from livepeer_builder.protocols import (
    ActorRepo,
    AllocationManager,
    AllowanceReader,
    CredentialResolver,
    RunnerTransport,
    SecretStore,
    UsageSource,
)

if TYPE_CHECKING:
    from livepeer_builder.adapters.clearinghouse import ClearinghousePorts

Keep = Callable[[object, Callable[[], Awaitable[None]]], None]
"""Registers a resource the engine opened, with how to close it (``assembly.own`` on the engine's stack)."""


class PaymentDefaults(Protocol):
    """What each payment part is when the caller supplies none. ``None`` is a value: the part is off, or,
    for ``credentials`` and ``allowances``, the engine answers from its own records."""

    def allocation_manager(self) -> AllocationManager | None: ...
    def grants_manager(self) -> AllocationManager | None: ...
    def usage_source(self) -> UsageSource | None: ...
    def credentials(self) -> CredentialResolver | None: ...
    def allowances(self) -> AllowanceReader | None: ...
    def transport(self) -> RunnerTransport: ...
    async def confirm_tenant(self) -> None:
        """Know that the tenant admin key the engine holds is its own tenant's, or raise: asked before a
        read that carries the key and whose answer states no tenant (the clearinghouse's runner list)."""


class _OwnSigning:
    """The two ports no clearinghouse serves: the engine signs at ``LPB_SIGNER_URL`` itself, and resolves
    each actor's key from its own records and secret store (``None``: ``PaymentsService``)."""

    def __init__(self, settings: EngineSettings) -> None:
        self._settings = settings

    def credentials(self) -> CredentialResolver | None:
        return None

    def transport(self) -> RunnerTransport:
        from livepeer_builder.adapters.sdk import SdkRunnerTransport

        return SdkRunnerTransport(self._settings.signer_url, self._settings.max_payment_challenge_retries)


class SelfHosted(_OwnSigning):
    """An engine in its ``both`` or its ``clearinghouse`` role: Batteries' management API and usage feed,
    as the settings name them."""

    def __init__(self, settings: EngineSettings, keep: Keep) -> None:
        super().__init__(settings)
        self._keep = keep

    def transport(self) -> RunnerTransport:
        """An engine in its ``clearinghouse`` role runs no job and signs nothing (D82), so it is given no
        transport that could pay: only the signer's identity call, which its wallet report makes."""
        if self._settings.runs_jobs:
            return super().transport()
        from livepeer_builder.adapters.sdk import IdentityOnlyTransport

        return IdentityOnlyTransport(self._settings.signer_url)

    def allocation_manager(self) -> AllocationManager | None:
        token = self._settings.batteries_token
        return self._batteries(token.get_secret_value() if token else None)

    def grants_manager(self) -> AllocationManager | None:
        """Off without the grants credential: the engine's own cannot list or create grants."""
        token = self._settings.batteries_grants_token
        return self._batteries(token.get_secret_value() if token else None)

    def usage_source(self) -> UsageSource | None:
        from livepeer_builder.adapters.batteries import BatteriesUsageSource

        settings = self._settings
        if not settings.batteries_url or not settings.batteries_token:
            return None
        usage = BatteriesUsageSource(
            settings.batteries_url,
            settings.batteries_token.get_secret_value(),
            timeout_s=settings.batteries_timeout_s,
        )
        self._keep(usage, usage.aclose)
        return usage

    def allowances(self) -> AllowanceReader | None:
        return None

    async def confirm_tenant(self) -> None:  # NOSONAR - async by protocol (PaymentDefaults)
        """It holds no tenant admin key: there is nothing to know."""

    def _batteries(self, token: str | None) -> AllocationManager | None:
        from livepeer_builder.adapters.batteries import BatteriesClient

        if not self._settings.batteries_url or not token:
            return None
        client = BatteriesClient(
            self._settings.batteries_url, token, timeout_s=self._settings.batteries_timeout_s
        )
        self._keep(client, client.aclose)
        return client


class ThroughClearinghouse(_OwnSigning):
    """An engine in its ``work`` role: the remote adapters, over one wire to the clearinghouse, opened
    when the first is asked for.
    ``actors`` and ``secrets`` are the engine's own: where it keeps each actor's allocation record and
    the key the clearinghouse handed over for it."""

    def __init__(self, settings: EngineSettings, keep: Keep, actors: ActorRepo, secrets: SecretStore) -> None:
        super().__init__(settings)
        self._keep = keep
        self._actors = actors
        self._secrets = secrets
        self._built: ClearinghousePorts | None = None

    def allocation_manager(self) -> AllocationManager | None:
        return self._ports().allocations

    def grants_manager(self) -> AllocationManager | None:
        """The same manager: it reads the tenant's own grant, and refuses what only a clearinghouse
        admin does."""
        return self._ports().allocations

    def usage_source(self) -> UsageSource | None:
        return self._ports().usage

    def allowances(self) -> AllowanceReader | None:
        return self._ports().allowances

    async def confirm_tenant(self) -> None:
        """The same answer the three adapters go by, read once for all of them."""
        await self._ports().tenant.confirm()

    def _ports(self) -> "ClearinghousePorts":
        from livepeer_builder.adapters.clearinghouse import ClearinghousePorts
        from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire

        if self._built is None:
            settings = self._settings
            token = settings.clearinghouse_token
            if not settings.clearinghouse_url or token is None or not settings.clearinghouse_tenant_id:
                raise ValueError("the clearinghouse settings are set together")  # config.py refuses a half
            wire = ClearinghouseWire(
                settings.clearinghouse_url,
                token.get_secret_value(),
                settings.clearinghouse_tenant_id,
                timeout_s=settings.clearinghouse_timeout_s,
            )
            self._keep(wire, wire.aclose)
            self._built = ClearinghousePorts.over(wire, self._actors, self._secrets)
        return self._built


def payment_defaults(
    settings: EngineSettings, keep: Keep, actors: ActorRepo, secrets: SecretStore
) -> PaymentDefaults:
    """By the role alone: ``work`` reaches a clearinghouse, and the other two roles Batteries
    (tests/structure/test_two_halves.py)."""
    if settings.uses_clearinghouse:
        return ThroughClearinghouse(settings, keep, actors, secrets)
    return SelfHosted(settings, keep)
