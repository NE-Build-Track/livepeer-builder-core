"""#168: ``LPB_CLEARINGHOUSE_URL`` and its two companions select the remote adapters (D84).

With them, the three payment ports that reach a clearinghouse (``AllocationManager``,
``AllowanceReader``, ``UsageSource``) are the remote adapters, and nothing of Batteries is built. The
other two stay the engine's own, as with self-hosted payments: it keeps each actor's key in its own
secret store, and it signs at the signer ``LPB_SIGNER_URL`` names, which is the clearinghouse's (D85
point 1, as amended). Without the settings the engine is built as before. A supplied part is never
replaced.
"""

import pytest

from livepeer_builder import BuilderEngine, EngineSettings, EngineSupply
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.adapters.clearinghouse import (
    ClearinghouseAllocations,
    ClearinghouseAllowances,
    ClearinghouseUsage,
)
from livepeer_builder.adapters.sdk import SdkRunnerTransport
from livepeer_builder.runtime.payment_defaults import ThroughClearinghouse
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import (
    FakeAllocationManager,
    FakeUsageSource,
    MemorySecretStore,
    ScriptedTransport,
)
from tests.unit.clearinghouse_engine import TOKEN, parts_of, self_hosted, with_clearinghouse

# -- which adapters ---------------------------------------------------------------------------------------


SIGNER = "http://127.0.0.1:8935"


async def test_the_clearinghouse_settings_point_three_payment_ports_at_the_clearinghouse() -> None:
    async with parts_of(with_clearinghouse()) as parts:
        assert isinstance(parts.allocation_manager, ClearinghouseAllocations)
        assert parts.grants_manager is parts.allocation_manager  # it reads the grant, and refuses the rest
        assert isinstance(parts.usage_source, ClearinghouseUsage)
        assert isinstance(parts.allowances, ClearinghouseAllowances)
        assert TOKEN not in repr(parts)


async def test_the_engine_keeps_its_actors_keys_itself_and_signs_at_the_signer() -> None:
    """The two ports no clearinghouse serves. ``CredentialResolver`` is the engine's own: the key it was
    handed, from its own secret store. Signing is the SDK's transport at ``LPB_SIGNER_URL``, exactly as
    a self-hosted engine's, and the clearinghouse's address is no part of it."""
    given = with_clearinghouse("http://127.0.0.1:9", signer_url=SIGNER)
    async with parts_of(given) as parts, parts_of(self_hosted(signer_url=SIGNER)) as own:
        assert parts.credentials is None  # the engine's own records and secret store
        assert isinstance(parts.transport, SdkRunnerTransport) and parts.transport.pays
        assert vars(parts.transport) == vars(own.transport)  # one transport, whoever runs the signer
        assert "127.0.0.1:9" not in repr(vars(parts.transport))
    engine = await BuilderEngine.create(given)
    try:
        assert engine.jobs._dispatcher._credentials is engine.payments  # where the paid call is (ISS-26)
    finally:
        await engine.stop()


async def test_without_a_signer_address_the_engine_does_not_pay() -> None:
    """As today for any engine: the clearinghouse's address never stands in for the signer's."""
    async with parts_of(with_clearinghouse()) as parts:
        assert isinstance(parts.transport, SdkRunnerTransport) and not parts.transport.pays
        assert vars(parts.transport)["_signer_url"] is None


async def test_without_them_the_engine_is_built_for_self_hosted_payments_as_before() -> None:
    batteries = {
        "batteries_url": "http://127.0.0.1:9",
        "batteries_token": "bt",
        "batteries_grants_token": "g",
    }
    async with parts_of(self_hosted(signer_url="http://127.0.0.1:9", **batteries)) as parts:
        assert isinstance(parts.allocation_manager, BatteriesClient)
        assert isinstance(parts.grants_manager, BatteriesClient)
        assert isinstance(parts.usage_source, BatteriesUsageSource)
        assert parts.credentials is None and parts.allowances is None  # the engine's own records
        assert isinstance(parts.transport, SdkRunnerTransport) and parts.transport.pays
    async with parts_of(self_hosted()) as bare:
        assert bare.allocation_manager is None and bare.grants_manager is None and bare.usage_source is None
        assert isinstance(bare.transport, SdkRunnerTransport) and not bare.transport.pays


async def test_a_supplied_part_is_never_replaced_by_a_remote_adapter() -> None:
    manager, usage, transport = FakeAllocationManager(), FakeUsageSource(), ScriptedTransport()
    supply = EngineSupply(
        allocation_manager=manager,
        grants_manager=None,
        usage_source=usage,
        transport=transport,
        credentials=None,
        allowances=None,
    )
    async with parts_of(with_clearinghouse(), supply) as parts:
        assert parts.allocation_manager is manager and parts.grants_manager is None
        assert parts.usage_source is usage and parts.transport is transport
        assert parts.credentials is None and parts.allowances is None


async def test_an_engine_that_uses_a_clearinghouse_needs_the_secret_key_for_its_payment_keys() -> None:
    with pytest.raises(ValueError, match="LPB_SECRET_KEY"):
        await BuilderEngine.create(with_clearinghouse(secret_key=None))


async def test_the_engine_reads_its_grant_and_has_no_wallet() -> None:
    engine = await BuilderEngine.create(with_clearinghouse())
    try:
        assert engine.payments.tenants is not None  # its own grant is read through the tenant desk
        assert engine.payments.wallet is None  # the wallet is the clearinghouse's
        assert engine.jobs.streaming is not None
    finally:
        await engine.stop()


async def test_the_remote_adapters_are_never_built_from_half_the_settings(store: SqlStore) -> None:
    """The settings refuse a half (``config.py``). Settings built past that check are refused here too."""
    half = EngineSettings.model_construct(clearinghouse_url="http://127.0.0.1:9", clearinghouse_token=None)
    defaults = ThroughClearinghouse(half, lambda *_: None, store, MemorySecretStore())
    with pytest.raises(ValueError, match="set together"):
        defaults.allocation_manager()
