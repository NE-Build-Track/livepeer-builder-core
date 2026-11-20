"""What the engine hashes into a stored record or a provider's idempotency key is a format, not a name.

Three hashes outlive the release that wrote them:

- ``lpb_operations.fingerprint``: a repeat of an admin command is answered from its record only when the
  fingerprint of the repeat equals the stored one (D57). One that differs is a conflict, so the repeat of
  a command whose answer was lost would be refused instead of replayed.
- the idempotency key of each provider write: Batteries replays a write it has seen under the same key
  and applies one it has not, so a command resumed under another key moves the money twice.
- ``lpb_jobs.request_fingerprint``, which decides the same for a job's ``operation_ref``.

So a rename of a command, a field or a credential kind must not change any of them. The digests below
were computed on ``releases/m4`` before #163 renamed anything, by running these same commands. To fix a
failure here, put the hashed text back as it was; never update a digest to make this pass.

Since #165 a tenant's id is its grant's id, and two of those commands can no longer be sent as they
were: an allotment that names a grant apart from its tenant, and the creation of a grant with no tenant
(onboarding a tenant replaced it). Their two digests are unchanged here and are checked against the
fingerprint of the request as it was recorded, and the tests at the end say what a repeat of such a
record does after the upgrade: it is replayed or it is a conflict, and it is never sent to the provider.
Every other digest, and every key sent to the provider, still comes from running the command.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    ActorAllocation,
    ActorContext,
    AllotRequest,
    CredentialKind,
    JobRequest,
    Money,
    OperationCommand,
    OperationRecord,
    SecretRef,
    TopUpRequest,
)
from livepeer_builder.errors import OperationConflict
from livepeer_builder.services import ledger
from livepeer_builder.services.grants import GRANT_TAG
from livepeer_builder.services.request_identity import request_fingerprint
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FixedClock
from tests.conftest import EngineFactory

USD_5 = Money(amount=Decimal("5"), currency="USD")
WHY = "a digest written before this release no longer matches: see this module's docstring"

FINGERPRINTS = {
    "provision": "8eeeba74ca6295846627a15f9de0b1994cff06254598fdd95e62b0ff000b190d",
    "fund": "73f24f70556e5eac6b2e2e89e8ca57a3299bb20a8347e92b562890039175d088",
    "fund-grant": "5f7818ec743f3f14b9eff1c6e23f5cfd719ba44eeb02b45ce460510f5316a971",
    "create-grant": "ecedcb64dc79c68a37c202c6a71a0f940c1ad97b2200985a678a7cadaaeb8a23",
    "rotate-payment-key": "2ccc34329672956c386e408d05de16352ddd7f2248543f7503ebe2160a39f8fe",
    "issue-admin-key": "d2cd57850aa5dcc09c24b8ffc6d400efb1cb5ed7149d01e048d4b4dd5502c5cd",
    "issue-auditor-key": "54886113fc718a043112897126230ac2b1b4d61dea0d4799ebaa56d740eeeb3f",
    "issue-actor-key": "824da991d86817da4ae6af5cb42cf76d28624496e78a114538a34edf1ad74220",
}
# The two commands #165 no longer sends in this form, as their requests were recorded: an allotment
# from a grant that is not its tenant's id, and a grant created with no tenant.
AS_RECORDED: dict[str, tuple[OperationCommand, dict[str, Any]]] = {
    "provision": (
        "allot",
        {
            "tenant_id": "default",
            "actor_id": "alice",
            "amount": USD_5,
            "label": "Alice",
            "grant_id": "grant_1",
        },
    ),
    "create-grant": ("create_grant", {"name": "Team", "amount": USD_5}),
}
# Written first by #165, and pinned from then on: onboarding a tenant, a tenant's status, a tenant admin
# key, and an allotment as it is hashed now (the grant is the tenant's id).
SINCE_165 = {
    "onboard-tenant": "e465ceace9bfa7dc9ffa16b3086b592496e712af7103f51911fb35585e3c4300",
    "set-tenant-status": "85853a0375cc734ec1d50d3d2a79f3e4097e6da171b4c0c01c8bfbc7e797f987",
    "issue-tenant-admin-key": "d8e72df2a994de99a45bf610c95b4c645de21c2f71f3b645199ef323b0aef63e",
    "allot": "aead52559a3ffcdab2faed857aea20ac66909dccc59a4799a38a0bd2358f9277",
}
# In the order the commands run: the allocation and its key, the allocation's money, the grant's money, the
# rotation's key, and the tag that finds a created grant again (Batteries takes no key on that call).
SENT_TO_THE_PROVIDER = [
    "kb-bvoOJubMv8opZcjgrQm49UDovkkpD3ieekczrY-w",
    "9t5UMwz545mkGq7vhproaAeAtGGjlDktQthvXUfX9Uo",
    "vVr1U2dYls0QCrgA8mxcTgdcwM1oGYQUgvPS72IOZyo",
    "plNGaYio7xHz7x5rFjYYB100YDPsHF0kjNCRHFip8RY",
    "EyXAIYuvDTcB32ErJvP-ptxlKiw8WkPbCEY48APwkI4",
]


async def run_every_command(engine: BuilderEngine, admin: ActorContext) -> None:
    """Each repeat-safe admin command once, under the reference its digest is keyed by. The rotation of an
    access key is left out: its request is a random key id and a label, and holds no name.

    Onboarding runs first, under the reference the grant's creation had: it creates ``grant_1``, the grant
    the top-up below names, and tags it as that creation did."""
    payments, keys, desk = engine.payments, engine.keys, engine.payments.tenants
    assert keys is not None and desk is not None
    onboarded = await desk.onboard(admin, "Team", USD_5, operation_ref="create-grant")
    assert onboarded.tenant_id == "grant_1"
    request = AllotRequest(
        tenant_id="default",
        actor_id="alice",
        amount=USD_5,
        label="Alice",
        operation_ref="provision",
    )
    await payments.allot(admin, request)
    await payments.top_up(admin, "default", "alice", TopUpRequest(amount=USD_5, operation_ref="fund"))
    await payments.top_up_grant(admin, "grant_1", TopUpRequest(amount=USD_5, operation_ref="fund-grant"))
    await payments.rotate_payment_key(admin, "default", "alice", "rotate-payment-key")
    await desk.set_status(admin, "grant_1", "paused", operation_ref="set-tenant-status")
    kinds: dict[str, CredentialKind] = {
        "admin": "clearinghouse_admin",
        "auditor": "auditor",
        "actor": "actor",
        "tenant-admin": "tenant_admin",
    }
    for was, kind in kinds.items():  # the reference names the kind as the digest was written
        await keys.issue(admin, kind=kind, actor_id="alice", label="L", operation_ref=f"issue-{was}-key")


async def stored_fingerprints(store: SqlStore, references: dict[str, str]) -> dict[str, str]:
    return {
        ref: record.fingerprint
        for ref in references
        if (record := await store.get_operation("default", "operator", ref)) is not None
    }


async def test_every_admin_command_keeps_its_fingerprint(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore
) -> None:
    await run_every_command(await make_engine(engine_id="engine-1"), admin)
    still_sent = {ref: digest for ref, digest in FINGERPRINTS.items() if ref not in AS_RECORDED}
    assert await stored_fingerprints(store, still_sent) == still_sent, WHY
    for ref, (command, request) in AS_RECORDED.items():
        assert ledger.fingerprint(command, request) == FINGERPRINTS[ref], WHY


async def test_the_commands_first_written_by_165_keep_their_fingerprints(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore
) -> None:
    await run_every_command(await make_engine(engine_id="engine-1"), admin)
    written = {"create-grant": "onboard-tenant", "provision": "allot"}  # the reference each ran under
    stored = await stored_fingerprints(store, dict.fromkeys([*SINCE_165, *written], ""))
    assert {written.get(ref, ref): digest for ref, digest in stored.items()} == SINCE_165, WHY


async def test_every_provider_write_keeps_its_idempotency_key(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine(engine_id="engine-1")
    await run_every_command(engine, admin)
    tagged = next(grant for grant in manager.grants if grant.id == "grant_1")
    sent = [*manager.replays, tagged.metadata[GRANT_TAG]]
    assert sent == SENT_TO_THE_PROVIDER, WHY


# -- a command recorded before #165, repeated after it -------------------------------------------------


def recorded(
    ref: str,
    clock: FixedClock,
    *,
    state: str = "done",
    result: Any = None,
    fingerprint: str | None = None,
    command: OperationCommand | None = None,
) -> OperationRecord:
    """The ledger record a release before #165 wrote for the command ``FINGERPRINTS`` keys by ``ref``."""
    return OperationRecord(
        tenant_id="default",
        actor_id="operator",
        operation_ref=ref,
        command=command or AS_RECORDED[ref][0],
        fingerprint=fingerprint or FINGERPRINTS[ref],
        state=state,  # type: ignore[arg-type]
        result=result,
        started_at=clock.now(),
        completed_at=clock.now() if state == "done" else None,
    )


