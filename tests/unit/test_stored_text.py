"""Text the store keeps is refused at the boundary when the store could not keep it (#32 review).

Ids, labels and references are ``varchar(200)`` (``store.schema.ID``). SQLite ignores the length and
PostgreSQL refuses a longer value, or one holding a NUL character, when the row is written: for
provisioning that is after the provider key was funded. So each is checked where the request is parsed.
A lone surrogate is refused there too: no store, hash or response can encode it.
"""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSettings, EngineSupply
from livepeer_builder.contracts import ActorContext, JobRequest, Money, ProvisionRequest, SecretRef
from livepeer_builder.http import create_app
from livepeer_builder.http.bodies import IssueKeyBody, RotateKeyBody
from livepeer_builder.testing import ScriptedStreamTransport
from livepeer_builder.testing.fakes import MemorySecretStore
from tests.conftest import EngineFactory

LONG = "x" * 201
NUL = "a\x00b"
SURROGATE = "a\ud800b"  # half of a UTF-16 pair, which JSON can write ("\ud800") and nothing can encode
UNSTORABLE = [LONG, NUL, SURROGATE]
PROVISION: dict[str, Any] = {
    "operation_ref": "p1",
    "application_id": "default",
    "actor_id": "alice",
    "amount": Money(amount=Decimal("5"), currency="USD"),
    "grant_id": "grant_1",
    "label": "Alice",
}


@pytest.mark.parametrize("field", ["application_id", "actor_id", "grant_id", "label", "operation_ref"])
@pytest.mark.parametrize("value", UNSTORABLE, ids=["too long", "NUL", "lone surrogate"])
def test_provisioning_refuses_text_the_store_cannot_keep(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        ProvisionRequest(**(PROVISION | {field: value}))


@pytest.mark.parametrize("field", ["app", "capability", "model", "operation_ref"])
@pytest.mark.parametrize("value", UNSTORABLE, ids=["too long", "NUL", "lone surrogate"])
def test_a_job_refuses_text_the_store_cannot_keep(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        JobRequest(**({"app": "example/echo"} | {field: value}))


@pytest.mark.parametrize("field", ["app", "capability", "model", "operation_ref"])
async def test_rest_refuses_a_lone_surrogate_in_a_job_s_text_with_422_before_anything_is_written(
    make_engine: EngineFactory, admin: ActorContext, transport: ScriptedStreamTransport, field: str
) -> None:
    """It reached the job's fingerprint and the store, which cannot encode it: a 500 (review of #139)."""
    engine = await make_engine()
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
    transport.steps = [ScriptedStreamTransport.ok()]  # what a runner would answer, were it reached
    body = json.dumps({"app": "example/echo"} | {field: SURROGATE}).encode()  # ASCII: "a\ud800b"
    headers = {"Authorization": f"Bearer {key.token}", "Content-Type": "application/json"}
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        answer = await client.post("/v1/jobs", content=body, headers=headers)
    assert answer.status_code == 422, answer.text
    refusal = answer.json()["detail"][0]
    assert (refusal["loc"], refusal["input"]) == (["body", field], SURROGATE)
    assert answer.content.isascii()  # the 422 repeats the surrogate as the escape it came in as
    assert await engine.jobs.list_jobs(admin) == [] and transport.calls == []


async def test_a_422_still_repeats_any_other_input_as_utf_8(
    make_engine: EngineFactory, admin: ActorContext
) -> None:
    engine = await make_engine()
    key = await engine.keys.issue(admin, kind="actor", actor_id="alice", operation_ref="issue-alice")
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        headers = {"Authorization": f"Bearer {key.token}"}
        answer = await client.post("/v1/jobs", json={"app": "é\x00"}, headers=headers)
    assert answer.status_code == 422, answer.text
    assert answer.headers["content-type"] == "application/json"
    assert '"input":"é\\u0000"'.encode() in answer.content  # as FastAPI writes it: "é" is not escaped


def test_a_job_keeps_text_of_exactly_the_stored_length() -> None:
    request = JobRequest(app="a" * 200, capability="c" * 200, model="m" * 200, operation_ref="o" * 200)
    assert len(request.app) == len(request.model or "") == 200


@pytest.mark.parametrize("field", ["actor_id", "application_id", "label"])
@pytest.mark.parametrize("value", UNSTORABLE, ids=["too long", "NUL", "lone surrogate"])
def test_issuing_a_key_refuses_text_the_store_cannot_keep(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        IssueKeyBody(**({"actor_id": "bob", "operation_ref": "k1"} | {field: value}))


@pytest.mark.parametrize("field", ["actor_id", "application_id"])
def test_a_key_needs_an_actor_and_an_application(field: str) -> None:
    """An empty one was issued, and then every request with that key failed: ``ActorContext`` needs both."""
    with pytest.raises(ValidationError, match=field):
        IssueKeyBody(**({"actor_id": "bob", "operation_ref": "k1"} | {field: ""}))


@pytest.mark.parametrize("value", UNSTORABLE, ids=["too long", "NUL", "lone surrogate"])
def test_rotating_a_key_refuses_a_label_the_store_cannot_keep(value: str) -> None:
    with pytest.raises(ValidationError, match="label"):
        RotateKeyBody(label=value, operation_ref="r1")


class VaultSecretStore(MemorySecretStore):
    """A supplied SecretStore whose reference is a long path, as an external vault's can be."""

    async def put(self, name: str, secret: str) -> SecretRef:
        ref = f"vault:{name}/{'p' * 300}"
        self.values[ref] = secret
        return SecretRef(ref=ref)


async def test_an_actor_whose_secret_reference_is_longer_than_200_characters_still_reads(
    make_engine: EngineFactory, admin: ActorContext
) -> None:  # a SecretRef is opaque (extending-the-engine.md), and SQLite kept longer ones before the caps
    engine = await make_engine(supply=EngineSupply(secret_store=VaultSecretStore()))
    await engine.payments.provision(admin, ProvisionRequest(**PROVISION))
    read = await engine.payments.get_actor(admin, "default", "alice")
    assert read.secret_ref.ref.startswith("vault:") and len(read.secret_ref.ref) > 200


async def test_the_memory_secret_store_s_ref_fits_for_the_longest_actor() -> None:
    ref = await MemorySecretStore().put(f"{'a' * 200}/{'b' * 200}", "lpg_secret")
    assert len(ref.ref) <= 200


def test_a_shared_allocation_id_longer_than_an_attempt_keeps_fails_at_startup() -> None:
    with pytest.raises(ValidationError, match="shared_allocation_id"):
        EngineSettings.model_validate({"discovery_url": "http://d/discovery", "shared_allocation_id": LONG})
