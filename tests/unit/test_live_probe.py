"""scripts/live_probe.py without a network (ISS-23, D61): the probe run against the engine in-process.

The live run itself is the operator's; these pin that its checks pass against a correct engine, that it
spends only with ``--yes`` and within its cap, and that a wrong answer fails it.
"""

import json
import runpy
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, Money, ProvisionRequest
from livepeer_builder.http import create_app
from livepeer_builder.testing import FakeAllocationManager, ScriptedTransport, runner
from tests.conftest import EngineFactory

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "live_probe.py"))
ProbeSettings: Any = SCRIPT["ProbeSettings"]


@pytest.fixture
async def stack(
    make_engine: EngineFactory, admin: ActorContext
) -> AsyncIterator[tuple[httpx.AsyncClient, BuilderEngine, str]]:
    """The engine behind its REST app, carol provisioned with 5 USD, and the bootstrap admin key."""
    engine = await make_engine()
    await engine.payments.provision(
        admin,
        ProvisionRequest(
            operation_ref="provision:default:carol", application_id="default", actor_id="carol",
            amount=Money(amount=Decimal("5"), currency="USD"), grant_id="grant_1",
        ),
    )  # fmt: skip
    token = (await engine.keys.bootstrap_admin()).token
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client, engine, token  # type: ignore[misc]


async def run(stack: tuple[httpx.AsyncClient, BuilderEngine, str], *, spend: bool, **fund: str) -> int:
    client, _, token = stack
    result: int = await SCRIPT["probe"](
        ProbeSettings("http://engine", token, **fund), spend=spend, client=client
    )
    return result


def test_without_a_stack_the_probe_says_so_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert SCRIPT["main"]([], env={"LPB_PROBE_URL": "https://engine.example"}) == 0
    assert "live probe skipped: set LPB_PROBE_URL and LPB_PROBE_ADMIN_KEY" in capsys.readouterr().out


def test_settings_come_from_lpb_probe_variables() -> None:
    env = {
        "LPB_PROBE_URL": "https://e.example/",
        "LPB_PROBE_ADMIN_KEY": "lpk_x",
        "LPB_PROBE_FUND_ACTOR": "carol",
    }
    assert SCRIPT["settings_from_env"](env) == ProbeSettings("https://e.example", "lpk_x", "carol", None)


@pytest.mark.parametrize(
    ("currency", "configured", "expected"),
    [
        ("USD", None, Decimal("0.01")),
        ("ETH", None, Decimal("0.000005")),
        ("USD", "0.05", Decimal("0.05")),
        ("USD", "0.06", None),  # more than a few cents
        ("ETH", "0.01", None),
        ("USD", "0", None),
        ("USD", "NaN", None),
        ("USD", "lots", None),
        ("EUR", None, None),
    ],
)
def test_the_fund_amount_stays_within_a_few_cents(
    currency: str, configured: str | None, expected: Decimal | None
) -> None:
    assert SCRIPT["fund_amount"](currency, configured) == expected


