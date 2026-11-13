"""#114 C12: one runner's text cannot take the catalog down for everyone.

A lone surrogate in a discovery ``app`` or ``url`` is valid JSON and no valid Unicode text: the catalog
could not be written as JSON, so ``GET /v1/offerings`` answered 500 for every application. A NUL, or an
``app`` longer than 200 characters, is text the store cannot keep on a job. Such a runner is skipped and
counted in ``discovery.inventory_skipped``; an optional text field with such text reads as absent and the
runner stays. Nothing is rewritten.

These tests carry into ISS-48 (see ``discovery_wire``).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from livepeer_builder import BuilderEngine
from livepeer_builder.http import create_app
from tests.unit.discovery_wire import ALICE, ORCHESTRATOR, document, record, skipped, wire_engine

SURROGATE = "a\ud800b"
NUL = "a\x00b"
CONTROL = "a\x1fb"
C1_CONTROL = "a\x85b"
INVALID = {"lone surrogate": SURROGATE, "NUL": NUL, "control": CONTROL, "C1 control": C1_CONTROL}
GOOD = record()


@asynccontextmanager
async def rest(engine: BuilderEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The REST service as the engine's first admin. A failure is its 500, not a raise."""
    admin = await engine.keys.bootstrap_admin()
    transport = httpx.ASGITransport(
        app=create_app(engine, manage_lifecycle=False), raise_app_exceptions=False
    )
    headers = {"Authorization": f"Bearer {admin.token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://engine", headers=headers) as client:
        yield client


@pytest.mark.parametrize(
    "bad",
    [
        *(pytest.param(record(app=text), id=f"app: {name}") for name, text in INVALID.items()),
        pytest.param(record(app="x" * 201), id="app: 201 characters"),
        pytest.param(record(app="bad/app", url=f"{ORCHESTRATOR}/apps/\ud800"), id="url: lone surrogate"),
        pytest.param(record(app="bad/app", url=f"{ORCHESTRATOR}/apps/\x00"), id="url: NUL"),
        pytest.param(record(app="bad/app", url=f"{ORCHESTRATOR}/apps/\x1f"), id="url: control"),
        pytest.param(record(app="bad/app", url=f"{ORCHESTRATOR}/apps/\x85"), id="url: C1 control"),
    ],
)
async def test_a_runner_whose_app_or_url_is_not_storable_text_is_skipped_and_counted(
    bad: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        async with wire_engine(document(bad, GOOD)) as engine, rest(engine) as client:
            catalog = await client.get("/v1/offerings")
            everything = engine.discovery.snapshot.runners
    assert catalog.status_code == 200
    assert [offering["app"] for offering in catalog.json()] == ["good/app"]
    assert [(runner.app, runner.url) for runner in everything] == [(GOOD["app"], GOOD["url"])]
    assert skipped(caplog.records) == [(0, 1)]


async def test_an_app_of_exactly_200_characters_is_kept() -> None:
    async with wire_engine(document(record(app="x" * 200))) as engine, rest(engine) as client:
        catalog = await client.get("/v1/offerings")
    assert [offering["app"] for offering in catalog.json()] == ["x" * 200]


@pytest.mark.parametrize("field", ["mode", "version", "metadata"])
@pytest.mark.parametrize("text", INVALID.values(), ids=INVALID.keys())
async def test_an_optional_text_field_with_invalid_text_reads_as_absent_and_the_runner_stays(
    field: str, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        async with wire_engine(document(record(**{field: text}))) as engine, rest(engine) as client:
            catalog = await client.get("/v1/offerings")
            listed = await client.get("/v1/runners", params={"app": "good/app"})
            (kept,) = engine.discovery.runners(ALICE, "good/app")
    assert (catalog.status_code, listed.status_code) == (200, 200)
    (offering,) = catalog.json()
    assert (offering["app"], offering["runner_count"], offering["modes"]) == ("good/app", 1, [])
    assert getattr(kept, field) is None
    assert listed.json()[0][field] is None
    assert skipped(caplog.records) == []


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"price_info": {"price": 1, "currency": "wei\ud800", "unit": "fixed"}}, id="currency"),
        pytest.param({"price_info": {"price": 1, "currency": "wei", "unit": "fixed\ud800"}}, id="unit"),
        pytest.param({"gpu": {"id": "GPU-\ud800", "name": "RTX\ud800", "vram_mb": 24564}}, id="gpu"),
    ],
)
async def test_invalid_text_in_a_price_or_a_gpu_reads_as_absent_and_the_runner_stays(
    fields: dict[str, Any],
) -> None:
    """The same 500: a rate's currency and unit are in the catalog, and a GPU's names in the runner list."""
    async with wire_engine(document(record(**fields))) as engine, rest(engine) as client:
        catalog = await client.get("/v1/offerings")
        listed = await client.get("/v1/runners", params={"app": "good/app"})
    assert (catalog.status_code, listed.status_code) == (200, 200)
    (offering,) = catalog.json()
    assert (offering["runner_count"], offering["rate_low"]) == (1, None)  # no rate: its text was not read
    (runner,) = listed.json()
    assert runner["url"] == GOOD["url"]
    assert "ud800" not in listed.text


WHITESPACE = {"tab": "a\tb", "newline": '{\n "model_id": "m"\n}', "carriage return": "a\r\nb"}


@pytest.mark.parametrize("field", ["mode", "version", "metadata"])
@pytest.mark.parametrize("text", WHITESPACE.values(), ids=WHITESPACE.keys())
async def test_a_tab_or_a_line_break_in_an_optional_text_field_is_kept_as_written(
    field: str, text: str
) -> None:
    """A tab, a newline and a carriage return can be stored and served, so an optional field keeps them:
    metadata written as JSON over several lines is still metadata. ``app`` and ``url`` stay strict."""
    async with wire_engine(document(record(**{field: text}))) as engine, rest(engine) as client:
        listed = await client.get("/v1/runners", params={"app": "good/app"})
        (kept,) = engine.discovery.runners(ALICE, "good/app")
    assert listed.status_code == 200
    assert getattr(kept, field) == text
    assert listed.json()[0][field] == text


async def test_a_rate_whose_currency_ends_in_a_newline_is_still_read() -> None:
    """The rate reader strips such a stray newline. Reading the field as absent made the runner unpayable."""
    stray = {"price_info": {"price": 1, "currency": "wei\n", "unit": "fixed"}}
    async with wire_engine(document(record(**stray))) as engine, rest(engine) as client:
        catalog = await client.get("/v1/offerings")
    (offering,) = catalog.json()
    assert (offering["runner_count"], offering["rate_low"] is not None) == (1, True)


async def test_valid_text_is_kept_as_written() -> None:
    """Nothing is rewritten: text outside ASCII, and a space, stay exactly as advertised."""
    advertised = record(app="modèle/图像 v2", mode="single-shot", version="1.2.3 β", metadata='{"名":"é"}')
    async with wire_engine(document(advertised)) as engine, rest(engine) as client:
        listed = await client.get("/v1/runners", params={"app": advertised["app"]})
    (runner,) = listed.json()
    assert {name: runner[name] for name in ("app", "mode", "version", "metadata")} == {
        name: advertised[name] for name in ("app", "mode", "version", "metadata")
    }
