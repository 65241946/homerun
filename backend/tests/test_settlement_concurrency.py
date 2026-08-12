from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from models.database import (
    Base,
    SimulationAccount,
    SimulationCashLedgerEntry,
    TraderOrderSettlement,
)
from services.settlement_coordinator import apply_shadow_market_resolution
from services.simulation_ledger import get_ledger_integrity
from tests.postgres_test_db import build_postgres_session_factory
from tests.settlement_test_support import seed_shadow_order


async def _apply_once(session_factory, *, order_id: str, resolution_id: str):
    async with session_factory() as session, session.begin():
        return await apply_shadow_market_resolution(
            session,
            trader_order_id=order_id,
            online_market_resolution_id=resolution_id,
        )


@pytest.mark.asyncio
async def test_same_order_concurrent_twenty_times_has_exactly_one_economic_effect():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_concurrent_same")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="account-concurrent-same",
            order_id="order-concurrent-same",
            condition_id="condition-concurrent-same",
            provider_market_id="market-concurrent-same",
        )
        ready = asyncio.Event()

        async def contender():
            await ready.wait()
            return await _apply_once(
                session_factory,
                order_id=seeded.order_id,
                resolution_id=seeded.resolution_id,
            )

        tasks = [asyncio.create_task(contender()) for _ in range(20)]
        ready.set()
        results = await asyncio.gather(*tasks)

        assert sum(1 for result in results if result.applied) == 1
        assert sum(1 for result in results if result.idempotent_replay) == 19
        assert len({result.settlement_id for result in results}) == 1

        async with session_factory() as session:
            account = await session.get(SimulationAccount, seeded.account_id)
            settlement_count = await session.scalar(
                select(func.count())
                .select_from(TraderOrderSettlement)
                .where(TraderOrderSettlement.trader_order_id == seeded.order_id)
            )
            credit_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(
                    SimulationCashLedgerEntry.account_id == seeded.account_id,
                    SimulationCashLedgerEntry.entry_type == "settlement_credit",
                )
            )

        assert account is not None
        assert account.current_capital == pytest.approx(1147.0)
        assert account.total_pnl == pytest.approx(147.0)
        assert account.winning_trades == 1
        assert settlement_count == 1
        assert credit_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_different_orders_on_same_account_do_not_lose_balance_or_pnl_updates():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_concurrent_account")
    try:
        winner = await seed_shadow_order(
            session_factory,
            account_id="account-concurrent-two",
            order_id="order-concurrent-winner",
            condition_id="condition-concurrent-winner",
            provider_market_id="market-concurrent-winner",
            held_outcome_index=0,
            winning_outcome_index=0,
        )
        loser = await seed_shadow_order(
            session_factory,
            account_id="account-concurrent-two",
            order_id="order-concurrent-loser",
            condition_id="condition-concurrent-loser",
            provider_market_id="market-concurrent-loser",
            held_outcome_index=0,
            winning_outcome_index=1,
        )

        winner_result, loser_result = await asyncio.gather(
            _apply_once(
                session_factory,
                order_id=winner.order_id,
                resolution_id=winner.resolution_id,
            ),
            _apply_once(
                session_factory,
                order_id=loser.order_id,
                resolution_id=loser.resolution_id,
            ),
        )
        assert winner_result.applied is True
        assert loser_result.applied is True
        assert winner_result.realized_pnl_usdc == Decimal("147.000000")
        assert loser_result.realized_pnl_usdc == Decimal("-100.000000")

        async with session_factory() as session:
            account = await session.get(SimulationAccount, winner.account_id)
            entries = (
                await session.execute(
                    select(SimulationCashLedgerEntry)
                    .where(SimulationCashLedgerEntry.account_id == winner.account_id)
                    .order_by(SimulationCashLedgerEntry.ledger_sequence)
                )
            ).scalars().all()
            integrity = await get_ledger_integrity(session, winner.account_id)

        assert account is not None
        assert account.current_capital == pytest.approx(1047.0)
        assert account.total_pnl == pytest.approx(47.0)
        assert account.winning_trades == 1
        assert account.losing_trades == 1
        assert len(entries) == 5
        assert [entry.ledger_sequence for entry in entries] == [1, 2, 3, 4, 5]
        assert integrity.difference_usdc == Decimal("0.000000")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_hundred_restart_style_replays_leave_economic_result_unchanged():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_replay_hundred")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="account-replay-hundred",
            order_id="order-replay-hundred",
            condition_id="condition-replay-hundred",
            provider_market_id="market-replay-hundred",
        )
        first = await _apply_once(
            session_factory,
            order_id=seeded.order_id,
            resolution_id=seeded.resolution_id,
        )
        assert first.applied is True

        replays = []
        for _ in range(100):
            replays.append(
                await _apply_once(
                    session_factory,
                    order_id=seeded.order_id,
                    resolution_id=seeded.resolution_id,
                )
            )
        assert all(result.applied is False for result in replays)
        assert all(result.idempotent_replay is True for result in replays)
        assert {result.settlement_id for result in replays} == {first.settlement_id}

        async with session_factory() as session:
            account = await session.get(SimulationAccount, seeded.account_id)
            settlement_count = await session.scalar(
                select(func.count()).select_from(TraderOrderSettlement)
            )
            credit_count = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.entry_type == "settlement_credit")
            )
            integrity = await get_ledger_integrity(session, seeded.account_id)

        assert account is not None
        assert account.current_capital == pytest.approx(1147.0)
        assert account.total_pnl == pytest.approx(147.0)
        assert account.winning_trades == 1
        assert settlement_count == 1
        assert credit_count == 1
        assert integrity.difference_usdc == Decimal("0.000000")
    finally:
        await engine.dispose()