def alice_from(grant_id: str, now: datetime) -> ActorAllocation:
    return ActorAllocation(
        tenant_id="default",
        actor_id="alice",
        grant_id=grant_id,
        allocation_id="alloc_before",
        api_key_id="key_before",
        secret_ref=SecretRef(ref="mem:before"),
        currency="USD",
        status="active",
        created_at=now,
        updated_at=now,
        label="Alice",
    )


ALLOT_ALICE = AllotRequest(
    tenant_id="default", actor_id="alice", amount=USD_5, label="Alice", operation_ref="provision"
)


async def test_an_allotment_recorded_with_its_grant_before_165_is_replayed_and_never_sent(
    make_engine: EngineFactory,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    clock: FixedClock,
) -> None:
    """The request named ``grant_1`` and no request names a grant now. The actor's record still does, so
    the repeat is this command's: it is answered from the record, and nothing is sent to the provider."""
    engine = await make_engine(engine_id="engine-1")
    alice = alice_from("grant_1", clock.now())
    await store.save_actor(alice)
    await store.begin_operation(recorded("provision", clock, result=alice.model_dump(mode="json")))
    assert await engine.payments.allot(admin, ALLOT_ALICE) == alice
    assert OPERATION_REPLAYED.get() is True and manager.calls == []
    with pytest.raises(OperationConflict):  # another amount under the reference is still a conflict
        await engine.payments.allot(
            admin, ALLOT_ALICE.model_copy(update={"amount": USD_5.model_copy(update={"amount": Decimal(50)})})
        )
    assert manager.calls == []


