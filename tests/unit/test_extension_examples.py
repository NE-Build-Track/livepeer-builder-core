"""Runnable enterprise extension examples (docs/design-docs/extending-the-engine.md).

Each class replaces one simple default with an enterprise-grade version **through a protocol**, with no
core patches. The tests wire them into a real BuilderEngine and prove the behavior. Copy these as a
starting point; they are examples, not shipped implementations.
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import (
    ActorContext,
    AuthProvenance,
    AuthRequest,
    JobRequest,
    Money,
    Operation,
    ProvisionRequest,
    ResourceRef,
    Runner,
    SecretRef,
)
from livepeer_builder.errors import AccessDenied, NotAuthenticated, NotFound
from livepeer_builder.protocols import AccessPolicy, DiscoverySource
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager, FixedClock, ScriptedTransport, runner

# -- 1. Authenticator: trust the enterprise SSO gateway, verified by HMAC ----------------------------


class SignedSsoHeaderAuthenticator:
    """The enterprise SSO proxy signs (user, tenant, team). Unsigned headers are never trusted."""

    def __init__(self, shared_secret: bytes) -> None:
        self._secret = shared_secret

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        user = request.header("x-sso-user")
        if user is None:
            return None  # not our credential: let the next authenticator try
        tenant = request.header("x-sso-tenant") or ""
        team = request.header("x-sso-team") or ""
        expected = sign(self._secret, user, tenant, team)
        if not hmac.compare_digest(expected, request.header("x-sso-signature") or ""):
            raise NotAuthenticated("bad SSO signature")  # ours, but invalid: stop the chain (401)
        return ActorContext(
            actor_id=user,
            application_id=tenant,
            permissions=frozenset(
                {Operation.DISCOVER, Operation.JOBS_RUN, Operation.JOBS_READ, Operation.COSTS_READ}
            ),
            attributes={"team": team},
            auth=AuthProvenance(method="sso", issuer="corp-sso"),
        )

    def challenge(self) -> str | None:
        return 'SSO realm="corp"'


def sign(secret: bytes, user: str, tenant: str, team: str) -> str:
    return hmac.new(secret, f"{user}|{tenant}|{team}".encode(), hashlib.sha256).hexdigest()


# -- 2. AccessPolicy: team-based reads, delegating everything else to the default -----------------


class TeamReadPolicy:
    """Teammates can read each other's jobs and costs. Writes and admin keep the default rules."""

    READS = frozenset({Operation.JOBS_READ, Operation.COSTS_READ})

    def __init__(self, directory: Mapping[str, str], fallback: AccessPolicy | None = None) -> None:
        self._team_of = dict(directory)  # actor_id -> team, from the enterprise directory
        self._fallback = fallback or DefaultPolicy()

    def check(self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None) -> None:
        owner = resource.owner_actor_id if resource else None
        same_team = owner is not None and self._team_of.get(owner) == self._team_of.get(actor.actor_id)
        teammate_read = operation in self.READS and resource is not None and same_team
        if (
            teammate_read
            and operation in actor.permissions
            and resource.application_id == actor.application_id
        ):
            return
        self._fallback.check(actor, operation, resource)


# -- 3. SecretStore: an enterprise vault with an access audit (never logs the secret) ---------------


@dataclass
class AuditedVaultSecretStore:
    """Stands in for HashiCorp Vault or a cloud secret manager."""

    vault: dict[str, str] = field(default_factory=dict)
    audit: list[tuple[str, str]] = field(default_factory=list)

    async def put(self, name: str, secret: str) -> SecretRef:
        self.vault[f"vault:{name}"] = secret
        self.audit.append(("put", name))
        return SecretRef(ref=f"vault:{name}")

    async def get(self, ref: SecretRef) -> str:
        self.audit.append(("get", ref.ref))
        if ref.ref not in self.vault:
            raise NotFound("unknown secret reference")
        return self.vault[ref.ref]

    async def delete(self, ref: SecretRef) -> None:
        self.audit.append(("delete", ref.ref))
        self.vault.pop(ref.ref, None)


# -- 4. SelectionPolicy: price-aware routing ---------------------------------------------------------


class CheapestReadyFirst:
    """Cheapest advertised rate first, then most free capacity. Unpriced runners go last."""

    def order(self, candidates: Sequence[Runner], request: JobRequest) -> Sequence[Runner]:
        del request

        def key(r: Runner) -> tuple[Decimal, int]:
            price = r.rate.amount if r.rate else Decimal("Infinity")
            return price, -(r.capacity_available if r.capacity_available is not None else 1)

        return sorted(candidates, key=key)


