"""portfolio listings and unknown buy price

Revision ID: 8d4e1b7a2c90
Revises: e2f7b9a4c618
Create Date: 2026-10-05 12:00:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d4e1b7a2c90"
down_revision: str | Sequence[str] | None = "e2f7b9a4c618"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("portfolio", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_portfolio_buy_price_positive"), type_="check")
        batch_op.alter_column("buy_price", existing_type=sa.Integer(), nullable=True)
        batch_op.create_check_constraint("buy_price_positive", "buy_price IS NULL OR buy_price > 0")

    op.create_table(
        "portfolio_listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("listed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("price > 0", name=op.f("ck_portfolio_listings_price_positive")),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["portfolio.id"],
            name=op.f("fk_portfolio_listings_position_id_portfolio"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolio_listings")),
    )
    with op.batch_alter_table("portfolio_listings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_portfolio_listings_position_id"), ["position_id"], unique=False
        )

    # Positions listed so far: keep their current listing as the first history entry.
    now = datetime.now(UTC).replace(tzinfo=None)
    op.execute(
        sa.text(
            "INSERT INTO portfolio_listings (position_id, price, listed_at) "
            "SELECT id, sell_price, :now FROM portfolio "
            "WHERE status = 'listed' AND sell_price IS NOT NULL"
        ).bindparams(now=now)
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("portfolio_listings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_portfolio_listings_position_id"))
    op.drop_table("portfolio_listings")

    op.execute("DELETE FROM portfolio WHERE buy_price IS NULL")
    with op.batch_alter_table("portfolio", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_portfolio_buy_price_positive"), type_="check")
        batch_op.alter_column("buy_price", existing_type=sa.Integer(), nullable=False)
        batch_op.create_check_constraint("buy_price_positive", "buy_price > 0")
