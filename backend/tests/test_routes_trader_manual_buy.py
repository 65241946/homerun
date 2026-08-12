import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import routes_traders  # noqa: E402


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commit = AsyncMock()

    def add(self, row: object) -> None:
        self.added.append(row)


def _manual_request() -> routes_traders.TraderManualBuyRequest:
    return routes_traders.TraderManualBuyRequest(
        positions=[
            routes_traders.ManualBuyPosition(
                token_id="0x1111111111111111111111111111111111111111",
                side="BUY",
                price=0.40,
                market_id="market-1",
                market_question="Will the test pass?",
                outcome="Yes",
            )
        ],
        size_usd=5.0,
        opportunity_id="manual-opportunity-1",
    )


def _patch_route_dependencies(monkeypatch: pytest.MonkeyPatch, *, mode: str) -> None:
    monkeypatch.setattr(routes_traders, "_assert_not_globally_paused", lambda: None)
    monkeypatch.setattr(
        routes_traders,
        "get_trader",
        AsyncMock(return_value={"id": "trader-1", "mode": mode, "risk_limits": {}}),
    )
    monkeypatch.setattr(routes_traders, "sync_trader_position_inventory", AsyncMock(return_value={}))
    monkeypatch.setattr(routes_traders, "create_trader_event", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_manual_shadow_buy_reuses_fill_result_and_never_mutates_simulation_account(monkeypatch):
    session = _RecordingSession()
    _patch_route_dependencies(monkeypatch, mode="shadow")
    shadow_submit = AsyncMock(
        return_value=SimpleNamespace(
            status="executed",
            effective_price=0.42,
            error_message=None,
            payload={
                "submission": "shadow_microstructure_simulated",
                "filled_size": 10.0,
                "filled_notional_usd": 4.2,
                "shadow_simulation": {"filled": True},
            },
            provider_order_id=None,
            provider_clob_order_id=None,
            shares=10.0,
            notional_usd=4.2,
        )
    )
    monkeypatch.setattr(routes_traders, "submit_execution_leg", shadow_submit)

    result = await routes_traders.manual_buy(
        trader_id="trader-1",
        request=_manual_request(),
        session=session,
    )

    assert result["status"] == "success"
    assert result["orders"][0]["status"] == "executed"
    assert result["orders"][0]["entry_price"] == pytest.approx(0.40)
    assert result["orders"][0]["effective_price"] == pytest.approx(0.42)
    assert result["orders"][0]["notional_usd"] == pytest.approx(4.2)
    assert result["orders"][0]["size_shares"] == pytest.approx(10.0)

    assert len(session.added) == 1
    order = session.added[0]
    assert isinstance(order, routes_traders.TraderOrder)
    assert order.status == "executed"
    assert order.executed_at is not None
    assert order.effective_price == pytest.approx(0.42)
    assert order.notional_usd == pytest.approx(4.2)
    assert order.payload_json["shadow_execution"]["shadow_simulation"]["filled"] is True
    assert "SimulationAccount" not in inspect.getsource(routes_traders.manual_buy)

    shadow_submit.assert_awaited_once()
    submit_kwargs = shadow_submit.await_args.kwargs
    assert submit_kwargs["mode"] == "shadow"
    assert submit_kwargs["trader_id"] == "trader-1"
    assert submit_kwargs["notional_usd"] == pytest.approx(5.0)
    assert submit_kwargs["leg"]["token_id"] == "0x1111111111111111111111111111111111111111"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_shadow_buy_persists_explicit_failure_when_fill_simulator_does_not_fill(monkeypatch):
    session = _RecordingSession()
    _patch_route_dependencies(monkeypatch, mode="shadow")
    monkeypatch.setattr(
        routes_traders,
        "submit_execution_leg",
        AsyncMock(
            return_value=SimpleNamespace(
                status="skipped",
                effective_price=0.40,
                error_message="No order book available for shadow execution leg.",
                payload={"reason": "missing_order_book"},
                provider_order_id=None,
                provider_clob_order_id=None,
                shares=0.0,
                notional_usd=0.0,
            )
        ),
    )

    result = await routes_traders.manual_buy(
        trader_id="trader-1",
        request=_manual_request(),
        session=session,
    )

    assert result["status"] == "partial_failure"
    assert result["orders"][0]["status"] == "failed"
    assert result["orders"][0]["error"] == "No order book available for shadow execution leg."
    order = session.added[0]
    assert order.status == "failed"
    assert order.executed_at is None
    assert order.notional_usd == 0.0
    assert order.payload_json["shadow_execution"]["reason"] == "missing_order_book"


@pytest.mark.asyncio
async def test_manual_live_buy_keeps_existing_live_execution_path(monkeypatch):
    session = _RecordingSession()
    _patch_route_dependencies(monkeypatch, mode="live")
    shadow_submit = AsyncMock(side_effect=AssertionError("shadow helper must not run in live mode"))
    monkeypatch.setattr(routes_traders, "submit_execution_leg", shadow_submit)
    monkeypatch.setattr(routes_traders.live_execution_service, "is_ready", lambda: True)
    monkeypatch.setattr(
        routes_traders.live_execution_service,
        "place_order",
        AsyncMock(
            return_value=SimpleNamespace(
                id="live-order-1",
                status=routes_traders.OrderStatus.FILLED,
                size=12.5,
                price=0.40,
                error_message=None,
            )
        ),
    )

    result = await routes_traders.manual_buy(
        trader_id="trader-1",
        request=_manual_request(),
        session=session,
    )

    assert result["status"] == "success"
    assert result["mode"] == "live"
    assert result["orders"][0]["status"] == "executed"
    order = session.added[0]
    assert order.status == "executed"
    assert order.notional_usd == pytest.approx(5.0)
    assert order.effective_price == pytest.approx(0.40)
    assert order.executed_at is not None
    shadow_submit.assert_not_awaited()
