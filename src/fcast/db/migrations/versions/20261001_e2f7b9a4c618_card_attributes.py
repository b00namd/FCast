"""card attributes

Revision ID: e2f7b9a4c618
Revises: c5d8a1e6f240
Create Date: 2026-10-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f7b9a4c618"
down_revision: str | Sequence[str] | None = "c5d8a1e6f240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attributes", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.drop_column("attributes")
