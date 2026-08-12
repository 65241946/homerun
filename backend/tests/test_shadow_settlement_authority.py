"""Regression: orchestrator shadow settlement authority.

Before the fix, the orchestrator mirrored shadow fills into a
``SimulationAccount`` ledger *asymmetrically*: the maintenance backfill
debited the account on open unconditionally, while the close-side credit
was gated behind ``enable_simulation_ledger`` (default ``False``, never
passed ``True``). Shadow orders therefore only ever debited the account,
draining ``current_capital`` until it errored.

The fix removes that half-built mirror entirely. The single source of
truth for orchestrator shadow PnL is ``TraderOrder.actual_profit``; the
orchestrator path must not touch ``SimulationAccount`` at all — not even
when a legacy ``simulation_ledger`` pointer is still present on the order
payload.
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import (  # noqa: E402
    Base,
    SimulationAccount,
    SimulationTrade,
    Trader,
    TradeSignal,
    TraderOrder,
)
from services.trader_orchestrator import position_lifecycle  # noqa: E402
from tests.postgres_test_db import build_postgres_session_factory  # noqa: E402


async def _seed_shadow_order(
    session: AsyncSession,
    *,
    simulation_ledger: dict | None = None,
) -> None:
    now = datetime.utcnow()
    session.add(
        Trader(
            id="trader-1",
            name="Shadow Trader",
            source_configs_json=[
                {"source_key": "crypto", "strategy_key": "btc_eth_maker_quote", "strategy_params": {}}
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
        TradeSignal(
            id="signal-1",
            source="crypto",
            signal_type="entry",
            strategy_type="crypto_15m",
            market_id="market-1",
            direction="buy_yes",
            entry_price=0.4,
            dedupe_key="dedupe-signal-1",
            payload_json={"yes_price": 0.4, "no_price": 0.6},
            created_at=now,
            updated_at=now,
        )
    )
    payload: dict = {}
    if simulation_ledger is not None:
        payload["simulation_ledger"] = simulation_ledger
    session.add(
        TraderOrder(
            id="order-1",
            trader_id="trader-1",
            signal_id="signal-1",
            source="crypto",
            market_id="market-1",
            direction="buy_yes",
            mode="shadow",
            status="executed",
            notional_usd=40.0,
            entry_price=0.4,
            effective_price=0.4,
            payload_json=payload,
            created_at=now,
            executed_at=now,
            updated_at=now,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_shadow_reconcile_writes_actual_profit_and_leaves_simulation_account_untouched(tmp_path, monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "shadow_settlement_authority")
    try:
        async with session_factory() as session:
            # A SimulationAccount the removed orchestrator mirror used to debit/credit.
            session.add(
                SimulationAccount(
                    id="shadow-1",
                    name="Shadow Account",
                    initial_capital=1000.0,
                    current_capital=1000.0,
                    total_pnl=0.0,
                )
            )
            # Seed the shadow order carrying a *legacy* mirror pointer that the
            # reconciler used to follow. It must now be inert.
            await _seed_shadow_order(
                session,
                simulation_ledger={
                    "account_id": "shadow-1",
                    "trade_id": "sim-trade-1",
                    "position_id": "sim-position-1",
                },
            )
            monkeypatch.setattr(
                position_lifecycle,
                "load_market_info_for_orders",
                AsyncMock(
                    return_value={
                        "market-1": {
                            "closed": True,
                            "accepting_orders": False,
                            "winner": None,
                            "winning_outcome": None,
                            "outcome_prices": [1.0, 0.0],
                        }
                    }
                ),
            )

            result = await position_lifecycle.reconcile_shadow_positions(
                session,
                trader_id="trader-1",
                trader_params={},
                dry_run=False,
            )

            order = await session.get(TraderOrder, "order-1")
            account = await session.get(SimulationAccount, "shadow-1")
            sim_trades = (
                await session.execute(
                    select(SimulationTrade).where(SimulationTrade.account_id == "shadow-1")
                )
            ).scalars().all()

        # Authoritative PnL lands on the TraderOrder: buy_yes resolved to
        # 1.0 => (40 / 0.4) * 1.0 - 40 = 60.0.
        assert result["closed"] == 1
        assert order is not None
        assert order.status == "resolved_win"
        assert order.actual_profit == pytest.approx(60.0)

        # The orchestrator shadow path no longer touches SimulationAccount.
        assert account is not None
        assert account.current_capital == pytest.approx(1000.0)
        assert account.total_pnl == pytest.approx(0.0)
        assert sim_trades == []

        # The removed mirror's downstream field never appears on the close payload.
        position_close = (order.payload_json or {}).get("position_close", {})
        assert "simulation_close" not in position_close
    finally:
        await engine.dispose()
