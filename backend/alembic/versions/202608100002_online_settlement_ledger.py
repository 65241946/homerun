"""Add typed online settlement facts and simulation cash journal.

Revision ID: 202608100002
Revises: 202608100001
Create Date: 2026-08-10

The migration is additive. Existing simulation accounts remain ledger v1 and
are explicitly marked ``legacy``; historical balances are not inferred.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from alembic_helpers import safe_add_column, safe_create_index, safe_create_table

revision = "202608100002"
down_revision = "202608100001"
branch_labels = None
depends_on = None


def _json_array_default() -> sa.TextClause:
    return sa.text("'[]'::json")


def _json_object_default() -> sa.TextClause:
    return sa.text("'{}'::json")


def upgrade() -> None:
    safe_add_column("trader_orders", sa.Column("venue", sa.String(), nullable=True))
    safe_add_column(
        "trader_orders",
        sa.Column("provider_market_id", sa.String(), nullable=True),
    )
    safe_add_column("trader_orders", sa.Column("condition_id", sa.String(), nullable=True))
    safe_add_column("trader_orders", sa.Column("token_id", sa.String(), nullable=True))
    safe_add_column("trader_orders", sa.Column("outcome_index", sa.Integer(), nullable=True))
    safe_add_column("trader_orders", sa.Column("identity_status", sa.String(), nullable=True))

    safe_add_column(
        "simulation_accounts",
        sa.Column("ledger_version", sa.Integer(), nullable=False, server_default="1"),
    )
    safe_add_column(
        "simulation_accounts",
        sa.Column(
            "ledger_integrity_status",
            sa.String(),
            nullable=False,
            server_default="legacy",
        ),
    )
    safe_add_column(
        "simulation_accounts",
        sa.Column("ledger_verified_at", sa.DateTime(), nullable=True),
    )

    safe_create_index(
        "idx_trader_orders_condition_id",
        "trader_orders",
        ["condition_id"],
    )
    safe_create_index(
        "idx_trader_orders_token_id",
        "trader_orders",
        ["token_id"],
    )
    safe_create_index(
        "idx_trader_orders_settlement_scan",
        "trader_orders",
        ["mode", "status", "identity_status"],
    )

    safe_create_table(
        "online_market_resolutions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("venue", sa.String(), nullable=False, server_default="polymarket"),
        sa.Column("provider", sa.String(), nullable=False, server_default="gamma"),
        sa.Column("provider_market_id", sa.String(), nullable=True),
        sa.Column("condition_id", sa.String(), nullable=False),
        sa.Column(
            "token_ids_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_array_default(),
        ),
        sa.Column(
            "outcomes_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_array_default(),
        ),
        sa.Column(
            "outcome_prices_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_array_default(),
        ),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("winning_token_id", sa.String(), nullable=True),
        sa.Column("winning_outcome_index", sa.Integer(), nullable=True),
        sa.Column("winning_outcome", sa.String(), nullable=True),
        sa.Column("provider_resolved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "first_observed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_observed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column("fact_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("evidence_hash", sa.String(), nullable=False),
        sa.Column(
            "evidence_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_object_default(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "venue",
            "condition_id",
            name="uq_online_market_resolutions_venue_condition",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed_pending', 'final', 'conflicted', 'invalid')",
            name="ck_online_market_resolutions_state",
        ),
    )
    safe_create_index(
        "idx_online_market_resolutions_state",
        "online_market_resolutions",
        ["state"],
    )
    safe_create_index(
        "idx_online_market_resolutions_provider_market",
        "online_market_resolutions",
        ["provider_market_id"],
    )

    safe_create_table(
        "online_market_resolution_observations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("venue", sa.String(), nullable=False, server_default="polymarket"),
        sa.Column("provider_market_id", sa.String(), nullable=True),
        sa.Column("condition_id", sa.String(), nullable=False),
        sa.Column(
            "token_ids_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_array_default(),
        ),
        sa.Column(
            "outcomes_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_array_default(),
        ),
        sa.Column(
            "outcome_prices_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_array_default(),
        ),
        sa.Column("closed", sa.Boolean(), nullable=True),
        sa.Column("accepting_orders", sa.Boolean(), nullable=True),
        sa.Column("resolution_status", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("winning_token_id", sa.String(), nullable=True),
        sa.Column("winning_outcome_index", sa.Integer(), nullable=True),
        sa.Column("winning_outcome", sa.String(), nullable=True),
        sa.Column("provider_resolved_at", sa.DateTime(), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_hash", sa.String(), nullable=False),
        sa.Column(
            "evidence_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_object_default(),
        ),
        sa.Column("observed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "provider",
            "condition_id",
            "evidence_hash",
            name="uq_online_resolution_observation_evidence",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed_pending', 'final', 'invalid')",
            name="ck_online_resolution_observations_state",
        ),
    )
    safe_create_index(
        "idx_online_resolution_observations_condition_observed",
        "online_market_resolution_observations",
        ["condition_id", "observed_at"],
    )

    safe_create_table(
        "trader_order_settlements",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "trader_order_id",
            sa.String(),
            sa.ForeignKey("trader_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("settlement_kind", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column(
            "simulation_account_id",
            sa.String(),
            sa.ForeignKey("simulation_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "simulation_trade_id",
            sa.String(),
            sa.ForeignKey("simulation_trades.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "simulation_position_id",
            sa.String(),
            sa.ForeignKey("simulation_positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "online_market_resolution_id",
            sa.String(),
            sa.ForeignKey("online_market_resolutions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("resolution_fact_version", sa.Integer(), nullable=True),
        sa.Column("held_token_id", sa.String(), nullable=True),
        sa.Column("winning_token_id", sa.String(), nullable=True),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("cost_basis_usdc", sa.Numeric(20, 6), nullable=True),
        sa.Column("gross_payout_usdc", sa.Numeric(20, 6), nullable=True),
        sa.Column("fee_usdc", sa.Numeric(20, 6), nullable=True),
        sa.Column("net_payout_usdc", sa.Numeric(20, 6), nullable=True),
        sa.Column("realized_pnl_usdc", sa.Numeric(20, 6), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="detected"),
        sa.Column("live_state", sa.String(), nullable=True),
        sa.Column("live_state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("live_evidence_hash", sa.String(), nullable=True),
        sa.Column(
            "live_evidence_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_object_default(),
        ),
        sa.Column("claimable_at", sa.DateTime(), nullable=True),
        sa.Column("redeem_submitted_at", sa.DateTime(), nullable=True),
        sa.Column("redeem_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("cash_verified_at", sa.DateTime(), nullable=True),
        sa.Column("redeem_tx_hash", sa.String(), nullable=True),
        sa.Column("authority", sa.String(), nullable=False),
        sa.Column("evidence_hash", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("projected_at", sa.DateTime(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "trader_order_id",
            "settlement_kind",
            name="uq_trader_order_settlements_order_kind",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_trader_order_settlements_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('detected', 'applied', 'projected', 'verified', "
            "'manual_review', 'reversed', 'failed')",
            name="ck_trader_order_settlements_status",
        ),
        sa.CheckConstraint(
            "live_state IS NULL OR live_state IN ('market_final', 'claimable', "
            "'redeem_submitted', 'redeem_confirmed', 'cash_verified')",
            name="ck_trader_order_settlements_live_state",
        ),
    )
    safe_create_index(
        "idx_trader_order_settlements_status",
        "trader_order_settlements",
        ["status"],
    )
    safe_create_index(
        "idx_trader_order_settlements_resolution",
        "trader_order_settlements",
        ["online_market_resolution_id"],
    )
    safe_create_index(
        "idx_trader_order_settlements_live_state",
        "trader_order_settlements",
        ["live_state"],
    )
    safe_create_index(
        "idx_trader_order_settlements_redeem_tx_hash",
        "trader_order_settlements",
        ["redeem_tx_hash"],
    )

    safe_create_table(
        "simulation_cash_ledger_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("simulation_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "simulation_trade_id",
            sa.String(),
            sa.ForeignKey("simulation_trades.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "trader_order_id",
            sa.String(),
            sa.ForeignKey("trader_orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "trader_order_settlement_id",
            sa.String(),
            sa.ForeignKey("trader_order_settlements.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=False),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("amount_usdc", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="USDC"),
        sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column(
            "reversal_of_entry_id",
            sa.String(),
            sa.ForeignKey("simulation_cash_ledger_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "evidence_json",
            sa.JSON(),
            nullable=False,
            server_default=_json_object_default(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_simulation_cash_ledger_idempotency",
        ),
        sa.UniqueConstraint(
            "account_id",
            "ledger_sequence",
            name="uq_simulation_cash_ledger_account_sequence",
        ),
        sa.UniqueConstraint(
            "reversal_of_entry_id",
            name="uq_simulation_cash_ledger_reversal_once",
        ),
        sa.CheckConstraint(
            "entry_type IN ('opening_balance', 'entry_debit', 'settlement_credit', "
            "'reversal', 'manual_adjustment')",
            name="ck_simulation_cash_ledger_entry_type",
        ),
        sa.CheckConstraint(
            "(entry_type = 'reversal' AND reversal_of_entry_id IS NOT NULL) OR "
            "(entry_type <> 'reversal' AND reversal_of_entry_id IS NULL)",
            name="ck_simulation_cash_ledger_reversal_reference",
        ),
        sa.CheckConstraint(
            "entry_type <> 'entry_debit' OR amount_usdc < 0",
            name="ck_simulation_cash_ledger_debit_negative",
        ),
        sa.CheckConstraint(
            "entry_type <> 'settlement_credit' OR amount_usdc >= 0",
            name="ck_simulation_cash_ledger_credit_nonnegative",
        ),
        sa.CheckConstraint(
            "entry_type <> 'opening_balance' OR amount_usdc = 0",
            name="ck_simulation_cash_ledger_opening_zero",
        ),
        sa.CheckConstraint(
            "currency = 'USDC'",
            name="ck_simulation_cash_ledger_currency",
        ),
    )
    safe_create_index(
        "idx_simulation_cash_ledger_account_occurred",
        "simulation_cash_ledger_entries",
        ["account_id", "occurred_at"],
    )
    safe_create_index(
        "idx_simulation_cash_ledger_settlement",
        "simulation_cash_ledger_entries",
        ["trader_order_settlement_id"],
    )


def downgrade() -> None:
    op.drop_table("simulation_cash_ledger_entries")
    op.drop_table("trader_order_settlements")
    op.drop_table("online_market_resolution_observations")
    op.drop_table("online_market_resolutions")

    op.execute("DROP INDEX IF EXISTS idx_trader_orders_settlement_scan")
    op.execute("DROP INDEX IF EXISTS idx_trader_orders_token_id")
    op.execute("DROP INDEX IF EXISTS idx_trader_orders_condition_id")

    op.drop_column("simulation_accounts", "ledger_verified_at")
    op.drop_column("simulation_accounts", "ledger_integrity_status")
    op.drop_column("simulation_accounts", "ledger_version")

    op.drop_column("trader_orders", "identity_status")
    op.drop_column("trader_orders", "outcome_index")
    op.drop_column("trader_orders", "token_id")
    op.drop_column("trader_orders", "condition_id")
    op.drop_column("trader_orders", "provider_market_id")
    op.drop_column("trader_orders", "venue")
