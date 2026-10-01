"""watch interval

Revision ID: 3f1a7c2d9b10
Revises: fa11c2698fed
Create Date: 2026-10-01 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f1a7c2d9b10"
down_revision: str | Sequence[str] | None = "fa11c2698fed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("watchlist", schema=None) as batch_op:
        batch_op.add_column(sa.Column("interval_min", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("watchlist", schema=None) as batch_op:
        batch_op.drop_column("checked_at")
        batch_op.drop_column("interval_min")
