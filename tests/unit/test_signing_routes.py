"""#166: a clearinghouse serves the signer's two paths, each called with a payment key (D87).

``POST /sign-orchestrator-info`` and ``POST /generate-live-payment`` are at the root of the listener,
where the unmodified gateway SDK calls a signer. An enterprise's engine points the SDK at the
clearinghouse with an actor's payment key where a Batteries key goes today. The clearinghouse finds the
actor, puts that actor's Batteries key in the key's place and hands the call to its own signer. The body,
the signer's ``state`` and the signer's answer pass through as they are. The Batteries key never leaves,
and the payment key never reaches the signer.

The signer here is the shipped fake (``FakeSigningTransport``), which records what it was handed.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import delete

from livepeer_builder import EngineSupply
from livepeer_builder.contracts import SignerAnswer
from livepeer_builder.errors import InvalidRequest, SignerNotReached, SignerOutcomeUnknown
from livepeer_builder.http import create_app
from livepeer_builder.store import SqlStore
from livepeer_builder.store import schema as t
from livepeer_builder.testing import FakeAllocationManager, FakeSigningTransport, ScriptedStreamTransport
from tests.conftest import EngineFactory
from tests.unit.clearinghouse_house import open_house
from tests.unit.test_tenants import House, Tenant, allotted

IDENTITY = "/sign-orchestrator-info"
PAYMENT = "/generate-live-payment"
PATHS = (IDENTITY, PAYMENT)
# One payment request as the SDK writes it: a float price, and the signer's opaque state from the cycle
# before. The spacing is the test's own, so a body that was parsed and written again would differ.
REQUEST = (
    b'{"orchestrator": "cGFyYW1z",  "type":"live","ManifestID":"m-1",'
    b'"maxPrice":{"price":1012.0000000000001,"currency":"wei","unit":"seconds"},'
    b'"state":{"state":"b3BhcXVlIHNpZ25lciBzdGF0ZQ==","sig":"c2ln"}}'
)


@pytest.fixture
def signer() -> FakeSigningTransport:
    return FakeSigningTransport()


@pytest.fixture
async def house(make_engine: EngineFactory, signer: FakeSigningTransport) -> AsyncIterator[House]:
    async with open_house(make_engine, supply=EngineSupply(transport=signer)) as opened:
        yield opened


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def payment_key(house: House, tenant: Tenant, actor_id: str) -> tuple[str, dict[str, Any]]:
    """An actor allotted in ``tenant``: its payment key, and its allocation."""
    allocation = await allotted(house, tenant, actor_id)
    return allocation["payment_key"]["token"], allocation


def batteries_key(manager: FakeAllocationManager, allocation: dict[str, Any]) -> str:
    return manager.keys[allocation["api_key_id"]][1]


async def call(house: House, path: str, token: str, body: bytes = REQUEST) -> httpx.Response:
    headers = bearer(token) | {"Content-Type": "application/json"}
    return await house.client.post(path, content=body, headers=headers)


# -- the swap ------------------------------------------------------------------------------------------------


async def test_a_payment_is_signed_with_the_actors_batteries_key_in_the_payment_keys_place(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager
) -> None:
    token, allocation = await payment_key(house, house.one, "alice")
    signed = SignerAnswer(
        status_code=200,
        content=b'{"payment": "cGF5", "segCreds":"c2Vn","state":{"state":"bmV4dA==","sig":"czI="}}',
        content_type="application/json",
    )
    signer.signer_answers.append(signed)

    answer = await call(house, PAYMENT, token)

    assert answer.status_code == 200
    assert answer.content == signed.content  # the signer's answer, byte for byte: its state included
    assert answer.headers["content-type"] == "application/json"
    [forwarded] = signer.forwarded
    assert forwarded.call == "generate-live-payment"
    assert forwarded.credential == batteries_key(manager, allocation)  # what the signer was handed
    assert forwarded.credential.startswith("lpg_")
    assert forwarded.body == REQUEST  # as it arrived: the float, the state and the spacing
    assert forwarded.content_type == "application/json"
    assert token not in str(forwarded)  # the payment key stops at the clearinghouse
    assert forwarded.credential not in answer.text and forwarded.credential not in str(answer.headers)


async def test_the_identity_call_takes_a_payment_key_and_goes_to_the_signer(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager
) -> None:
    token, allocation = await payment_key(house, house.one, "alice")
    answer = await call(house, IDENTITY, token, b"{}")
    assert answer.status_code == 200
    assert set(answer.json()) == {"address", "signature"}  # what the SDK reads of a signer's identity
    [forwarded] = signer.forwarded
    assert (forwarded.call, forwarded.body) == ("sign-orchestrator-info", b"{}")
    assert forwarded.credential == batteries_key(manager, allocation)


async def test_a_payment_key_signs_for_its_own_actor_only(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager
) -> None:
    """Three actors in two tenants, one actor id used twice. Each key reaches the Batteries key of the
    actor it was issued for, and never another's: not another actor's of its tenant, not another
    tenant's actor of the same name."""
    holders = [(house.one, "alice"), (house.one, "bob"), (house.two, "alice")]
    keys = [await payment_key(house, tenant, actor_id) for tenant, actor_id in holders]
    own = [batteries_key(manager, allocation) for _, allocation in keys]
    assert len(set(own)) == 3
    for (token, _), expected in zip(keys, own, strict=True):
        signer.forwarded.clear()
        for path in PATHS:
            assert (await call(house, path, token)).status_code == 200
        assert [forwarded.credential for forwarded in signer.forwarded] == [expected, expected]


