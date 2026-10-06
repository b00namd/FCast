"""portfolio buy price 0 for free cards

Revision ID: 5a3c9e7f1d24
Revises: 8d4e1b7a2c90
Create Date: 2026-10-06 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a3c9e7f1d24"
down_revision: str | Sequence[str] | None = "8d4e1b7a2c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("portfolio", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_portfolio_buy_price_positive"), type_="check")
        batch_op.create_check_constraint(
            "buy_price_not_negative", "buy_price IS NULL OR buy_price >= 0"
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Free cards become cards with an unknown buy price.
    op.execute("UPDATE portfolio SET buy_price = NULL WHERE buy_price = 0")
    with op.batch_alter_table("portfolio", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_portfolio_buy_price_not_negative"), type_="check")
        batch_op.create_check_constraint("buy_price_positive", "buy_price IS NULL OR buy_price > 0")
