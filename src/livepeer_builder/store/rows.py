"""Row-to-contract mappings shared by the SQL store."""

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from livepeer_builder.contracts import (
    Attempt,
    AttemptCost,
    Failure,
    Fees,
    Job,
    JobCost,
    MeasuredUsage,
    ResultRef,
    SignedSession,
    UsageRow,
)
from livepeer_builder.store import schema as t


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


async def load_jobs(conn: AsyncConnection, rows: Sequence[Any]) -> list[Job]:
    """Jobs from their rows, with each attempt and its signed sessions, read in chunks."""
    ids = [row["id"] for row in rows]
    attempts_by_job: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in ids}
    sessions_by_attempt: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for chunk in chunks(ids):
        found = await conn.execute(
            select(t.attempts).where(t.attempts.c.job_id.in_(chunk)).order_by(t.attempts.c.number)
        )
        for attempt in found.mappings():
            attempts_by_job[attempt["job_id"]].append(dict(attempt))
        sessions = await conn.execute(
            select(t.payment_sessions)
            .where(t.payment_sessions.c.job_id.in_(chunk))
            .order_by(t.payment_sessions.c.seq)
        )
        for session in sessions.mappings():
            key = (session["job_id"], session["attempt_number"])
            sessions_by_attempt.setdefault(key, []).append(dict(session))
    jobs: list[Job] = []
    for row in rows:
        attempts = tuple(
            Attempt(
                job_id=UUID(a["job_id"]),
                number=a["number"],
                runner_url=a["runner_url"],
                orchestrator_url=a["orchestrator_url"],
                allocation_id=a["allocation_id"],
                sessions=tuple(
                    SignedSession(
                        manifest_id=session["manifest_id"],
                        payment_session_id=session["payment_session_id"],
                        match=session["match_status"],
                        payment_state=session["payment_state"],
                    )
                    for session in sessions_by_attempt.get((a["job_id"], a["number"]), ())
                ),
                payment_sent=bool(a["payment_sent"]),
                outcome=a["outcome"],
                status_code=a["status_code"],
                started_at=a["started_at"],
                ended_at=a["ended_at"],
            )
            for a in attempts_by_job[row["id"]]
        )
        jobs.append(
            Job(
                id=UUID(row["id"]),
                actor_id=row["actor_id"],
                application_id=row["application_id"],
                operation_ref=row["operation_ref"],
                mode=row["mode"],
                app=row["app"],
                capability=row["capability"],
                model=row["model"],
                state=row["state"],
                attempts=attempts,
                failure=Failure.model_validate(row["failure"]) if row["failure"] else None,
                result=ResultRef.model_validate(row["result"]) if row["result"] else None,
                usage=MeasuredUsage.model_validate(row["usage"]) if row["usage"] else None,
                created_at=row["created_at"],
                completed_at=row["completed_at"],
            )
        )
    return jobs
