import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from models.database import (
    Base,
    ImmutableLedgerEntryError,
    SimulationAccount,
    SimulationCashLedgerEntry,
    SimulationTrade,
    TradeStatus,
)
from models.opportunity import Opportunity
from services import simulation as simulation_module
from services.simulation import SimulationService
from services.simulation_ledger import (
    LedgerAlreadyReversed,
    LedgerIdempotencyConflict,
    checkpoint_legacy_account,
    get_ledger_integrity,
    record_entry_debit,
    record_settlement_credit,
    reverse_cash_entry,
)
from tests.postgres_test_db import build_postgres_session_factory


async def _new_v2_account(monkeypatch, session_factory, *, initial_capital: float = 1000.0):
    monkeypatch.setattr(simulation_module._legacy, "AsyncSessionLocal", session_factory)
    return await SimulationService().create_account(
        name="V2 proof account",
        initial_capital=initial_capital,
    )


@pytest.mark.asyncio
async def test_v2_orchestrator_open_close_is_rebuildable_and_idempotent(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_v2_flow")
    try:
        account = await _new_v2_account(monkeypatch, session_factory)
        service = SimulationService()

        async with session_factory() as session:
            opened = await service.record_orchestrator_shadow_fill(
                account_id=account.id,
                trader_id="trader-1",
                signal_id="signal-1",
                market_id="3486334",
                market_question="Will the proof account reconcile?",
                direction="buy_yes",
                notional_usd=200.0,
                entry_price=0.5,
                strategy_type="proof_strategy",
                token_id="123",
                session=session,
                commit=False,
            )
            await session.commit()

        async with session_factory() as session:
            after_open = await session.get(SimulationAccount, account.id)
            open_entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry)
                    .where(SimulationCashLedgerEntry.account_id == account.id)
                    .order_by(SimulationCashLedgerEntry.ledger_sequence)
                )
            ).scalars().all()

        assert after_open is not None
        assert after_open.ledger_version == 2
        assert after_open.ledger_integrity_status == "complete"
        assert Decimal(str(after_open.current_capital)).quantize(Decimal("0.000001")) == Decimal("800.000000")
        assert [(row.entry_type, row.amount_usdc) for row in open_entries] == [
            ("opening_balance", Decimal("0.000000")),
            ("entry_debit", Decimal("-200.000000")),
        ]

        async with session_factory() as session:
            replay_open = await record_entry_debit(
                session,
                account_id=account.id,
                simulation_trade_id=opened["trade_id"],
                entry_cost_usdc=Decimal(200),
            )
            assert replay_open.inserted is False
            with pytest.raises(LedgerIdempotencyConflict):
                await record_entry_debit(
                    session,
                    account_id=account.id,
                    simulation_trade_id=opened["trade_id"],
                    entry_cost_usdc=Decimal(201),
                )
            await session.rollback()

        async with session_factory() as session:
            closed = await service.close_orchestrator_shadow_fill(
                account_id=account.id,
                trade_id=opened["trade_id"],
                position_id=opened["position_id"],
                close_price=0.8,
                close_trigger="resolution",
                price_source="test_finality",
                reason="proof_test",
                session=session,
                commit=False,
            )
            await session.commit()

        assert closed["actual_payout"] == pytest.approx(317.6)
        assert closed["actual_pnl"] == pytest.approx(117.6)

        async with session_factory() as session:
            replay_close = await service.close_orchestrator_shadow_fill(
                account_id=account.id,
                trade_id=opened["trade_id"],
                position_id=opened["position_id"],
                close_price=0.8,
                close_trigger="resolution",
                session=session,
                commit=False,
            )
            assert replay_close["already_closed"] is True
            await session.commit()

        async with session_factory() as session:
            result = await record_settlement_credit(
                session,
                account_id=account.id,
                idempotency_key=f"shadow-close:{opened['trade_id']}",
                payout_usdc=Decimal("317.6"),
                realized_pnl_usdc=Decimal("117.6"),
                won=True,
                simulation_trade_id=opened["trade_id"],
            )
            assert result.inserted is False
            await session.commit()

        async with session_factory() as session:
            integrity = await get_ledger_integrity(session, account.id)
            final_account = await session.get(SimulationAccount, account.id)
            final_entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry)
                    .where(SimulationCashLedgerEntry.account_id == account.id)
                    .order_by(SimulationCashLedgerEntry.ledger_sequence)
                )
            ).scalars().all()

        assert final_account is not None
        assert Decimal(str(final_account.current_capital)).quantize(Decimal("0.000001")) == Decimal("1117.600000")
        assert Decimal(str(final_account.total_pnl)).quantize(Decimal("0.000001")) == Decimal("117.600000")
        assert final_account.winning_trades == 1
        assert final_account.losing_trades == 0
        assert [row.ledger_sequence for row in final_entries] == [1, 2, 3]
        assert [row.entry_type for row in final_entries] == [
            "opening_balance",
            "entry_debit",
            "settlement_credit",
        ]
        assert integrity.status == "complete"
        assert integrity.journal_delta_usdc == Decimal("117.600000")
        assert integrity.rebuilt_balance_usdc == Decimal("1117.600000")
        assert integrity.projected_balance_usdc == Decimal("1117.600000")
        assert integrity.difference_usdc == Decimal("0.000000")
        assert integrity.sequence_contiguous is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cash_entries_are_immutable_and_reversal_is_single_use(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_reversal")
    try:
        account = await _new_v2_account(monkeypatch, session_factory)
        trade_id = uuid.uuid4().hex
        async with session_factory() as session:
            session.add(
                SimulationTrade(
                    id=trade_id,
                    account_id=account.id,
                    strategy_type="reversal_test",
                    positions_data=[],
                    total_cost=50.0,
                    status=TradeStatus.OPEN,
                )
            )
            await session.flush()
            debit = await record_entry_debit(
                session,
                account_id=account.id,
                simulation_trade_id=trade_id,
                entry_cost_usdc=Decimal(50),
            )
            await session.commit()

        async with session_factory() as session:
            row = await session.get(SimulationCashLedgerEntry, debit.entry.id)
            assert row is not None
            row.amount_usdc = Decimal(-49)
            with pytest.raises(ImmutableLedgerEntryError):
                await session.flush()
            await session.rollback()

        async with session_factory() as session:
            row = await session.get(SimulationCashLedgerEntry, debit.entry.id)
            assert row is not None
            await session.delete(row)
            with pytest.raises(ImmutableLedgerEntryError):
                await session.flush()
            await session.rollback()

        async with session_factory() as session:
            reversal = await reverse_cash_entry(
                session,
                entry_id=debit.entry.id,
                idempotency_key=f"shadow-reversal:{debit.entry.id}",
                reason="operator_verified_correction",
            )
            assert reversal.inserted is True
            assert reversal.entry.entry_type == "reversal"
            assert reversal.entry.reversal_of_entry_id == debit.entry.id
            assert reversal.entry.amount_usdc == Decimal("50.000000")
            await session.commit()

        async with session_factory() as session:
            replay = await reverse_cash_entry(
                session,
                entry_id=debit.entry.id,
                idempotency_key=f"shadow-reversal:{debit.entry.id}",
                reason="operator_verified_correction",
            )
            assert replay.inserted is False
            with pytest.raises(LedgerAlreadyReversed):
                await reverse_cash_entry(
                    session,
                    entry_id=debit.entry.id,
                    idempotency_key=f"shadow-reversal-second:{debit.entry.id}",
                    reason="must_not_double_reverse",
                )
            await session.rollback()

        async with session_factory() as session:
            integrity = await get_ledger_integrity(session, account.id)
            entry_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.account_id == account.id)
            )
            locked_account = await session.get(SimulationAccount, account.id)
            assert locked_account is not None
            await session.delete(locked_account)
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        assert entry_count == 3
        assert integrity.rebuilt_balance_usdc == Decimal("1000.000000")
        assert integrity.difference_usdc == Decimal("0.000000")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_account_requires_explicit_checkpoint_and_never_becomes_complete():
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_checkpoint")
    account_id = uuid.uuid4().hex
    try:
        async with session_factory() as session:
            session.add(
                SimulationAccount(
                    id=account_id,
                    name="Historical legacy account",
                    initial_capital=Decimal(1000),
                    current_capital=Decimal(875),
                    ledger_version=1,
                    ledger_integrity_status="legacy",
                )
            )
            await session.commit()

        async with session_factory() as session:
            before = await get_ledger_integrity(session, account_id)
        assert before.status == "legacy"

        async with session_factory() as session:
            checkpoint = await checkpoint_legacy_account(
                session,
                account_id=account_id,
                reason="explicit_historical_cutover",
                operator="test_operator",
            )
            assert checkpoint.inserted is True
            await session.commit()

        async with session_factory() as session:
            replay = await checkpoint_legacy_account(
                session,
                account_id=account_id,
                reason="explicit_historical_cutover",
                operator="test_operator",
            )
            assert replay.inserted is False
            await session.commit()

        async with session_factory() as session:
            after = await get_ledger_integrity(session, account_id)
            upgraded = await session.get(SimulationAccount, account_id)
            entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry).where(
                        SimulationCashLedgerEntry.account_id == account_id
                    )
                )
            ).scalars().all()

        assert upgraded is not None
        assert upgraded.ledger_version == 2
        assert upgraded.ledger_integrity_status == "checkpointed"
        assert Decimal(str(upgraded.current_capital)).quantize(Decimal("0.000001")) == Decimal("875.000000")
        assert len(entries) == 1
        assert entries[0].entry_type == "manual_adjustment"
        assert entries[0].amount_usdc == Decimal("-125.000000")
        assert after.status == "checkpointed"
        assert after.coverage == "checkpoint"
        assert after.rebuilt_balance_usdc == Decimal("875.000000")
        assert after.difference_usdc == Decimal("0.000000")
    finally:
        await engine.dispose()