# -- 5. DiscoverySource: only approved orchestrators (allow-list) ------------------------------------


class ApprovedOrchestratorsOnly:
    """Wraps any discovery source and drops runners on orchestrators the enterprise has not approved."""

    def __init__(self, inner: DiscoverySource, approved: set[str]) -> None:
        self._inner = inner
        self._approved = approved

    async def fetch(self) -> tuple[Runner, ...]:
        return tuple(r for r in await self._inner.fetch() if r.orchestrator_url in self._approved)


# -- wiring ------------------------------------------------------------------------------------------

SECRET = b"shared-with-the-sso-proxy"
DIRECTORY = {"alice": "red", "bob": "red", "carol": "blue"}


def priced(url: str, price: str, orchestrator: str = "https://orch.example") -> Runner:
    return runner(url=url, price=price).model_copy(update={"orchestrator_url": orchestrator})


@pytest.fixture
async def enterprise_engine(store: SqlStore, clock: FixedClock):  # type: ignore[no-untyped-def]
    vault = AuditedVaultSecretStore()
    transport = ScriptedTransport()
    discovery = ApprovedOrchestratorsOnly(
        StaticDiscoverySource(
            [
                priced("https://expensive", "0.50"),
                priced("https://cheap", "0.10"),
                priced("https://unapproved", "0.01", orchestrator="https://rogue.example"),
            ]
        ),
        approved={"https://orch.example"},
    )
    engine = await BuilderEngine.from_settings(
        EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False),
        store=store,
        clock=clock,
        authenticators=[SignedSsoHeaderAuthenticator(SECRET)],  # tried before the engine's API keys
        policy=TeamReadPolicy(DIRECTORY),
        secret_store=vault,
        selection=CheapestReadyFirst(),
        discovery_source=discovery,
        transport=transport,
        allocation_manager=FakeAllocationManager(),
    )
    await engine.discovery.refresh()
    yield engine, vault, transport
    await engine.stop()


def sso(user: str, tenant: str = "acme", signature: str | None = None) -> AuthRequest:
    team = DIRECTORY.get(user, "")
    return AuthRequest(
        method="POST",
        path="/v1/jobs",
        headers={
            "x-sso-user": user,
            "x-sso-tenant": tenant,
            "x-sso-team": team,
            "x-sso-signature": signature if signature is not None else sign(SECRET, user, tenant, team),
        },
    )


async def test_enterprise_build_runs_without_core_patches(enterprise_engine) -> None:  # type: ignore[no-untyped-def]
    engine, vault, transport = enterprise_engine
    admin = ActorContext(actor_id="ops", credential_kind="admin")

    alice = await engine.authenticator.authenticate(sso("alice"))
    assert alice is not None and (alice.actor_id, alice.application_id) == ("alice", "acme")
    with pytest.raises(NotAuthenticated):
        await engine.authenticator.authenticate(sso("alice", signature="forged"))

    await engine.payments.provision(
        admin,
        ProvisionRequest(
            application_id="acme",
            actor_id="alice",
            amount=Money(amount=Decimal(5), currency="USD"),
            grant_id="grant_1",
        ),
    )
    assert vault.audit[0] == ("put", "lpg/acme/alice")  # the payment key went to the enterprise vault

    transport.steps = [ScriptedTransport.ok()]
    result = await engine.jobs.run(alice, JobRequest(app="example/echo"))
    assert (
        transport.calls[0][0] == "https://cheap"
    )  # price-aware routing; the unapproved runner never appears
    assert ("get", "vault:lpg/acme/alice") in vault.audit

    bob = await engine.authenticator.authenticate(sso("bob"))
    carol = await engine.authenticator.authenticate(sso("carol"))
    assert bob is not None and carol is not None
    assert (await engine.jobs.get(bob, result.job.id)).id == result.job.id  # teammate can read
    with pytest.raises(NotFound):
        await engine.jobs.get(carol, result.job.id)  # other team: invisible
    with pytest.raises(AccessDenied):
        await engine.payments.provision(
            bob,
            ProvisionRequest(
                application_id="acme", actor_id="bob", amount=Money(amount=Decimal(1), currency="USD")
            ),
        )  # admin rules still come from the default policy


async def test_offerings_respect_the_allow_list(enterprise_engine) -> None:  # type: ignore[no-untyped-def]
    engine, _, _ = enterprise_engine
    alice = await engine.authenticator.authenticate(sso("alice"))
    runners = engine.discovery.runners(alice, "example/echo")
    assert {r.url for r in runners} == {"https://expensive", "https://cheap"}