async def test_every_check_passes_against_the_engine_and_the_fund_applies_once(
    stack: tuple[httpx.AsyncClient, BuilderEngine, str], manager: FakeAllocationManager,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    _, engine, _ = stack
    allocation = (await engine.store.get_actor("default", "carol")).allocation_id  # type: ignore[union-attr]
    assert await run(stack, spend=True, fund_actor="carol") == 0
    out = capsys.readouterr().out
    assert "FAIL  " not in out and "SKIP  " not in out and ": 26 passed, 0 failed, 0 skipped" in out, out
    assert manager.allocations[allocation].granted == Decimal("5.01")  # one cent, applied once
    assert manager.calls.count("fund_allocation") == 1
    probe_keys = [
        key for key in await engine.store.list_access_keys(None) if key.actor_id.startswith("lpb-probe-")
    ]
    assert len(probe_keys) == 3 and all(key.revoked_at is not None for key in probe_keys)


async def test_without_yes_the_probe_spends_nothing(
    stack: tuple[httpx.AsyncClient, BuilderEngine, str], manager: FakeAllocationManager,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    assert await run(stack, spend=False, fund_actor="carol") == 0
    assert "SKIP  a fund repeated under one operation_ref applies once: pass --yes" in capsys.readouterr().out
    assert "fund_allocation" not in manager.calls


async def test_a_fund_above_the_cap_is_refused_before_any_call(
    stack: tuple[httpx.AsyncClient, BuilderEngine, str], manager: FakeAllocationManager,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    assert await run(stack, spend=True, fund_actor="carol", fund_amount="1") == 1
    assert "FAIL  a fund repeated under one operation_ref applies once: refused" in capsys.readouterr().out
    assert "fund_allocation" not in manager.calls


async def test_an_engine_that_answers_wrongly_fails_the_probe(capsys: pytest.CaptureFixture[str]) -> None:
    def everything_allowed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(everything_allowed), base_url="http://e"
    ) as client:
        assert await SCRIPT["probe"](ProbeSettings("http://e", "lpk_admin"), spend=False, client=client) == 1
    assert "FAIL  issue a key (actor): 200" in capsys.readouterr().out


async def test_a_probe_app_that_some_runner_advertises_sends_no_job(
    make_engine: EngineFactory, transport: ScriptedTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    """In per_application mode the shared key pays for any actor, so a job for an advertised app would be
    paid on the POST itself. The probe's app is its own run's, and it checks the catalog first: an
    advertised app sends no job at all (#83 review)."""
    engine = await make_engine(
        runners=(runner(app=SCRIPT["probe_app"]("lpb-probe-test")),),
        allocation_mode="per_application",
        shared_payment_key="lpg_shared",
    )
    transport.steps = [ScriptedTransport.ok(), ScriptedTransport.ok()]
    token = (await engine.keys.bootstrap_admin()).token
    app = create_app(engine, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        settings = ProbeSettings("http://engine", token)
        assert await SCRIPT["probe"](settings, spend=False, client=client, run="lpb-probe-test") == 1
    assert "FAIL  no runner advertises the probe's app" in capsys.readouterr().out
    assert transport.calls == []  # no runner was called, so nothing was paid
    assert not [
        job for job in await engine.store.list_jobs("default", None) if job.actor_id.startswith("lpb-probe-")
    ]


class _SecondJobRan(httpx.AsyncBaseTransport):
    """The engine, except the second job read answers as a job that ran and was paid."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner, self._reads = inner, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/jobs/") and path.count("/") == 3:
            self._reads += 1
            if self._reads == 2:
                await response.aread()
                job = json.loads(response.content) | {"state": "succeeded", "failure": None}
                job["attempts"] = [{"payment_sent": True}]
                return httpx.Response(200, json=job, request=request)
        return response


async def test_the_second_actor_s_job_must_also_end_unpaid(
    stack: tuple[httpx.AsyncClient, BuilderEngine, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both probe jobs are read; a second one that ran and paid fails the probe (#83 review)."""
    _, engine, token = stack
    transport = _SecondJobRan(httpx.ASGITransport(app=create_app(engine, manage_lifecycle=False)))
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        assert await SCRIPT["probe"](ProbeSettings("http://engine", token), spend=False, client=client) == 1
    assert "FAIL  another actor's operation_ref is a new job" in capsys.readouterr().out


class _BreaksOnJobs(httpx.AsyncBaseTransport):
    """The engine, except one request to /v1/jobs breaks: the first POST times out, or the second GET (the
    other actor's job list, read as JSON) answers 200 with a body that is not JSON."""

    def __init__(self, inner: httpx.AsyncBaseTransport, failure: str) -> None:
        self._inner, self._failure, self._gets = inner, failure, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/jobs":
            if self._failure == "timeout" and request.method == "POST":
                self._failure = ""
                raise httpx.ReadTimeout("no answer", request=request)
            if self._failure == "not-json" and request.method == "GET":
                self._gets += 1
                if self._gets == 2:
                    return httpx.Response(200, content=b"<html>not json</html>", request=request)
        return await self._inner.handle_async_request(request)


@pytest.mark.parametrize("failure", ["timeout", "not-json"])
async def test_the_probe_revokes_its_keys_even_when_a_check_breaks(
    stack: tuple[httpx.AsyncClient, BuilderEngine, str], failure: str
) -> None:
    """Cleanup is on the way out whatever broke: an HTTP error or a body that is not JSON (#83)."""
    _, engine, token = stack
    app = create_app(engine, manage_lifecycle=False)
    transport = _BreaksOnJobs(httpx.ASGITransport(app=app), failure)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        assert await SCRIPT["probe"](ProbeSettings("http://engine", token), spend=False, client=client) == 1
    keys = [key for key in await engine.store.list_access_keys(None) if key.actor_id.startswith("lpb-probe-")]
    assert len(keys) == 3 and all(key.revoked_at is not None for key in keys)


async def test_a_201_without_a_key_id_is_a_failed_issue(capsys: pytest.CaptureFixture[str]) -> None:
    """A token with no key id is not a key the probe can revoke, nor the previous one (#83)."""

    def no_key_id(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/admin/keys" and request.method == "POST":
            return httpx.Response(201, json={"token": "lpk_new"})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_key_id), base_url="http://e") as client:
        assert await SCRIPT["probe"](ProbeSettings("http://e", "lpk_admin"), spend=False, client=client) == 1
    assert "FAIL  issue a key (actor): 201, no key id" in capsys.readouterr().out
