"""Contracts defined in M2 and implemented later: the events feed (M3) and persistent sessions (M4).

They are part of the public contract now so consumers can design against them. Calling the
services that serve them raises ``NotImplementedInMilestone``. See docs/contracts/events.md and
docs/contracts/engine.md#persistent-sessions.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from livepeer_builder.contracts.common import Contract

EventType = Literal["job.created", "job.finished", "usage.reported", "cost.observed", "cost.corrected"]


class EngineEvent(Contract):
    sequence: int  # the cursor; strictly increasing
    id: str  # stable; consumers drop duplicates on it
    type: EventType
    schema_version: int
    occurred_at: datetime
    application_id: str
    actor_id: str
    job_id: UUID
    operation_ref: str | None
    payload: dict[str, Any]  # the Job for job events; the JobCost (with cost classes) for cost events


class EventPage(Contract):
    events: tuple[EngineEvent, ...]
    next_cursor: int


class SessionRequest(Contract):
    app: str
    operation_ref: str | None = None
    max_duration_s: int = Field(default=3600, gt=0)
    idle_timeout_s: int = Field(default=300, gt=0)


SessionState = Literal["opening", "open", "closed", "failed"]


class Session(Contract):
    """A persistent runner session. The engine proxies all traffic; clients never see the app URL."""

    id: UUID
    job_id: UUID
    application_id: str
    actor_id: str
    app: str
    state: SessionState
    proxy_path: str  # engine-relative path that forwards HTTP, SSE and WebSocket traffic
    opened_at: datetime | None
    closed_at: datetime | None
