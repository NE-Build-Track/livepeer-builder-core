"""scripts/live_probe.py without a network (ISS-23, D61): the probe run against the engine in-process.

The live run itself is the operator's; these pin that its checks pass against a correct engine, that it
spends only with ``--yes`` and within its cap, and that a wrong answer fails it.
"""

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
from livepeer_builder.testing import FakeAllocationManager
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
    assert "FAIL  " not in out and "SKIP  " not in out and ": 25 passed, 0 failed, 0 skipped" in out, out
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
