"""ISS-03: DefaultPolicy, engine-issued keys, the API-key authenticator and the chain."""

import pytest

from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.contracts import ActorContext, AuthRequest, Operation, ResourceRef
from livepeer_builder.errors import AccessDenied, InvalidRequest, NotAuthenticated
from livepeer_builder.services.keys import KeyService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FixedClock


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
