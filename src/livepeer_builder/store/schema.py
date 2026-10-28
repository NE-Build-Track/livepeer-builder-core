"""Engine-owned tables (prefix ``lpb_``), vendor-neutral across SQLite and PostgreSQL.

Conventions (docs/contracts/persistence.md):
- Money and fees are exact decimal strings, summed in Python. Never floats.
- Timestamps are timezone-aware UTC (``UTCDateTime`` restores the zone SQLite drops).
- Lists live in child tables when they are queried (payment sessions), JSON otherwise.
- Every change to this file needs an Alembic migration; tests/store/test_migrations.py checks drift.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Dialect,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.types import TypeDecorator

from livepeer_builder.utils import utc


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        return None if value is None else utc(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        del dialect
        return None if value is None else utc(value)


metadata = MetaData()
ID = 200  # length for opaque external identifiers
AMOUNT = 64  # length for decimal strings

# -- 0001: access and execution records --------------------------------------------------------------

access_keys = Table(
    "lpb_access_keys",
    metadata,
    Column("key_id", String(64), primary_key=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("kind", String(16), nullable=False),
    Column("actor_id", String(ID), nullable=False),
    Column("application_id", String(ID), nullable=False),
    Column("permissions", JSON, nullable=False),
    Column("label", String(ID), nullable=False, server_default=""),
    Column("created_at", UTCDateTime, nullable=False),
    Column("revoked_at", UTCDateTime, nullable=True),
)

jobs = Table(
    "lpb_jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("application_id", String(ID), nullable=False),
    Column("actor_id", String(ID), nullable=False),
    Column("operation_ref", String(ID), nullable=True),
    Column("request_fingerprint", String(64), nullable=True),
    Column("mode", String(16), nullable=False),
    Column("app", String(ID), nullable=False),
    Column("capability", String(ID), nullable=True),
    Column("model", String(ID), nullable=True),
    Column("state", String(16), nullable=False),
    Column("failure", JSON, nullable=True),
    Column("result", JSON, nullable=True),
    Column("usage", JSON, nullable=True),
    Column("created_at", UTCDateTime, nullable=False),
    Column("completed_at", UTCDateTime, nullable=True),
    Column("evidence_version", Integer, nullable=False, server_default=text("0")),  # +1 per evidence change
)
Index(
    "lpb_jobs_operation_ref",
    jobs.c.application_id,
    jobs.c.actor_id,
    jobs.c.operation_ref,
    unique=True,
    sqlite_where=text("operation_ref IS NOT NULL"),
    postgresql_where=text("operation_ref IS NOT NULL"),
)
Index("lpb_jobs_owner_created", jobs.c.application_id, jobs.c.actor_id, jobs.c.created_at)
Index("lpb_jobs_state", jobs.c.state)

attempts = Table(
    "lpb_attempts",
    metadata,
    Column("job_id", String(36), ForeignKey("lpb_jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("runner_url", Text, nullable=True),
    Column("orchestrator_url", Text, nullable=True),
    Column("allocation_id", String(ID), nullable=True),
    Column("payment_sent", Boolean, nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("status_code", Integer, nullable=True),
    Column("started_at", UTCDateTime, nullable=False),
    Column("ended_at", UTCDateTime, nullable=True),
)

payment_sessions = Table(
    "lpb_payment_sessions",
    metadata,
    Column("job_id", String(36), primary_key=True),
    Column("attempt_number", Integer, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("manifest_id", String(ID), nullable=False),
    Column("payment_session_id", String(ID), nullable=True),
    Column("match_status", String(16), nullable=False),
    Column("payment_state", String(16), nullable=False, server_default="paid"),  # D54
    ForeignKeyConstraint(
        ["job_id", "attempt_number"], ["lpb_attempts.job_id", "lpb_attempts.number"], ondelete="CASCADE"
    ),
)
Index("lpb_payment_sessions_manifest", payment_sessions.c.manifest_id)
Index("lpb_payment_sessions_session", payment_sessions.c.payment_session_id)

# -- 0002: payments and cost projections --------------------------------------------------------------

actors = Table(
    "lpb_actors",
    metadata,
    Column("application_id", String(ID), primary_key=True),
    Column("actor_id", String(ID), primary_key=True),
    Column("grant_id", String(ID), nullable=False),
    Column("allocation_id", String(ID), nullable=False, unique=True),
    Column("api_key_id", String(ID), nullable=False),
    Column("secret_ref", String(ID), nullable=False),  # a SecretRef, never the key itself
    Column("currency", String(3), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("label", String(ID), nullable=False, server_default=""),  # readable beside an opaque actor_id
)

# People an identity provider signed in as actors, before or after an allocation (#52, D48).
actor_sightings = Table(
    "lpb_actor_sightings",
    metadata,
    Column("application_id", String(ID), primary_key=True),
    Column("actor_id", String(ID), primary_key=True),
    Column("name", String(ID), nullable=False, server_default=""),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("last_seen_at", UTCDateTime, nullable=False),
)

secrets = Table(
    "lpb_secrets",
    metadata,
    Column("ref", String(64), primary_key=True),
    Column("ciphertext", Text, nullable=False),  # Fernet token; the key lives outside the database
    Column("created_at", UTCDateTime, nullable=False),
)

usage_rows = Table(
    "lpb_usage_rows",
    metadata,
    Column("id", String(ID), primary_key=True),
    Column("event_id", String(ID), nullable=True),
    Column("status", String(16), nullable=False),
    Column("manifest_id", String(ID), nullable=True),
    Column("allocation_id", String(ID), nullable=True),
    Column("payment_session_id", String(ID), nullable=True),
    Column("request_id", String(ID), nullable=True),
    Column("pipeline", String(ID), nullable=True),
    Column("computed_fee_eth", String(AMOUNT), nullable=True),
    Column("computed_fee_usd", String(AMOUNT), nullable=True),
    Column("currency", String(8), nullable=True),
    Column("created_at", UTCDateTime, nullable=False),
    Column("synced_at", UTCDateTime, nullable=False),
)
Index("lpb_usage_rows_allocation", usage_rows.c.allocation_id)
Index("lpb_usage_rows_session", usage_rows.c.payment_session_id)

cost_cursor = Table(
    "lpb_cost_cursor",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),  # always 1
    Column("resume_cursor", Text, nullable=True),
    Column("filters", JSON, nullable=False),
)

cost_projections = Table(
    "lpb_cost_projections",
    metadata,
    Column("job_id", String(36), ForeignKey("lpb_jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("fee_eth", String(AMOUNT), nullable=False),
    Column("fee_usd", String(AMOUNT), nullable=True),
    Column("row_count", Integer, nullable=False),
    Column("attempts", JSON, nullable=False, server_default=text("'[]'")),
    Column("first_observed_at", UTCDateTime, nullable=True),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("evidence_version", Integer, nullable=False, server_default=text("0")),  # the job's, when computed
)

VERSION_TABLE = "lpb_alembic_version"