def _legacy_manual_opportunity(*, opportunity_id: str) -> Opportunity:
    return Opportunity(
        id=opportunity_id,
        strategy="legacy_manual_test",
        title="Legacy manual simulation entrypoint",
        description="Compatibility proof for the pre-v2 manual simulation API",
        total_cost=0.5,
        expected_payout=1.0,
        gross_profit=0.5,
        fee=0.0,
        net_profit=0.5,
        roi_percent=100.0,
        min_liquidity=1_000.0,
        max_position_size=100.0,
        positions_to_take=[
            {
                "market_id": "legacy-market-1",
                "market_question": "Will the legacy compatibility test pass?",
                "token_id": "legacy-token-yes",
                "outcome": "YES",
                "price": 0.5,
            }
        ],
    )


@pytest.mark.asyncio
async def test_v2_account_rejects_legacy_manual_execute_entrypoint(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_v2_execute_gate")
    try:
        account = await _new_v2_account(monkeypatch, session_factory)
        service = SimulationService()

        with pytest.raises(ValueError, match="v2 ledger account.*legacy manual execute"):
            await service.execute_opportunity(
                account_id=account.id,
                opportunity=_legacy_manual_opportunity(opportunity_id="v2-blocked-open"),
                position_size=10.0,
            )

        async with session_factory() as session:
            stored = await session.get(SimulationAccount, account.id)
            trade_count = await session.scalar(
                select(func.count())
                .select_from(SimulationTrade)
                .where(SimulationTrade.account_id == account.id)
            )
            entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry).where(
                        SimulationCashLedgerEntry.account_id == account.id
                    )
                )
            ).scalars().all()

        assert stored is not None
        assert stored.current_capital == pytest.approx(1000.0)
        assert stored.total_trades == 0
        assert trade_count == 0
        assert [entry.entry_type for entry in entries] == ["opening_balance"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v2_account_rejects_legacy_manual_resolve_entrypoint(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_v2_resolve_gate")
    trade_id = uuid.uuid4().hex
    try:
        account = await _new_v2_account(monkeypatch, session_factory)
        async with session_factory() as session:
            session.add(
                SimulationTrade(
                    id=trade_id,
                    account_id=account.id,
                    opportunity_id="v2-blocked-resolve",
                    strategy_type="legacy_manual_test",
                    positions_data=[{"outcome": "YES"}],
                    total_cost=10.0,
                    status=TradeStatus.OPEN,
                )
            )
            await session.commit()

        with pytest.raises(ValueError, match="v2 ledger account.*legacy resolve_trade"):
            await SimulationService().resolve_trade(trade_id=trade_id, winning_outcome="YES")

        async with session_factory() as session:
            stored_account = await session.get(SimulationAccount, account.id)
            stored_trade = await session.get(SimulationTrade, trade_id)
            entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry).where(
                        SimulationCashLedgerEntry.account_id == account.id
                    )
                )
            ).scalars().all()

        assert stored_account is not None
        assert stored_account.current_capital == pytest.approx(1000.0)
        assert stored_account.total_pnl == pytest.approx(0.0)
        assert stored_trade is not None
        assert stored_trade.status == TradeStatus.OPEN
        assert [entry.entry_type for entry in entries] == ["opening_balance"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v2_account_delete_is_rejected_before_append_only_history_is_touched(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_v2_delete_gate")
    try:
        account = await _new_v2_account(monkeypatch, session_factory)

        with pytest.raises(ValueError, match="v2 ledger account.*cannot be deleted"):
            await SimulationService().delete_account(account.id)

        async with session_factory() as session:
            stored = await session.get(SimulationAccount, account.id)
            entry_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.account_id == account.id)
            )

        assert stored is not None
        assert entry_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_manual_execute_and_resolve_entrypoints_remain_compatible(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "simulation_cash_legacy_api")
    account_id = uuid.uuid4().hex
    try:
        monkeypatch.setattr(simulation_module._legacy, "AsyncSessionLocal", session_factory)
        async with session_factory() as session:
            session.add(
                SimulationAccount(
                    id=account_id,
                    name="Legacy manual API account",
                    initial_capital=100.0,
                    current_capital=100.0,
                    ledger_version=1,
                    ledger_integrity_status="legacy",
                )
            )
            await session.commit()

        service = SimulationService()
        trade = await service.execute_opportunity(
            account_id=account_id,
            opportunity=_legacy_manual_opportunity(opportunity_id="legacy-open-resolve"),
            position_size=10.0,
        )
        resolved = await service.resolve_trade(trade_id=trade.id, winning_outcome="YES")

        async with session_factory() as session:
            stored = await session.get(SimulationAccount, account_id)
            journal_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.account_id == account_id)
            )

        assert resolved.status == TradeStatus.RESOLVED_LOSS
        assert stored is not None
        assert stored.ledger_version == 1
        assert stored.total_trades == 1
        assert stored.losing_trades == 1
        assert stored.current_capital == pytest.approx(99.8995)
        assert journal_count == 0
    finally:
        await engine.dispose()
