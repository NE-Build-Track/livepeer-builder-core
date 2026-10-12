"""Property-based tests for the money rules: invariants that must hold for every input, not just examples."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from livepeer_builder.contracts import Attempt, Job, JobRequest, UsageRow
from livepeer_builder.services.costs import add_fees, classify, fees_of
from livepeer_builder.services.jobs import request_fingerprint
from livepeer_builder.store.sql import _usage_row, _usage_values

NOW = datetime(2026, 10, 12, tzinfo=UTC)

# Batteries amounts: exact decimals with up to 18 fractional digits, never floats.
amounts = st.decimals(
    min_value=Decimal(0), max_value=Decimal(10**6), places=18, allow_nan=False, allow_infinity=False
)


@st.composite
def usage_rows(draw: st.DrawFn, sessions: tuple[str, ...] = ("s1", "s2", "s3", "orphan")) -> UsageRow:
    return UsageRow(
        id=str(uuid4()),
        event_id=None,
        status=draw(
            st.sampled_from(["applied", "applied", "applied", "quarantined", "ignored", "duplicate"])
        ),
        manifest_id=draw(st.none() | st.text(max_size=8)),
        allocation_id="alloc",
        payment_session_id=draw(st.sampled_from(sessions)),
        request_id=None,
        pipeline=None,
        computed_fee_eth=draw(amounts),
        computed_fee_usd=draw(st.none() | amounts),
        currency="usd",
        created_at=NOW,
    )


def job_with(outcomes: list[str], state: str) -> Job:
    job_id = uuid4()
    attempts = tuple(
        Attempt(
            job_id=job_id, number=n, runner_url="r", orchestrator_url=None, auth_ids=(f"s{n}",), manifest_id=None,
            payment_sent=True, outcome=outcome, status_code=None, started_at=NOW, ended_at=NOW,  # type: ignore[arg-type]
        )
        for n, outcome in enumerate(outcomes, start=1)
    )  # fmt: skip
    return Job(
        id=job_id, actor_id="a", application_id="default", operation_ref=None, mode="immediate", app="x",
        capability=None, model=None, state=state, attempts=attempts, failure=None, result=None, usage=None,  # type: ignore[arg-type]
        created_at=NOW, completed_at=NOW,
    )  # fmt: skip


@given(rows=st.lists(usage_rows(), min_size=1, max_size=30), split=st.integers(min_value=0, max_value=30))
def test_fees_add_up_however_the_rows_are_split(rows: list[UsageRow], split: int) -> None:
    left, right = rows[:split], rows[split:]
    combined = add_fees([fees_of(left), fees_of(right)])
    whole = fees_of(rows)
    assert (combined.fee_eth, combined.fee_usd, combined.row_count) == (
        whole.fee_eth,
        whole.fee_usd,
        whole.row_count,
    )


@given(
    rows=st.lists(usage_rows(), max_size=30),
    outcomes=st.lists(
        st.sampled_from(["succeeded", "refused", "http", "timeout", "in_flight"]), min_size=1, max_size=3
    ),
    state=st.sampled_from(["succeeded", "failed", "uncertain"]),
)
def test_every_attributed_charge_is_counted_exactly_once(
    rows: list[UsageRow], outcomes: list[str], state: str
) -> None:
    job = job_with(outcomes, state)
    cost = classify(job, rows)
    own_sessions = {auth_id for attempt in job.attempts for auth_id in attempt.auth_ids}
    expected = fees_of(row for row in rows if row.payment_session_id in own_sessions)
    assert sum((a.fees.fee_eth for a in cost.attempts), Decimal(0)) == expected.fee_eth == cost.fees.fee_eth
    assert sum(a.fees.row_count for a in cost.attempts) == expected.row_count
    assert all(
        a.cost_class == "pending" for a in cost.attempts if a.fees.row_count == 0
    )  # missing is never zero


@given(row=usage_rows())
def test_money_survives_the_store_exactly(row: UsageRow) -> None:
    stored = _usage_values(row)
    assert all(
        isinstance(stored[k], str | None) for k in ("computed_fee_eth", "computed_fee_usd")
    )  # never floats
    assert _usage_row(stored) == row


@settings(max_examples=50)
@given(
    payload=st.dictionaries(st.text(max_size=5), st.integers() | st.text(max_size=5), max_size=4),
    other=st.dictionaries(st.text(max_size=5), st.integers() | st.text(max_size=5), max_size=4),
    timeout=st.floats(min_value=1, max_value=3600),
)
def test_fingerprint_tracks_the_request_not_its_limits(payload: dict, other: dict, timeout: float) -> None:  # type: ignore[type-arg]
    base = JobRequest(app="a/b", path="/x", payload=payload)
    assert request_fingerprint(base) == request_fingerprint(
        base.model_copy(update={"timeout_s": timeout, "max_attempts": 7, "operation_ref": "retry"})
    )
    if other != payload:
        assert request_fingerprint(base) != request_fingerprint(base.model_copy(update={"payload": other}))
