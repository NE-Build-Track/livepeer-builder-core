"""ISS-03 and ISS-08: DefaultPolicy, engine-issued keys (issue, inspect, rotate, revoke, audit log), the
API-key authenticator and the chain."""

import logging
from decimal import Decimal

import pytest

from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.contracts import (
    ActorContext,
    AuthRequest,
    Money,
    Operation,
    ProvisionRequest,
    ResourceRef,
)
from livepeer_builder.errors import AccessDenied, InvalidRequest, NotAuthenticated, NotFound
from livepeer_builder.services.keys import KeyService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock
from livepeer_builder.utils import token_hash
from tests.conftest import EngineFactory


def bearer(token: str) -> AuthRequest:
    return AuthRequest(method="GET", path="/v1/jobs", headers={"authorization": f"Bearer {token}"})


def test_policy_needs_permission_and_ownership(alice: ActorContext) -> None:
    policy = DefaultPolicy()
    policy.check(alice, Operation.JOBS_RUN)
    with pytest.raises(AccessDenied):
        policy.check(alice, Operation.EVENTS_READ)
    with pytest.raises(AccessDenied, match="another actor"):
        policy.check(
            alice,
            Operation.JOBS_READ,
            ResourceRef(kind="job", application_id="default", owner_actor_id="bob"),
        )
    with pytest.raises(AccessDenied, match="another application"):
        policy.check(
            alice,
            Operation.JOBS_READ,
            ResourceRef(kind="job", application_id="other", owner_actor_id="alice"),
        )


def test_admin_operations_need_an_admin_credential(alice: ActorContext, admin: ActorContext) -> None:
    policy = DefaultPolicy()
    loaded = alice.model_copy(update={"permissions": alice.permissions | {"admin.keys"}})
    with pytest.raises(AccessDenied, match="admin credential"):
        policy.check(loaded, Operation.ADMIN_KEYS)  # a permission string on an actor key is not enough
    policy.check(admin, Operation.ADMIN_KEYS)
    policy.check(admin, Operation.JOBS_READ, ResourceRef(kind="job", application_id="x", owner_actor_id="y"))


async def test_issued_key_authenticates_and_revocation_stops_it(
    store: SqlStore, clock: FixedClock, admin: ActorContext
) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    issued = await keys.issue(admin, kind="actor", actor_id="alice", application_id="app-1")
    authenticator = ApiKeyAuthenticator(store)
    actor = await authenticator.authenticate(bearer(issued.token))
    assert actor is not None
    assert (actor.actor_id, actor.application_id, actor.credential_kind) == ("alice", "app-1", "actor")
    assert actor.auth is not None and actor.auth.credential_id == issued.key.key_id
    assert "jobs.run" in actor.permissions
    listed = await keys.list_keys(admin)
    assert [key.key_id for key in listed] == [issued.key.key_id]  # inspection never returns the token
    await keys.revoke(admin, issued.key.key_id)
    with pytest.raises(NotAuthenticated):
        await authenticator.authenticate(bearer(issued.token))


async def test_only_admins_issue_keys(store: SqlStore, clock: FixedClock, alice: ActorContext) -> None:
    with pytest.raises(AccessDenied):
        await KeyService(store, DefaultPolicy(), clock).issue(alice, kind="actor", actor_id="mallory")


async def test_permission_validation(store: SqlStore, clock: FixedClock, admin: ActorContext) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    with pytest.raises(InvalidRequest, match="unknown"):
        await keys.issue(admin, kind="actor", actor_id="a", permissions=["jobs.everything"])
    with pytest.raises(InvalidRequest, match="service credential"):
        await keys.issue(admin, kind="actor", actor_id="a", permissions=["actors.assert"])
    service = await keys.issue(admin, kind="service", actor_id="backend", permissions=["actors.assert"])
    assert service.key.permissions == ("actors.assert",)


async def test_bootstrap_admin_is_one_time(store: SqlStore, clock: FixedClock) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    first = await keys.bootstrap_admin()
    assert first.key.kind == "admin"
    with pytest.raises(AccessDenied, match="already exists"):
        await keys.bootstrap_admin()


async def test_unrecognized_credentials_fall_through_but_invalid_ones_stop(store: SqlStore) -> None:
    class Never:
        calls = 0

        async def authenticate(self, request: AuthRequest) -> ActorContext | None:
            Never.calls += 1
            return None

        def challenge(self) -> str | None:
            return 'Basic realm="enterprise"'

    chain = AuthenticatorChain([ApiKeyAuthenticator(store), Never()])
    assert await chain.authenticate(bearer("not-an-engine-key")) is None
    assert Never.calls == 1
    with pytest.raises(NotAuthenticated):
        await chain.authenticate(bearer("lpk_forged"))  # recognized as ours, invalid: no fall-through
    assert Never.calls == 1
    assert chain.challenge() == 'Bearer realm="livepeer-builder", Basic realm="enterprise"'


async def test_request_fields_cannot_establish_identity(store: SqlStore) -> None:
    request = AuthRequest(
        method="POST", path="/v1/jobs", headers={"x-actor-id": "admin", "livepeer-actor": "admin"}
    )
    assert await ApiKeyAuthenticator(store).authenticate(request) is None


async def test_a_key_is_inspected_without_its_secret(
    store: SqlStore, clock: FixedClock, admin: ActorContext, alice: ActorContext
) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    issued = await keys.issue(admin, kind="actor", actor_id="alice", label="laptop")
    shown = await keys.get(admin, issued.key.key_id)
    assert shown == issued.key
    assert issued.token not in shown.model_dump_json()
    with pytest.raises(NotFound):
        await keys.get(admin, "key_unknown")
    with pytest.raises(AccessDenied):
        await keys.get(alice, issued.key.key_id)  # inspecting keys is admin work