@pytest.mark.parametrize("state", ["done", "started"])
async def test_an_allotment_recorded_before_165_that_matches_no_record_is_a_conflict_and_never_sent(
    make_engine: EngineFactory,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    clock: FixedClock,
    state: str,
) -> None:
    """Two cases have no record to match. A request that named no grant (the caller's own allocation
    chose it) was hashed with an empty grant. And a command still ``started`` has saved no actor, so
    nothing says which grant it named. Each repeat is 409 ``operation_conflict`` before any provider
    call, however old the record: it is never resumed, so never sent under a key the provider has not
    seen. Resolve a ``started`` allotment before upgrading."""
    engine = await make_engine(engine_id="engine-1")
    no_grant = ledger.fingerprint("allot", AS_RECORDED["provision"][1] | {"grant_id": ""})
    held = FINGERPRINTS["provision"] if state == "started" else no_grant
    if state == "done":
        await store.save_actor(alice_from("grant_1", clock.now()))
    await store.begin_operation(recorded("provision", clock, state=state, fingerprint=held))
    clock.advance(3600)  # long past the stale window: a matching record would be resumed
    with pytest.raises(OperationConflict):
        await engine.payments.allot(admin, ALLOT_ALICE)
    assert manager.calls == []


async def test_an_allotment_left_started_before_165_for_a_revoked_actor_is_a_conflict_and_never_resumed(
    make_engine: EngineFactory,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    clock: FixedClock,
) -> None:
    """An actor revoked and allotted again before #165, with the second allotment left ``started``. Its
    record names the grant the request named, so the repeat matches the record's earlier form. It is
    still never resumed (#165 review): resumed, it would be refused for the grant that is not the
    tenant's id, a resumed run is never abandoned, and the reference would answer 400 for good. It is 409
    ``operation_conflict`` before any provider call, however old and however often repeated."""
    engine = await make_engine(engine_id="engine-1")
    revoked = alice_from("grant_1", clock.now()).model_copy(update={"status": "revoked"})
    await store.save_actor(revoked)
    await store.begin_operation(recorded("provision", clock, state="started"))
    clock.advance(3600)  # long past the stale window: a record of this release's form would be resumed
    for _ in range(2):
        with pytest.raises(OperationConflict):
            await engine.payments.allot(admin, ALLOT_ALICE)
    assert manager.calls == []
    held = await store.get_operation("default", "operator", "provision")
    assert held is not None and held.state == "started" and held.fencing_token == 1  # never taken over


