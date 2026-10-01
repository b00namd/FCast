"""radar

Revision ID: c5d8a1e6f240
Revises: 7b2e4d91c3a5
Create Date: 2026-10-01 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d8a1e6f240"
down_revision: str | Sequence[str] | None = "7b2e4d91c3a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "radar_cards",
        sa.Column("futbin_ref", sa.String(length=200), nullable=False),
        sa.Column("list_name", sa.String(length=16), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
            name=op.f("fk_radar_cards_player_id_players"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("futbin_ref", name=op.f("pk_radar_cards")),
    )
    op.create_table(
        "usage_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
            name=op.f("fk_usage_observations_player_id_players"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_observations")),
    )
    with op.batch_alter_table("usage_observations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_usage_observations_player_id_observed_at",
            ["player_id", "observed_at"],
            unique=False,
        )
    op.create_table(
        "fodder_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum("console", "pc", name="platform", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fodder_prices")),
    )
    with op.batch_alter_table("fodder_prices", schema=None) as batch_op:
        batch_op.create_index(
            "ix_fodder_prices_rating_observed_at", ["rating", "observed_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("fodder_prices", schema=None) as batch_op:
        batch_op.drop_index("ix_fodder_prices_rating_observed_at")
    op.drop_table("fodder_prices")
    with op.batch_alter_table("usage_observations", schema=None) as batch_op:
        batch_op.drop_index("ix_usage_observations_player_id_observed_at")
    op.drop_table("usage_observations")
    op.drop_table("radar_cards")
