from __future__ import annotations

from pathlib import Path

from sqlalchemy import CheckConstraint, Numeric, UniqueConstraint

from models import database as db

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _model(name: str):
    model = getattr(db, name, None)
    assert model is not None, f"models.database.{name} is required"
    return model


def _column_names(model) -> set[str]:
    return {column.name for column in model.__table__.columns}


def _unique_column_sets(model) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_constraint_names(model) -> set[str]:
    return {
        str(constraint.name)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_trader_orders_persist_typed_market_identity() -> None:
    required = {
        "venue",
        "provider_market_id",
        "condition_id",
        "token_id",
        "outcome_index",
        "identity_status",
    }

    assert required <= _column_names(db.TraderOrder)


def test_simulation_accounts_expose_ledger_integrity_projection() -> None:
    required = {
        "ledger_version",
        "ledger_integrity_status",
        "ledger_verified_at",
    }

    assert required <= _column_names(db.SimulationAccount)


def test_online_resolution_models_have_required_identity_and_evidence_constraints() -> None:
    resolution = _model("OnlineMarketResolution")
    observation = _model("OnlineMarketResolutionObservation")

    assert {
        "venue",
        "provider",
        "provider_market_id",
        "condition_id",
        "token_ids_json",
        "outcomes_json",
        "state",
        "winning_token_id",
        "winning_outcome_index",
        "winning_outcome",
        "provider_resolved_at",
        "first_observed_at",
        "last_observed_at",
        "finalized_at",
        "fact_version",
        "evidence_hash",
        "evidence_json",
    } <= _column_names(resolution)
    assert ("venue", "condition_id") in _unique_column_sets(resolution)

    assert {
        "provider",
        "venue",
        "provider_market_id",
        "condition_id",
        "state",
        "evidence_hash",
        "evidence_json",
        "observed_at",
    } <= _column_names(observation)
    assert ("provider", "condition_id", "evidence_hash") in _unique_column_sets(observation)


def test_order_settlement_model_has_exactly_once_keys_and_fixed_precision_money() -> None:
    settlement = _model("TraderOrderSettlement")
    columns = settlement.__table__.columns

    assert {
        "trader_order_id",
        "settlement_kind",
        "mode",
        "online_market_resolution_id",
        "resolution_fact_version",
        "held_token_id",
        "winning_token_id",
        "quantity",
        "cost_basis_usdc",
        "gross_payout_usdc",
        "fee_usdc",
        "net_payout_usdc",
        "realized_pnl_usdc",
        "status",
        "live_state",
        "live_state_version",
        "live_evidence_hash",
        "live_evidence_json",
        "claimable_at",
        "redeem_submitted_at",
        "redeem_confirmed_at",
        "cash_verified_at",
        "redeem_tx_hash",
        "authority",
        "evidence_hash",
        "idempotency_key",
        "attempt_count",
        "last_error",
    } <= set(columns.keys())
    assert ("trader_order_id", "settlement_kind") in _unique_column_sets(settlement)
    assert ("idempotency_key",) in _unique_column_sets(settlement)
    assert "ck_trader_order_settlements_live_state" in _check_constraint_names(settlement)

    for name in (
        "quantity",
        "cost_basis_usdc",
        "gross_payout_usdc",
        "fee_usdc",
        "net_payout_usdc",
        "realized_pnl_usdc",
    ):
        assert isinstance(columns[name].type, Numeric), f"{name} must use NUMERIC, not Float"


def test_simulation_cash_ledger_has_sequence_idempotency_and_fixed_precision_amount() -> None:
    entry = _model("SimulationCashLedgerEntry")
    columns = entry.__table__.columns

    assert {
        "account_id",
        "simulation_trade_id",
        "trader_order_id",
        "trader_order_settlement_id",
        "ledger_sequence",
        "entry_type",
        "amount_usdc",
        "currency",
        "occurred_at",
        "idempotency_key",
        "reversal_of_entry_id",
        "evidence_json",
    } <= set(columns.keys())
    assert ("idempotency_key",) in _unique_column_sets(entry)
    assert ("account_id", "ledger_sequence") in _unique_column_sets(entry)
    assert ("reversal_of_entry_id",) in _unique_column_sets(entry)
    assert next(iter(columns["account_id"].foreign_keys)).ondelete == "RESTRICT"
    assert {
        "ck_simulation_cash_ledger_reversal_reference",
        "ck_simulation_cash_ledger_debit_negative",
        "ck_simulation_cash_ledger_credit_nonnegative",
        "ck_simulation_cash_ledger_opening_zero",
    } <= _check_constraint_names(entry)
    assert isinstance(columns["amount_usdc"].type, Numeric)
    assert columns["amount_usdc"].type.scale == 6


def test_online_settlement_migration_extends_the_actual_runtime_head() -> None:
    migration = BACKEND_ROOT / "alembic" / "versions" / "202608100002_online_settlement_ledger.py"

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'revision = "202608100002"' in text
    assert 'down_revision = "202608100001"' in text
