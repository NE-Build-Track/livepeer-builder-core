"""Give each provisioned actor a readable label (#52, D46).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26

With an identity provider, an actor_id is the provider's immutable subject (sub), often an opaque UUID.
The label is the name an operator gives the actor at provisioning, so an admin can tell actors apart.
Existing actors get an empty label.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lpb_actors", sa.Column("label", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("lpb_actors", "label")
