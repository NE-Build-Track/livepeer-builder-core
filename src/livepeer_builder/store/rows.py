"""Row-to-contract mappings shared by the SQL store."""

from collections.abc import Iterable
from decimal import Decimal
from typing import Any
from uuid import UUID

from livepeer_builder.contracts import AttemptCost, Fees, JobCost, UsageRow


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


def usage_row(row: Any) -> UsageRow:
    data = {key: row[key] for key in UsageRow.model_fields}
    return UsageRow.model_validate(data)


def chunks(values: list[str], size: int = 500) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