async def test_a_rotated_allocation_signs_with_its_new_batteries_key(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager
) -> None:
    old, before = await payment_key(house, house.one, "alice")
    rotated = (
        await house.client.post(
            house.one.allocation("alice", "/rotate-payment-key"),
            json={"operation_ref": "rotate-1"},
            headers=house.one.admin,
        )
    ).json()
    assert (await call(house, PAYMENT, old)).status_code == 401
    assert (await call(house, PAYMENT, rotated["payment_key"]["token"])).status_code == 200
    [forwarded] = signer.forwarded
    assert forwarded.credential == batteries_key(manager, rotated) != batteries_key(manager, before)


# -- who is let in -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_nothing_but_a_live_payment_key_is_answered_and_nothing_else_reaches_the_signer(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager, path: str
) -> None:
    """Both paths, the identity one included: nothing on a clearinghouse's listener answers without a
    credential. An access key and a Batteries key are no payment key, and are never forwarded."""
    token, allocation = await payment_key(house, house.one, "alice")
    access_key = house.one.admin["Authorization"].removeprefix("Bearer ")
    clearinghouse_admin = house.admin["Authorization"].removeprefix("Bearer ")
    lpg = batteries_key(manager, allocation)
    refused: dict[str, dict[str, str]] = {
        "no credential": {},
        "an empty bearer": {"Authorization": "Bearer "},
        "another scheme": {"Authorization": f"Basic {token}"},
        "the key with no scheme": {"Authorization": token},
        "an access key": bearer(access_key),
        "a clearinghouse admin's key": bearer(clearinghouse_admin),
        "the Batteries key": bearer(lpg),
        "a payment key never issued": bearer("lpp_" + "x" * 43),
        "the key cut short": bearer(token[:-1]),
        "the key with more after it": bearer(token + "x"),
        "a key far too long": bearer("lpp_" + "x" * 5000),
    }
    for name, headers in refused.items():
        answer = await house.client.post(path, content=REQUEST, headers=headers)
        assert answer.status_code == 401, name
        assert answer.headers["www-authenticate"] == "Bearer", name
        assert answer.json()["error"]["message"], name
        for secret in (token, lpg, access_key):
            assert secret not in answer.text, name
    assert signer.forwarded == []

    assert (await house.client.post(path, content=REQUEST, headers=bearer(token))).status_code == 200
    spelled = {"authorization": f"bearer {token}"}  # the scheme's case does not matter, as for an access key
    assert (await house.client.post(path, content=REQUEST, headers=spelled)).status_code == 200


@pytest.mark.parametrize("path", PATHS)
async def test_a_revoked_allocations_payment_key_is_401_and_nothing_is_forwarded(
    house: House, signer: FakeSigningTransport, path: str
) -> None:
    token, _ = await payment_key(house, house.one, "alice")
    revoked = await house.client.post(house.one.allocation("alice", "/revoke"), headers=house.one.admin)
    assert revoked.status_code == 200
    assert (await call(house, path, token)).status_code == 401
    assert signer.forwarded == []


async def test_a_paused_allocation_is_the_signers_to_refuse(
    house: House, signer: FakeSigningTransport
) -> None:
    """Pausing stops the payments at Batteries, which the signer asks before it signs (403). The
    clearinghouse keeps the key and forwards the call: it decides nothing about money itself."""
    token, _ = await payment_key(house, house.one, "alice")
    paused = await house.client.post(
        house.one.allocation("alice", "/status"), json={"status": "paused"}, headers=house.one.admin
    )
    assert paused.status_code == 200
    refusal = b'{"error":{"message":"signer auth rejected request with status 403"}}'
    signer.signer_answers.append(
        SignerAnswer(status_code=403, content=refusal, content_type="application/json")
    )
    answer = await call(house, PAYMENT, token)
    assert (answer.status_code, answer.content) == (403, refusal)
    assert len(signer.forwarded) == 1


