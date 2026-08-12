"""Preserve wallet trade outcome-token identity.

Wallet activity previously stored only BUY/SELL.  A Polymarket trade also
contains the outcome token (asset), label, and outcome index; without those
fields, BUY NO and SELL YES were indistinguishable from BUY YES and SELL NO.

Existing rollups are intentionally left NULL rather than guessed from side.
Existing confluence signals are derived from the ambiguous rows, so they are
deactivated and will be rebuilt from outcome-confirmed observations.

Revision ID: 202608100001
Revises: 202606160003
Create Date: 2026-08-10
"""

import sqlalchemy as sa

from alembic import op
from alembic_helpers import safe_add_column

revision = "202608100001"
down_revision = "202606160003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    safe_add_column("wallet_activity_rollups", sa.Column("token_id", sa.String(), nullable=True))
    safe_add_column("wallet_activity_rollups", sa.Column("outcome", sa.String(), nullable=True))
    safe_add_column("wallet_activity_rollups", sa.Column("outcome_index", sa.Integer(), nullable=True))

    # These rows are rebuildable derived signals, not positions or financial
    # ledger entries.  Keeping pre-migration rows active would expose known
    # ambiguous directions to the strategy firehose for up to 90 minutes.
    op.execute(
        """
        UPDATE market_confluence_signals
        SET is_active = FALSE,
            expired_at = COALESCE(expired_at, NOW())
        WHERE is_active = TRUE
        """
    )


def downgrade() -> None:
    op.drop_column("wallet_activity_rollups", "outcome_index")
    op.drop_column("wallet_activity_rollups", "outcome")
    op.drop_column("wallet_activity_rollups", "token_id")
