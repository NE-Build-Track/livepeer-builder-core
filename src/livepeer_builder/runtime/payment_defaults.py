"""The default adapter of each payment port (D84, #168): Batteries and the signer for self-hosted
payments, or a payment clearinghouse.

``default_parts`` asks here for each payment part the caller did not supply, so a supplied part is never
built. ``LPB_CLEARINGHOUSE_URL`` (with the tenant admin key and the tenant's id) selects the
clearinghouse: every payment port then reaches it, and nothing of Batteries or a signer is built. The
settings refuse a mix of the two (``config.py``).

**Signing** is inside the runner transport, and it is the same transport either way: the gateway SDK's.
A clearinghouse serves the signer's two paths at the root of its listener (D87), so an engine that uses
one gives the transport the clearinghouse's address where a signer's goes, and the actor's payment key,
which ``CredentialResolver`` returns, is the credential it signs with. The SDK is unmodified.

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


class SelfHosted:
    """Batteries' management API and usage feed, and the signer, as the settings name them."""

    def __init__(self, settings: EngineSettings, keep: Keep) -> None:
        self._settings = settings
        self._keep = keep

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

    def credentials(self) -> CredentialResolver | None:
        return None

    def allowances(self) -> AllowanceReader | None:
        return None

    def transport(self) -> RunnerTransport:
        from livepeer_builder.adapters.sdk import SdkRunnerTransport

        return SdkRunnerTransport(self._settings.signer_url, self._settings.max_payment_challenge_retries)

    def _batteries(self, token: str | None) -> AllocationManager | None:
        from livepeer_builder.adapters.batteries import BatteriesClient

        if not self._settings.batteries_url or not token:
            return None
        client = BatteriesClient(
            self._settings.batteries_url, token, timeout_s=self._settings.batteries_timeout_s
        )
        self._keep(client, client.aclose)
        return client


class ThroughClearinghouse:
    """The remote adapters, over one wire to the clearinghouse, opened when the first is asked for.
    ``actors`` and ``secrets`` are the engine's own: where it keeps each actor's allocation record and
    the payment key the clearinghouse issued for it."""

    def __init__(self, settings: EngineSettings, keep: Keep, actors: ActorRepo, secrets: SecretStore) -> None:
        self._settings = settings
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

    def credentials(self) -> CredentialResolver | None:
        return self._ports().credentials

    def allowances(self) -> AllowanceReader | None:
        return self._ports().allowances

    def transport(self) -> RunnerTransport:
        """The SDK's transport, signing at the clearinghouse: its address is the signer's address. The
        credential of each call is the actor's payment key, and never the tenant admin key, which this
        transport is not given."""
        from livepeer_builder.adapters.sdk import SdkRunnerTransport

        return SdkRunnerTransport(
            self._settings.clearinghouse_url, self._settings.max_payment_challenge_retries
        )

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
    if settings.uses_clearinghouse:
        return ThroughClearinghouse(settings, keep, actors, secrets)
    return SelfHosted(settings, keep)
