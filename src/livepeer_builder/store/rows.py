"""Row-to-contract mappings shared by the SQL store."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from livepeer_builder.contracts import AttemptCost, Fees, JobCost


def projection(row: Any) -> JobCost:
    fee_usd = row["fee_usd"]
    return JobCost(
        job_id=UUID(row["job_id"]),
        status=row["status"],
        fees=Fees(
            fee_eth=Decimal(row["fee_eth"]),
            fee_usd=None if fee_usd is None else Decimal(fee_usd),
            row_count=row["row_count"],
        ),
        attempts=tuple(AttemptCost.model_validate(attempt) for attempt in row["attempts"]),
        first_observed_at=row["first_observed_at"],
        updated_at=row["updated_at"],
    )
