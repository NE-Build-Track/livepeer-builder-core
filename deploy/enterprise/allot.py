"""Allot one actor of this engine's tenant, through the engine's embedded API.

An engine in its ``work`` role serves no route that allots: the allocation routes are the clearinghouse
half's. The engine itself allots through ``engine.payments.allot``, which calls the clearinghouse with
the tenant admin key and keeps the actor's payment key in this engine's secret store, where its jobs
find it. This script does that once, in the engine's own container, from the engine's own settings:

    docker compose exec engine python /opt/enterprise/allot.py ACTOR_ID AMOUNT OPERATION_REF

It moves the amount from the tenant's grant to the actor's allocation at the clearinghouse. A repeat with
the same reference allots nothing more. Nothing secret is printed: the payment key is in no answer.
"""

import asyncio
import json
import sys
from decimal import Decimal

from livepeer_builder import BuilderEngine, EngineSettings
from livepeer_builder.contracts import ActorContext, AllotRequest, Money


async def allot(actor_id: str, amount: str, operation_ref: str) -> dict[str, object]:
    settings = EngineSettings.from_env()
    if settings.role != "work" or settings.clearinghouse_tenant_id is None:
        raise SystemExit("this script is for an engine in its work role (LPB_ROLE=work)")
    request = AllotRequest(
        tenant_id=settings.clearinghouse_tenant_id,
        actor_id=actor_id,
        amount=Money(amount=Decimal(amount), currency=settings.default_currency),
        operation_ref=operation_ref,
    )
    engine = await BuilderEngine.create(settings)  # built, never started: no loop runs and no job is touched
    try:
        admin = ActorContext(actor_id="allot-script", credential_kind="clearinghouse_admin")
        allotted = await engine.payments.allot(admin, request)
    finally:
        await engine.stop()
    return {
        "tenant_id": allotted.tenant_id,
        "actor_id": allotted.actor_id,
        "allocation_id": allotted.allocation_id,
        "status": allotted.status,
    }


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: allot.py ACTOR_ID AMOUNT OPERATION_REF")
    print(json.dumps(asyncio.run(allot(*sys.argv[1:])), indent=2))  # noqa: T201