@pytest.mark.parametrize("state", ["done", "started"])
async def test_a_grant_creation_recorded_before_165_is_never_run_again_as_an_onboarding(
    make_engine: EngineFactory,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    clock: FixedClock,
    state: str,
) -> None:
    """Onboarding replaced the creation of a grant. Under the reference of a creation recorded before
    the upgrade, finished or not, it is another command: 409 before any provider call, so a grant the
    earlier run may have created is never joined by a second."""
    engine = await make_engine(engine_id="engine-1")
    desk = engine.payments.tenants
    assert desk is not None
    await store.begin_operation(recorded("create-grant", clock, state=state))
    clock.advance(3600)
    with pytest.raises(OperationConflict):
        await desk.onboard(admin, "Team", USD_5, operation_ref="create-grant")
    assert manager.calls == [] and [grant.id for grant in manager.grants] == ["default"]


async def test_a_grant_top_up_left_unfinished_before_165_resumes_under_the_key_it_was_sent_with(
    make_engine: EngineFactory,
    admin: ActorContext,
    store: SqlStore,
    manager: FakeAllocationManager,
    clock: FixedClock,
) -> None:
    """The grant was named in the body and is named in the path now. The fingerprint and the provider's
    idempotency key hash the same text, so the repeat resumes its own record and the provider, which may
    have applied the first call, replays it: the money moves once."""
    manager.add_grant("grant_1", total=0)
    engine = await make_engine(engine_id="engine-1")
    await store.begin_operation(recorded("fund-grant", clock, state="started", command="top_up_grant"))
    clock.advance(3600)
    request = TopUpRequest(amount=USD_5, operation_ref="fund-grant")
    await engine.payments.top_up_grant(admin, "grant_1", request)
    assert [key for _, _, key in manager.topped_up_grants] == [SENT_TO_THE_PROVIDER[2]]
    await engine.payments.top_up_grant(admin, "grant_1", request)
    assert manager.calls.count("top_up_grant") == 1 and manager.unallocated("grant_1") == Decimal(5)


def test_a_job_request_keeps_its_fingerprint() -> None:
    request = JobRequest(app="livepeer-example/hello", path="/run", payload={"prompt": "hi"})
    stored = "e29b846bd1314a69ae0ffc978347354fb5570560f1e47298245d1e418c12b111"
    assert request_fingerprint(request) == stored, WHY