async def test_rotation_replaces_a_key_at_once_with_the_same_identity(
    store: SqlStore, clock: FixedClock, admin: ActorContext
) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    authenticator = ApiKeyAuthenticator(store)
    old = await keys.issue(
        admin, kind="actor", actor_id="alice", application_id="app-1", permissions=["jobs.run"], label="ci"
    )
    new = await keys.rotate(admin, old.key.key_id)
    assert new.token != old.token and new.key.key_id != old.key.key_id
    assert (new.key.kind, new.key.actor_id, new.key.application_id, new.key.permissions, new.key.label) == (
        "actor", "alice", "app-1", ("jobs.run",), "ci",
    )  # fmt: skip
    actor = await authenticator.authenticate(bearer(new.token))
    assert actor is not None and (actor.actor_id, actor.application_id) == ("alice", "app-1")
    with pytest.raises(NotAuthenticated):
        await authenticator.authenticate(bearer(old.token))  # no overlap window (D44)
    relabelled = await keys.rotate(admin, new.key.key_id, label="ci-2")
    assert relabelled.key.label == "ci-2"


async def test_only_an_admin_rotates_and_only_an_active_key(
    store: SqlStore, clock: FixedClock, admin: ActorContext, alice: ActorContext
) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    issued = await keys.issue(admin, kind="actor", actor_id="alice")
    with pytest.raises(AccessDenied):
        await keys.rotate(alice, issued.key.key_id)
    await keys.revoke(admin, issued.key.key_id)
    with pytest.raises(InvalidRequest, match="no active key"):
        await keys.rotate(admin, issued.key.key_id)
    with pytest.raises(InvalidRequest, match="no active key"):
        await keys.rotate(admin, "key_unknown")


async def test_key_changes_are_logged_with_who_acted_and_never_the_secret(
    store: SqlStore, clock: FixedClock, admin: ActorContext, caplog: pytest.LogCaptureFixture
) -> None:
    keys = KeyService(store, DefaultPolicy(), clock)
    with caplog.at_level(logging.DEBUG):
        first = await keys.bootstrap_admin()
        issued = await keys.issue(admin, kind="actor", actor_id="alice", application_id="app-1")
        rotated = await keys.rotate(admin, issued.key.key_id)
        await keys.revoke(admin, rotated.key.key_id)
    events = {
        getattr(r, "event", ""): r for r in caplog.records if getattr(r, "event", "").startswith("keys.")
    }
    assert list(events) == ["keys.bootstrapped", "keys.issued", "keys.rotated", "keys.revoked"]
    assert (events["keys.bootstrapped"].key_id, events["keys.bootstrapped"].by) == (first.key.key_id, "cli")
    issued_event = events["keys.issued"]
    assert (issued_event.key_id, issued_event.kind, issued_event.actor_id, issued_event.application_id) == (
        issued.key.key_id, "actor", "alice", "app-1",
    )  # fmt: skip
    assert issued_event.by == admin.actor_id
    assert (events["keys.rotated"].key_id, events["keys.rotated"].replaced_key_id) == (
        rotated.key.key_id, issued.key.key_id,
    )  # fmt: skip
    assert (events["keys.revoked"].key_id, events["keys.revoked"].by) == (rotated.key.key_id, admin.actor_id)


async def test_no_secret_reaches_a_log_across_the_whole_credential_lifecycle(
    make_engine: EngineFactory, admin: ActorContext, alice: ActorContext, caplog: pytest.LogCaptureFixture
) -> None:
    """Engine keys and payment keys, issued, used, rotated and revoked, with every logger at DEBUG.

    A raw secret never appears anywhere. A token hash is not a credential (authentication hashes what is
    presented), but the engine never logs one either; only a database driver's own DEBUG output, which
    the engine's logging never enables, echoes the stored parameters.
    """
    engine = await make_engine()
    assert engine.keys is not None
    with caplog.at_level(logging.DEBUG):
        first = await engine.keys.bootstrap_admin()
        issued = await engine.keys.issue(admin, kind="actor", actor_id="alice")
        assert await ApiKeyAuthenticator(engine.store).authenticate(bearer(issued.token)) is not None
        rotated = await engine.keys.rotate(admin, issued.key.key_id)
        await engine.keys.revoke(admin, rotated.key.key_id)
        await engine.payments.provision(
            admin,
            ProvisionRequest(
                application_id="default", actor_id="alice", amount=Money(amount=Decimal("1"), currency="USD"),
                grant_id="g-1",
            ),
        )  # fmt: skip
        payment_keys = [(await engine.payments.credential(alice)).token]
        await engine.payments.rotate_key(admin, "default", "alice", "rotate-1")
        payment_keys.append((await engine.payments.credential(alice)).token)
        await engine.payments.revoke(admin, "default", "alice")
    tokens = [first.token, issued.token, rotated.token]
    secrets = [*tokens, *(key for key in payment_keys if key)]
    assert len(secrets) == 5 and all(s.startswith(("lpk_", "lpg_")) for s in secrets)
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__}"
        assert not [s for s in secrets if s in text], f"a secret reached the log: {record.name}"
        if record.name.startswith("livepeer_builder") or record.levelno >= logging.INFO:
            assert not [t for t in tokens if token_hash(t) in text], (
                f"a token hash reached the log: {record.name}"
            )
