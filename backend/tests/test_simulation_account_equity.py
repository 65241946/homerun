from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import routes_simulation
from services import simulation as simulation_module
from services.simulation import SimulationService


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AccountSession:
    async def get(self, model, account_id):
        return _account() if account_id == "shadow-1" else None


def _account():
    return SimpleNamespace(
        id="shadow-1",
        name="Equity Account",
        initial_capital=1000.0,
        current_capital=900.0,
        total_pnl=0.0,
        total_trades=1,
        winning_trades=0,
        losing_trades=0,
        max_open_positions=10,
        created_at=datetime.now(timezone.utc),
    )


def _positions():
    return [
        SimpleNamespace(
            quantity=200.0,
            entry_price=0.5,
            current_price=0.5,
            entry_cost=100.0,
            unrealized_pnl=0.0,
        )
    ]


@pytest.mark.asyncio
async def test_account_stats_roi_uses_cash_plus_open_position_market_value(monkeypatch):
    session = _AccountSession()
    monkeypatch.setattr(simulation_module._legacy, "AsyncSessionLocal", lambda: _SessionContext(session))

    service = SimulationService()
    monkeypatch.setattr(service, "get_open_positions", AsyncMock(return_value=_positions()))

    stats = await service.get_account_stats("shadow-1")

    assert stats is not None
    assert stats["current_capital"] == pytest.approx(900.0)
    assert stats["roi_percent"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_account_list_roi_uses_cash_plus_open_position_market_value(monkeypatch):
    monkeypatch.setattr(
        routes_simulation.simulation_service,
        "get_all_accounts_with_positions",
        AsyncMock(return_value=[(_account(), _positions())]),
    )

    accounts = await routes_simulation.list_simulation_accounts()

    assert accounts[0]["current_capital"] == pytest.approx(900.0)
    assert accounts[0]["market_value"] == pytest.approx(100.0)
    assert accounts[0]["roi_percent"] == pytest.approx(0.0)
