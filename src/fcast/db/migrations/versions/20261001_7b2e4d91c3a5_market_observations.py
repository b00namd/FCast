"""market observations

Revision ID: 7b2e4d91c3a5
Revises: 3f1a7c2d9b10
Create Date: 2026-10-01 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b2e4d91c3a5"
down_revision: str | Sequence[str] | None = "3f1a7c2d9b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "market_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum("console", "pc", name="platform", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("listings_csv", sa.String(length=120), nullable=False),
        sa.Column("range_max", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
            name=op.f("fk_market_observations_player_id_players"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_observations")),
    )
    with op.batch_alter_table("market_observations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_market_observations_player_id_observed_at",
            ["player_id", "observed_at"],
            unique=False,
        )
    # Seed the history with the current supply picture of every card.
    op.execute(
        "INSERT INTO market_observations "
        "(player_id, platform, source, observed_at, listings_csv, range_max) "
        "SELECT player_id, platform, source, observed_at, listings_csv, range_max "
        "FROM market_state"
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("market_observations", schema=None) as batch_op:
        batch_op.drop_index("ix_market_observations_player_id_observed_at")
    op.drop_table("market_observations")
