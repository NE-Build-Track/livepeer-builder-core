"""Job contracts: requests, attempts, failures, results and measured usage (docs/contracts/engine.md)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from livepeer_builder.contracts.common import Contract
from livepeer_builder.contracts.http import request_headers

ExecutionMode = Literal["immediate", "async"]  # "async" is defined in M2, implemented in M4 (ISS-26)

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
SessionMatch = Literal["unmatched", "matched", "ambiguous"]


class JobRequest(Contract):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
    app: str = Field(min_length=1)  # the runner app a discovery entry advertises
    path: str = ""  # appended to the runner URL; must start with "/" when set
    method: HttpMethod = "POST"
    payload: dict[str, Any] | None = None
    raw_body: bytes | None = Field(default=None, strict=True, max_length=8388608, repr=False)
    content_type: str | None = Field(default=None, min_length=1, max_length=256)
    headers: tuple[tuple[str, str], ...] = Field(default=(), repr=False)

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return request_headers(value)

    @field_validator("content_type")
    @classmethod
    def _media_type(cls, value: str | None) -> str | None:
        if value is not None and any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("Invalid content type")
        return value

    operation_ref: str | None = Field(default=None, max_length=200)  # the caller's retry key
    capability: str | None = None  # label only in M2; resolution is a later contract
    model: str | None = None
    mode: ExecutionMode = "immediate"
    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_s: float = Field(default=300.0, gt=0, le=3600)  # the whole job, from its start to the last byte
    # streams only (D35): request → first body chunk (capped at timeout_s), and the longest gap between
    # chunks once data flows. None turns the idle limit off.
    first_byte_timeout_s: float = Field(default=60.0, gt=0, le=3600)
    idle_timeout_s: float | None = Field(default=60.0, gt=0, le=3600)

    @model_validator(mode="after")
    def _first_byte_within_total(self) -> "JobRequest":
        if self.raw_body is not None and self.payload is not None:
            raise ValueError("Use either payload or raw_body")
        if self.raw_body is not None and self.content_type is None:
            raise ValueError("raw_body requires an explicit content_type")
        if "first_byte_timeout_s" in self.model_fields_set and self.first_byte_timeout_s > self.timeout_s:
            raise ValueError("first_byte_timeout_s cannot be longer than timeout_s")
        return self


# "payment_refused": the signer refused to pay *this orchestrator* before anything was sent (bad payment
# params, price above the session ceiling). Safe to fail over. "payment": any other payment failure.
# "abandoned": the caller stopped reading a stream before the runner finished
# (docs/contracts/engine.md#streaming).
AttemptOutcome = Literal[
    "in_flight",
    "succeeded",
    "refused",
    "unreachable",
    "payment_refused",
    "payment",
    "timeout",
    "http",
    "abandoned",
]


class SignedSession(Contract):
    """One session the signer signed for an attempt.

    ``manifest_id`` is the orchestrator's label from the 402 challenge. Cost sync resolves it to
    the Batteries ``payment_session_id`` on ``(allocation_id, manifest_id)``. ``match`` stays
    ``unmatched`` until that resolution, becomes ``matched`` when exactly one Batteries session
    and one log entry share the key, and ``ambiguous`` when they do not.
    """

    manifest_id: str
    payment_session_id: str | None = None
    match: SessionMatch = "unmatched"


class Attempt(Contract):
    """One runner call. Written as ``in_flight`` before the call, updated after it."""

    job_id: UUID
    number: int
    runner_url: str | None
    orchestrator_url: str | None
    allocation_id: str | None  # the allocation whose key paid; None when the call was unpaid
    sessions: tuple[SignedSession, ...]  # one entry per signed session, in signing order
    payment_sent: bool
    outcome: AttemptOutcome
    status_code: int | None
    started_at: datetime
    ended_at: datetime | None


FailureKind = Literal[
    "no_offering",
    "no_capacity",
    "not_provisioned",
    "refused",
    "unreachable",
    "payment",
    "timeout",
    "runner_rejected",
    "runner_error",
    "interrupted",
    "abandoned",
]


class Failure(Contract):
    kind: FailureKind
    message: str
    status_code: int | None = None  # the runner's own status, when it answered
    body: str | None = None  # the runner's own words, bounded
    payment_sent: bool = False


JobState = Literal["queued", "running", "succeeded", "failed", "uncertain"]  # "queued" is reserved (async)


class ResultRef(Contract):
    """Where a result lives. M2 keeps immediate results inline (not retained) and records their shape."""

    storage: Literal["inline", "stored"]  # "stored" is reserved for async results (ISS-26)
    content_type: str
    size_bytes: int
    sha256: str
    location: str | None = None


class MeasuredUsage(Contract):
    """Work done, in units the capability defines. Retains its source and is never equated with cost."""

    quantities: dict[str, int]
    source: Literal["app_reported", "engine_meter"]
    reported_at: datetime


class Job(Contract):
    id: UUID
    actor_id: str
    application_id: str
    operation_ref: str | None
    mode: ExecutionMode
    app: str
    capability: str | None
    model: str | None
    state: JobState
    attempts: tuple[Attempt, ...]
    failure: Failure | None
    result: ResultRef | None
    usage: MeasuredUsage | None
    created_at: datetime
    completed_at: datetime | None


class RunnerReply(Contract):
    """What a RunnerTransport returns for one successful call."""

    status_code: int
    content: bytes
    content_type: str
    data: dict[str, Any] | None
    manifest_ids: tuple[str, ...]  # one per signed session, in signing order
    payment_sent: bool


class JobResult(Contract):
    job: Job
    status_code: int
    content: bytes
    content_type: str
    data: dict[str, Any] | None