async def test_a_payment_key_is_still_no_access_key_where_the_signers_paths_are_served(house: House) -> None:
    token, _ = await payment_key(house, house.one, "alice")
    for path in (
        house.one.path("/actors"),
        house.one.path("/usage"),
        "/v1/jobs",
        "/v1/clearinghouse/discovery",
    ):
        assert (await house.client.get(path, headers=bearer(token))).status_code == 401, path


# -- what passes through -------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 402, 403, 480, 481, 482, 500, 502, 503])
async def test_the_signers_own_answer_comes_back_whatever_its_status(
    house: House, signer: FakeSigningTransport, status: int
) -> None:
    """An empty allocation is the signer's 402, from Batteries; a paused one its 403; an expired
    challenge its 480. The SDK and the engine read each as they read a signer's, so each comes back with
    its status, its body and its content type, and the call is made once."""
    token, _ = await payment_key(house, house.one, "alice")
    body = json.dumps({"error": {"message": f"signer said {status}"}}).encode()
    for path in PATHS:
        signer.signer_answers.append(
            SignerAnswer(status_code=status, content=body, content_type="application/json; charset=utf-8")
        )
        answer = await call(house, path, token)
        assert (answer.status_code, answer.content) == (status, body), path
        assert answer.headers["content-type"] == "application/json; charset=utf-8"
    assert len(signer.forwarded) == 2  # one call each: nothing is sent twice


async def test_only_the_body_and_its_type_come_back_from_the_signer(
    house: House, signer: FakeSigningTransport
) -> None:
    token, _ = await payment_key(house, house.one, "alice")
    answer = await call(house, PAYMENT, token)
    assert set(answer.headers) <= {"content-type", "content-length", "cache-control"}
    assert answer.headers["cache-control"] == "no-store"


async def test_an_answer_that_holds_the_batteries_key_is_never_passed_on(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager
) -> None:
    """No signer echoes its bearer. If one ever did, the answer stops here, and since the call was made,
    its outcome is unknown."""
    token, allocation = await payment_key(house, house.one, "alice")
    lpg = batteries_key(manager, allocation)
    echo = json.dumps({"error": {"message": f"bad bearer {lpg}"}}).encode()
    signer.signer_answers.append(SignerAnswer(status_code=400, content=echo, content_type="text/plain"))
    answer = await call(house, PAYMENT, token)
    assert answer.status_code == 502 and answer.json()["code"] == "signer_outcome_unknown"
    assert lpg not in answer.text


# -- when the signer gives no answer --------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_a_signer_that_was_not_reached_and_one_that_gave_no_answer_are_told_apart(
    house: House, signer: FakeSigningTransport, manager: FakeAllocationManager, path: str
) -> None:
    """Nothing was sent, so nothing was signed: 503 with ``code: signer_not_reached``. The call left and
    no answer came, so it may have signed: 502 with ``code: signer_outcome_unknown``. An engine that
    pays through a clearinghouse retries after the first and checks after the second (D87)."""
    token, allocation = await payment_key(house, house.one, "alice")
    lpg = batteries_key(manager, allocation)

    signer.signer_answers.append(SignerNotReached("connection refused by http://signer.internal:8935"))
    not_reached = await call(house, path, token)
    assert not_reached.status_code == 503
    assert not_reached.json()["code"] == "signer_not_reached"
    assert not_reached.headers["retry-after"] == "10"

    signer.signer_answers.append(SignerOutcomeUnknown("no answer from http://signer.internal:8935 in 4 s"))
    unknown = await call(house, path, token)
    assert unknown.status_code == 502
    assert unknown.json()["code"] == "signer_outcome_unknown"
    assert "retry-after" not in unknown.headers

    for answer in (not_reached, unknown):
        assert answer.json()["error"]["message"]  # where the SDK reads a signer's words
        assert "signer.internal" not in answer.text  # the private signer's address stays private
        assert lpg not in answer.text and token not in answer.text
    assert len(signer.forwarded) == 2  # each request was handed over once, and never again


