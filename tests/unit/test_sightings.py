"""People an identity provider signs in become known actors before any allocation (#52, D48)."""

import logging

import pytest

from livepeer_builder.contracts import ActorContext, AuthProvenance, AuthRequest, SeenActor
from livepeer_builder.services.sightings import SightingRecorder
from livepeer_builder.testing import FixedClock

REQUEST = AuthRequest(method="GET", path="/v1/jobs", headers={})


def person(name: str | None = "alice", *, kind: str = "actor", method: str = "jwt") -> ActorContext:
    return ActorContext(
        actor_id="4f6c2a1e",
        credential_kind=kind,  # type: ignore[arg-type]
        auth=AuthProvenance(method=method),
        display_name=name,
    )


class Inner:
    def __init__(self, actor: ActorContext | None) -> None:
        self.actor = actor

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        return self.actor

    def challenge(self) -> str | None:
        return 'Bearer realm="test"'


class Sightings:
    """A spy ActorRepo: the sightings the recorder saved, in order."""

    def __init__(self, *, broken: bool = False) -> None:
        self.saved: list[SeenActor] = []
        self.broken = broken

    async def save_sighting(self, seen: SeenActor) -> None:
        if self.broken:
            raise RuntimeError("database is locked")
        self.saved.append(seen)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


async def test_a_person_is_recorded_with_their_name_once_an_hour(clock: FixedClock) -> None:
    store = Sightings()
    recorder = SightingRecorder(Inner(person()), store, clock)
    assert (await recorder.authenticate(REQUEST)) == person()
    await recorder.authenticate(REQUEST)  # the same person again: no write
    assert [(s.actor_id, s.name, s.first_seen_at) for s in store.saved] == [
        ("4f6c2a1e", "alice", clock.now())
    ]
    clock.advance(3600)
    await recorder.authenticate(REQUEST)
    assert (
        len(store.saved) == 2 and store.saved[-1].last_seen_at == clock.now()
    )  # last seen, refreshed hourly


async def test_a_new_name_is_recorded_at_once(clock: FixedClock) -> None:
    store = Sightings()
    inner = Inner(person())
    recorder = SightingRecorder(inner, store, clock)
    await recorder.authenticate(REQUEST)
    inner.actor = person("alice.smith")  # renamed at the identity provider
    await recorder.authenticate(REQUEST)
    assert [s.name for s in store.saved] == ["alice", "alice.smith"]


@pytest.mark.parametrize(
    "actor",
    [
        person(kind="admin"),
        person(kind="auditor"),
        person(method="api_key"),
        None,
    ],
    ids=["an operator", "the console's reader", "an engine key's actor", "not a credential"],
)
async def test_only_identity_provider_builders_are_recorded(
    clock: FixedClock, actor: ActorContext | None
) -> None:
    store = Sightings()
    recorder = SightingRecorder(Inner(actor), store, clock)
    assert await recorder.authenticate(REQUEST) == actor
    assert store.saved == []


async def test_a_failed_write_never_fails_the_request(
    clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = SightingRecorder(Inner(person()), Sightings(broken=True), clock)
    with caplog.at_level(logging.WARNING):
        assert await recorder.authenticate(REQUEST) == person()
    assert any(getattr(r, "event", None) == "actors.sighting_failed" for r in caplog.records)


async def test_the_challenge_is_the_inner_chains(clock: FixedClock) -> None:
    assert SightingRecorder(Inner(None), Sightings(), clock).challenge() == 'Bearer realm="test"'
