from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from models.database import (
    Base,
    OnlineMarketResolution,
    SimulationAccount,
    SimulationCashLedgerEntry,
    TraderOrderSettlement,
    TradeStatus,
)
from services.settlement_coordinator import (
    SettlementIdempotencyConflict,
    apply_shadow_market_resolution,
)
from services.simulation_ledger import get_ledger_integrity
from tests.postgres_test_db import build_postgres_session_factory
from tests.settlement_test_support import load_seeded_rows, seed_shadow_order


@pytest.mark.asyncio
async def test_shadow_final_resolution_atomically_projects_order_trade_position_and_cash():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_coordinator_happy")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="account-happy",
            order_id="order-happy",
            condition_id="condition-happy",
            provider_market_id="market-happy",
        )

        async with session_factory() as session, session.begin():
            result = await apply_shadow_market_resolution(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        assert result.applied is True
        assert result.idempotent_replay is False
        assert result.status == "projected"
        assert result.reason_code is None
        assert result.net_payout_usdc == Decimal("247.000000")
        assert result.realized_pnl_usdc == Decimal("147.000000")

        async with session_factory() as session:
            account, order, trade, position, _resolution = await load_seeded_rows(session, seeded)
            settlements = (
                await session.execute(
                    select(TraderOrderSettlement).where(
                        TraderOrderSettlement.trader_order_id == seeded.order_id
                    )
                )
            ).scalars().all()
            cash_entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry)
                    .where(SimulationCashLedgerEntry.account_id == seeded.account_id)
                    .order_by(SimulationCashLedgerEntry.ledger_sequence)
                )
            ).scalars().all()
            integrity = await get_ledger_integrity(session, seeded.account_id)

        assert account is not None
        assert account.current_capital == pytest.approx(1147.0)
        assert account.total_pnl == pytest.approx(147.0)
        assert account.winning_trades == 1
        assert account.losing_trades == 0
        assert order is not None
        assert order.status == "resolved_win"
        assert order.actual_profit == pytest.approx(147.0)
        assert trade is not None and trade.status == TradeStatus.RESOLVED_WIN
        assert trade.actual_payout == pytest.approx(247.0)
        assert trade.actual_pnl == pytest.approx(147.0)
        assert position is not None and position.status == TradeStatus.RESOLVED_WIN
        assert len(settlements) == 1
        assert settlements[0].status == "projected"
        assert settlements[0].authority == "gamma_final"
        assert settlements[0].held_token_id == seeded.held_token_id
        assert settlements[0].winning_token_id == seeded.winning_token_id
        assert settlements[0].net_payout_usdc == Decimal("247.000000")
        assert settlements[0].realized_pnl_usdc == Decimal("147.000000")
        assert [entry.entry_type for entry in cash_entries] == [
            "opening_balance",
            "entry_debit",
            "settlement_credit",
        ]
        assert integrity.difference_usdc == Decimal("0.000000")
        assert integrity.status == "complete"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unsafe_resolution_order_or_ledger_inputs_become_manual_review_without_cash_change():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_coordinator_manual")
    cases = [
        ("pending", {"resolution_state": "closed_pending", "winning_outcome_index": None}, "resolution_not_final"),
        ("conflicted", {"resolution_state": "conflicted"}, "resolution_conflicted"),
        ("identity", {"identity_status": "ambiguous"}, "identity_not_settlement_safe"),
        ("live", {"mode": "live"}, "unsupported_order_mode"),
        ("missing_refs", {"include_simulation_refs": False}, "simulation_ledger_reference_missing"),
    ]
    try:
        seeded_cases = []
        for suffix, overrides, expected_code in cases:
            seeded = await seed_shadow_order(
                session_factory,
                account_id="account-manual",
                order_id=f"order-{suffix}",
                condition_id=f"condition-{suffix}",
                provider_market_id=f"market-{suffix}",
                initial_capital=2_000.0,
                **overrides,
            )
            seeded_cases.append((seeded, expected_code))

        async with session_factory() as session:
            before = await session.get(SimulationAccount, "account-manual")
            assert before is not None
            before_capital = before.current_capital
            before_credit_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(
                    SimulationCashLedgerEntry.account_id == "account-manual",
                    SimulationCashLedgerEntry.entry_type == "settlement_credit",
                )
            )

        results = []
        for seeded, expected_code in seeded_cases:
            async with session_factory() as session, session.begin():
                result = await apply_shadow_market_resolution(
                    session,
                    trader_order_id=seeded.order_id,
                    online_market_resolution_id=seeded.resolution_id,
                )
            assert result.applied is False
            assert result.status == "manual_review"
            assert result.reason_code == expected_code
            results.append(result)

        async with session_factory() as session:
            after = await session.get(SimulationAccount, "account-manual")
            assert after is not None
            after_credit_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(
                    SimulationCashLedgerEntry.account_id == "account-manual",
                    SimulationCashLedgerEntry.entry_type == "settlement_credit",
                )
            )
            manual_rows = (
                await session.execute(
                    select(TraderOrderSettlement).where(
                        TraderOrderSettlement.status == "manual_review"
                    )
                )
            ).scalars().all()

        assert after.current_capital == pytest.approx(before_capital)
        assert before_credit_count == 0
        assert after_credit_count == 0
        assert len(manual_rows) == len(cases)
        assert {row.id for row in manual_rows} == {result.settlement_id for result in results}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failure_after_settlement_intent_rolls_back_every_economic_and_terminal_write():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_coordinator_rollback")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="account-rollback",
            order_id="order-rollback",
            condition_id="condition-rollback",
            provider_market_id="market-rollback",
        )

        def fail_after_intent(stage: str) -> None:
            if stage == "after_settlement_intent":
                raise RuntimeError("injected failure after settlement intent")

        with pytest.raises(RuntimeError, match="injected failure"):
            async with session_factory() as session:
                async with session.begin():
                    await apply_shadow_market_resolution(
                        session,
                        trader_order_id=seeded.order_id,
                        online_market_resolution_id=seeded.resolution_id,
                        fault_injector=fail_after_intent,
                    )

        async with session_factory() as session:
            account, order, trade, position, _resolution = await load_seeded_rows(session, seeded)
            settlement_count = await session.scalar(
                select(func.count())
                .select_from(TraderOrderSettlement)
                .where(TraderOrderSettlement.trader_order_id == seeded.order_id)
            )
            cash_entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry)
                    .where(SimulationCashLedgerEntry.account_id == seeded.account_id)
                    .order_by(SimulationCashLedgerEntry.ledger_sequence)
                )
            ).scalars().all()

        assert account is not None and account.current_capital == pytest.approx(900.0)
        assert account.total_pnl == pytest.approx(0.0)
        assert order is not None and order.status == "open" and order.actual_profit is None
        assert trade is not None and trade.status == TradeStatus.OPEN
        assert position is not None and position.status == TradeStatus.OPEN
        assert settlement_count == 0
        assert [entry.entry_type for entry in cash_entries] == ["opening_balance", "entry_debit"]

        async with session_factory() as session, session.begin():
            recovered = await apply_shadow_market_resolution(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )
        assert recovered.applied is True
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure_stage", ["after_cash_journal", "before_terminal_flush"])
@pytest.mark.asyncio
async def test_failure_after_cash_write_still_rolls_back_journal_and_all_projections(failure_stage):
    schema_suffix = failure_stage.replace("_", "")
    engine, session_factory = await build_postgres_session_factory(
        Base,
        f"settlement_rollback_{schema_suffix}",
    )
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id=f"account-{failure_stage}",
            order_id=f"order-{failure_stage}",
            condition_id=f"condition-{failure_stage}",
            provider_market_id=f"market-{failure_stage}",
        )

        def fail_at_selected_stage(stage: str) -> None:
            if stage == failure_stage:
                raise RuntimeError(f"injected failure at {failure_stage}")

        with pytest.raises(RuntimeError, match=failure_stage):
            async with session_factory() as session:
                async with session.begin():
                    await apply_shadow_market_resolution(
                        session,
                        trader_order_id=seeded.order_id,
                        online_market_resolution_id=seeded.resolution_id,
                        fault_injector=fail_at_selected_stage,
                    )

        async with session_factory() as session:
            account, order, trade, position, _resolution = await load_seeded_rows(session, seeded)
            settlement_count = await session.scalar(
                select(func.count())
                .select_from(TraderOrderSettlement)
                .where(TraderOrderSettlement.trader_order_id == seeded.order_id)
            )
            cash_types = (
                await session.execute(
                    select(SimulationCashLedgerEntry.entry_type)
                    .where(SimulationCashLedgerEntry.account_id == seeded.account_id)
                    .order_by(SimulationCashLedgerEntry.ledger_sequence)
                )
            ).scalars().all()

        assert account is not None and account.current_capital == pytest.approx(900.0)
        assert account.total_pnl == pytest.approx(0.0)
        assert order is not None and order.status == "open" and order.actual_profit is None
        assert trade is not None and trade.status == TradeStatus.OPEN
        assert position is not None and position.status == TradeStatus.OPEN
        assert settlement_count == 0
        assert list(cash_types) == ["opening_balance", "entry_debit"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_settlement_replay_rejects_a_different_resolution_fact():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_terminal_conflict")
    try:
        primary = await seed_shadow_order(
            session_factory,
            account_id="account-terminal-primary",
            order_id="order-terminal-primary",
            condition_id="condition-terminal-primary",
            provider_market_id="market-terminal-primary",
        )
        alternate = await seed_shadow_order(
            session_factory,
            account_id="account-terminal-alternate",
            order_id="order-terminal-alternate",
            condition_id="condition-terminal-alternate",
            provider_market_id="market-terminal-alternate",
        )

        async with session_factory() as session, session.begin():
            first = await apply_shadow_market_resolution(
                session,
                trader_order_id=primary.order_id,
                online_market_resolution_id=primary.resolution_id,
            )
        assert first.applied is True

        with pytest.raises(SettlementIdempotencyConflict, match="different resolution"):
            async with session_factory() as session:
                async with session.begin():
                    await apply_shadow_market_resolution(
                        session,
                        trader_order_id=primary.order_id,
                        online_market_resolution_id=alternate.resolution_id,
                    )

        async with session_factory() as session, session.begin():
            primary_resolution = await session.get(
                OnlineMarketResolution,
                primary.resolution_id,
            )
            assert primary_resolution is not None
            primary_resolution.state = "conflicted"
            primary_resolution.fact_version = int(primary_resolution.fact_version or 0) + 1

        with pytest.raises(SettlementIdempotencyConflict, match="non-final"):
            async with session_factory() as session:
                async with session.begin():
                    await apply_shadow_market_resolution(
                        session,
                        trader_order_id=primary.order_id,
                        online_market_resolution_id=primary.resolution_id,
                    )

        async with session_factory() as session:
            account = await session.get(SimulationAccount, primary.account_id)
            settlement_count = await session.scalar(
                select(func.count())
                .select_from(TraderOrderSettlement)
                .where(TraderOrderSettlement.trader_order_id == primary.order_id)
            )
            credit_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(
                    SimulationCashLedgerEntry.account_id == primary.account_id,
                    SimulationCashLedgerEntry.entry_type == "settlement_credit",
                )
            )

        assert account is not None and account.current_capital == pytest.approx(1147.0)
        assert account.total_pnl == pytest.approx(147.0)
        assert settlement_count == 1
        assert credit_count == 1
    finally:
        await engine.dispose()