async def test_a_batteries_key_the_clearinghouse_cannot_read_sends_nothing(
    house: House, signer: FakeSigningTransport, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secret store cannot give the key (a wrong ``LPB_SECRET_KEY``): the clearinghouse is not ready,
    and nothing was sent. A key that is gone is a key that stopped: 401."""
    token, _ = await payment_key(house, house.one, "alice")
    secrets = house.engine.payments._secrets

    async def unreadable(_: object) -> str:
        raise InvalidRequest("secret could not be decrypted with the configured key")

    monkeypatch.setattr(secrets, "get", unreadable)
    answer = await call(house, PAYMENT, token)
    assert answer.status_code == 503 and answer.json()["code"] == "signer_not_reached"
    assert "decrypted" not in answer.text and signer.forwarded == []
    monkeypatch.undo()

    async with store.engine.begin() as conn:  # revoked just now: the secret went before the row changed
        await conn.execute(delete(t.secrets))
    assert (await call(house, PAYMENT, token)).status_code == 401
    assert signer.forwarded == []


# -- the edges -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_a_body_larger_than_the_signer_takes_is_refused_and_never_forwarded(
    house: House, signer: FakeSigningTransport, path: str
) -> None:
    """The signer reads at most 1 MiB and answers 400. The clearinghouse holds to the same bound, with
    the same status, whether the length is declared or the body is streamed."""
    token, _ = await payment_key(house, house.one, "alice")
    limit = 1 << 20
    fits = b'{"pad":"' + b"a" * (limit - 10) + b'"}'
    assert len(fits) == limit and (await call(house, path, token, fits)).status_code == 200

    too_large = await call(house, path, token, fits + b" ")
    assert too_large.status_code == 400 and "too large" in too_large.json()["error"]["message"]

    async def streamed() -> AsyncIterator[bytes]:
        for _ in range(17):
            yield b"a" * (1 << 16)

    chunked = await house.client.post(path, content=streamed(), headers=bearer(token))
    assert chunked.status_code == 400
    assert len(signer.forwarded) == 1  # the one that fit

    unauthenticated = await house.client.post(path, content=fits + b" ")
    assert unauthenticated.status_code == 401  # the credential is read first


@pytest.mark.parametrize("path", PATHS)
async def test_the_signers_paths_take_a_post_and_nothing_else(house: House, path: str) -> None:
    token, _ = await payment_key(house, house.one, "alice")
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert (await house.client.request(method, path, headers=bearer(token))).status_code == 405


async def test_no_key_is_in_any_log_line_and_each_call_is_logged(
    house: House,
    signer: FakeSigningTransport,
    manager: FakeAllocationManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token, allocation = await payment_key(house, house.one, "alice")
    lpg = batteries_key(manager, allocation)
    refused = SignerAnswer(status_code=402, content=b"{}", content_type="application/json")
    signer.signer_answers += [refused, SignerNotReached("refused"), SignerOutcomeUnknown("late")]
    with caplog.at_level(logging.DEBUG):
        statuses = [(await call(house, PAYMENT, token)).status_code for _ in range(4)]
        assert (await call(house, PAYMENT, "lpp_" + "x" * 43)).status_code == 401
    assert statuses == [402, 503, 502, 200]
    logged = "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)
    assert token not in logged and lpg not in logged
    calls = [record for record in caplog.records if getattr(record, "event", "") == "payments.signer_call"]
    assert [(record.outcome, record.status_code) for record in calls] == [  # type: ignore[attr-defined]
        ("answered", 402),
        ("not_reached", None),
        ("unknown", None),
        ("answered", 200),
    ]
    assert {(record.tenant_id, record.actor_id) for record in calls} == {(house.one.id, "alice")}  # type: ignore[attr-defined]
    assert {record.allocation_id for record in calls} == {allocation["allocation_id"]}  # type: ignore[attr-defined]


# -- when the routes exist -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport",
    [ScriptedStreamTransport(), FakeSigningTransport(pays=False)],
    ids=["a transport that forwards no signer call", "a transport with no signer"],
)
async def test_an_engine_that_cannot_forward_a_signer_call_serves_neither_path(
    make_engine: EngineFactory, transport: ScriptedStreamTransport
) -> None:
    async with open_house(make_engine, supply=EngineSupply(transport=transport)) as house:
        assert house.engine.payments.signing is None
        token, _ = await payment_key(house, house.one, "alice")
        for path in PATHS:
            assert (await house.client.post(path, content=REQUEST, headers=bearer(token))).status_code == 404


async def test_the_published_schema_names_both_paths_and_the_payment_key(house: House) -> None:
    schema = create_app(house.engine, manage_lifecycle=False).openapi()
    for path in PATHS:
        operation = schema["paths"][path]["post"]
        assert set(schema["paths"][path]) == {"post"}
        assert operation["security"] == [{"PaymentKey": []}]
        assert {"200", "401", "502", "503"} <= set(operation["responses"])
    scheme = schema["components"]["securitySchemes"]["PaymentKey"]
    assert (scheme["scheme"], scheme["bearerFormat"]) == ("bearer", "lpp")
