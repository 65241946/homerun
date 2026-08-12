import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import (
    Base,
    SimulationAccount,
    SimulationPosition,
    SimulationTrade,
    Trader,
    TraderOrder,
)
from tests.postgres_test_db import build_postgres_session_factory
from workers import trader_orchestrator_worker


async def _build_session_factory(_tmp_path: Path):
    return await build_postgres_session_factory(Base, "trader_orchestrator_shadow_backfill")


@pytest.mark.asyncio
async def test_backfill_passes_shadow_simulation_fee_and_slippage_to_ledger(tmp_path, monkeypatch):
    engine, session_factory = await _build_session_factory(tmp_path)
    try:
        async with session_factory() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(
                Trader(
                    id="trader-1",
                    name="Backfill Trader",
                    source_configs_json=[{"source_key": "crypto", "strategy_key": "btc_eth_maker_quote", "strategy_params": {}}],
                    risk_limits_json={},
                    metadata_json={},
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=60,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TraderOrder(
                    id="order-1",
                    trader_id="trader-1",
                    signal_id=None,
                    source="crypto",
                    market_id="market-1",
                    market_question="Will this backfill?",
                    direction="buy_yes",
                    mode="shadow",
                    status="executed",
                    notional_usd=50.0,
                    entry_price=0.5,
                    effective_price=0.5,
                    payload_json={
                        "shadow_simulation": {
                            "estimated_fee_usd": 1.2,
                            "slippage_usd": 0.75,
                            "fill_ratio": 0.9,
                        }
                    },
                    created_at=now,
                    executed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

            record_mock = AsyncMock(
                return_value={
                    "account_id": "shadow-1",
                    "trade_id": "trade-1",
                    "position_id": "position-1",
                }
            )
            monkeypatch.setattr(
                trader_orchestrator_worker.simulation_service,
                "record_orchestrator_shadow_fill",
                record_mock,
            )

            result = await trader_orchestrator_worker._backfill_simulation_ledger_for_active_shadow_orders(
                session,
                trader_id="trader-1",
                shadow_account_id="shadow-1",
            )
            assert result["attempted"] == 1
            assert result["backfilled"] == 1
            assert result["errors"] == []

        async with session_factory() as verification_session:
            order = await verification_session.get(TraderOrder, "order-1")
            assert order is not None
            assert isinstance((order.payload_json or {}).get("simulation_ledger"), dict)

        record_mock.assert_awaited_once()
        kwargs = record_mock.await_args.kwargs
        assert kwargs["execution_fee_usd"] == pytest.approx(1.2, rel=1e-9)
        assert kwargs["execution_slippage_usd"] == pytest.approx(0.75, rel=1e-9)
        assert kwargs["payload"]["shadow_simulation"]["fill_ratio"] == pytest.approx(0.9, rel=1e-9)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_repairs_marker_when_referenced_ledger_rows_are_missing(tmp_path):
    engine, session_factory = await _build_session_factory(tmp_path)
    try:
        async with session_factory() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(
                SimulationAccount(
                    id="shadow-1",
                    name="Repair Account",
                    initial_capital=1000.0,
                    current_capital=1000.0,
                    total_pnl=0.0,
                    total_trades=0,
                )
            )
            session.add(
                Trader(
                    id="trader-1",
                    name="Repair Trader",
                    source_configs_json=[
                        {
                            "source_key": "scanner",
                            "strategy_key": "stat_arb",
                            "strategy_params": {},
                        }
                    ],
                    risk_limits_json={},
                    metadata_json={},
                    is_enabled=True,
                    is_paused=False,
                    interval_seconds=60,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TraderOrder(
                    id="order-stale-marker",
                    trader_id="trader-1",
                    signal_id="signal-stale-marker",
                    source="scanner",
                    strategy_key="stat_arb",
                    market_id="market-1",
                    market_question="Will the stale marker be repaired?",
                    direction="buy_yes",
                    mode="shadow",
                    status="open",
                    notional_usd=10.0,
                    entry_price=0.5,
                    effective_price=0.5,
                    payload_json={
                        "simulation_ledger": {
                            "account_id": "shadow-1",
                            "trade_id": "missing-trade",
                            "position_id": "missing-position",
                        }
                    },
                    created_at=now,
                    executed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

            result = await trader_orchestrator_worker._backfill_simulation_ledger_for_active_shadow_orders(
                session,
                trader_id="trader-1",
                shadow_account_id="shadow-1",
            )

            assert result["attempted"] == 1
            assert result["backfilled"] == 1
            assert result["stale_markers"] == 1
            assert result["errors"] == []

        async with session_factory() as verification_session:
            order = await verification_session.get(TraderOrder, "order-stale-marker")
            assert order is not None
            ledger = (order.payload_json or {}).get("simulation_ledger") or {}
            assert ledger.get("trade_id") != "missing-trade"
            assert ledger.get("position_id") != "missing-position"

            trade = await verification_session.get(SimulationTrade, ledger["trade_id"])
            position = await verification_session.get(SimulationPosition, ledger["position_id"])
            account = await verification_session.get(SimulationAccount, "shadow-1")
            assert trade is not None
            assert position is not None
            assert account is not None
            assert account.total_trades == 1
            assert account.current_capital == pytest.approx(990.0, rel=1e-9)
    finally:
        await engine.dispose()
