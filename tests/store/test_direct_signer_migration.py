"""The direct signer migration removes legacy tokens without losing other records."""

from sqlalchemy import inspect, text

from livepeer_builder.store import create_engine, upgrade
from livepeer_builder.store.migrate import downgrade


async def test_upgrade_and_downgrade_retire_tokens(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        await upgrade(engine, "0015")
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO lpb_payment_keys VALUES ('t', 'a', 'allocation', 'hash', '2026-11-19')")
            )
        await upgrade(engine)
        async with engine.connect() as conn:
            assert "lpb_payment_keys" not in await conn.run_sync(lambda c: inspect(c).get_table_names())
        await downgrade(engine, "0015")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM lpb_payment_keys"))).scalar_one() == 0
        await upgrade(engine)
    finally:
        await engine.dispose()
