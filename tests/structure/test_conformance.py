"""The engine and FakeEngine satisfy the facade Protocols (livepeer_builder.api).

Protocols are structural: nothing in ``BuilderEngine`` says it implements ``ActorApi``, so a renamed
member or a changed signature would only show up in a consumer. mypy checks this file (pyproject
``[tool.mypy] files``), so such a change fails the type check here; the tests run the same functions.
"""

from livepeer_builder import ActorApi, AdminApi, BuilderEngine, EngineApi
from livepeer_builder.testing import FakeEngine, make_engine


def engine_as_actor_api(engine: BuilderEngine) -> ActorApi:
    return engine


def engine_as_admin_api(engine: BuilderEngine) -> AdminApi:
    return engine


def engine_as_engine_api(engine: BuilderEngine) -> EngineApi:
    return engine


def fake_as_actor_api(fake: FakeEngine) -> ActorApi:
    return fake


async def test_the_engine_offers_every_role() -> None:
    async with make_engine() as engine:
        roles = (engine_as_actor_api(engine), engine_as_admin_api(engine), engine_as_engine_api(engine))
        assert all(role is engine for role in roles)


def test_the_fake_offers_the_actor_api() -> None:
    fake = FakeEngine()
    assert fake_as_actor_api(fake) is fake
