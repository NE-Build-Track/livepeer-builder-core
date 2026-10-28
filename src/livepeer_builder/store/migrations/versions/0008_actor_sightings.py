"""Remember people an identity provider signed in as actors (#52, D48).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26

Under JWT bearer an actor is the provider's subject (sub). The engine records each person the first time
their token reaches it, with the provider's display name, so an operator can provision them by name
instead of typing an opaque sub. The allocation stays on lpb_actors; this table only says who was seen.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from livepeer_builder.store.schema import UTCDateTime

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lpb_actor_sightings",
        sa.Column("application_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("first_seen_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lpb_actor_sightings")
