import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import routes_traders
from models.database import (
    Base,
    ScannerSnapshot,
    SimulationAccount,
    Trader,
    TraderDecision,
    TraderOrder,
    TraderOrchestratorControl,
)
from models.opportunity import Opportunity
from services import shared_state
from tests.postgres_test_db import build_postgres_session_factory


def _wallet_no_opportunity() -> Opportunity:
    return Opportunity(
        id="wallet-opportunity-1",
        stable_id="wallet-opportunity-stable-1",
        strategy="traders_confluence",
        title="Ekaterina Alexandrova vs. opponent",
        description="Wallet consensus buys the NO outcome.",
        total_cost=0.66,
        expected_payout=1.0,
        gross_profit=0.34,
        fee=0.0,
        net_profit=0.34,
        roi_percent=51.5,
        risk_score=0.35,
        confidence=0.82,
        markets=[
            {
                "id": "provider-market-1",
                "condition_id": "0x" + ("1" * 64),
                "question": "Will Ekaterina Alexandrova win?",
                "yes_price": 0.34,
                "no_price": 0.66,
                "tokens": [
                    {"token_id": "1" * 72, "outcome": "YES", "price": 0.34},
                    {"token_id": "2" * 72, "outcome": "NO", "price": 0.66},
                ],
            }
        ],
        positions_to_take=[
            {
                "token_id": "2" * 72,
                "action": "BUY",
                "price": 0.66,
                "market_id": "provider-market-1",
                "market": "Will Ekaterina Alexandrova win?",
                "outcome": "NO",
            }
        ],
        strategy_context={"source_key": "traders", "outcome": "NO"},
    )


def test_manual_buy_signal_uses_server_opportunity_token_and_canonical_no_direction():
    request = routes_traders.TraderManualBuyRequest(
        positions=[
            routes_traders.ManualBuyPosition(
                token_id="",
                side="BUY",
                price=0.66,
                market_id="provider-market-1",
                market_question="Will Ekaterina Alexandrova win?",
                outcome="Ekaterina Alexandrova",
            )
        ],
        size_usd=25.0,
        opportunity_id="wallet-opportunity-1",
        client_request_id="manual-request-1",
        order_type="market",
    )

    signal = routes_traders._build_manual_runtime_signal(
        opportunity=_wallet_no_opportunity(),
        request=request,
        trader_id="trader-1",
        request_fingerprint="fingerprint-1",
    )

    assert signal.direction == "buy_no"
    assert signal.payload_json["selected_token_id"] == "2" * 72
    assert signal.payload_json["selected_outcome_index"] == 1
    assert signal.payload_json["execution_plan"]["legs"] == [
        {
            "leg_id": "manual_leg_1",
            "market_id": "provider-market-1",
            "market_question": "Will Ekaterina Alexandrova win?",
            "token_id": "2" * 72,
            "side": "buy",
            "outcome": "no",
            "direction": "buy_no",
            "limit_price": 0.66,
            "price_policy": "taker_limit",
            "time_in_force": "IOC",
            "post_only": False,
            "notional_weight": 1.0,
            "min_fill_ratio": 0.0,
            "metadata": {
                "condition_id": "0x" + ("1" * 64),
                "outcome_index": 1,
                "manual_request_id": "manual-request-1",
            },
        }
    ]
    assert signal.payload_json["manual_execution"]["request_fingerprint"] == "fingerprint-1"


@pytest.mark.asyncio
async def test_manual_buy_route_uses_authoritative_signal_and_unified_execution_engine(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "routes_trader_manual_buy_engine")
    opportunity = _wallet_no_opportunity()
    engine_calls: list[dict] = []

    class FakeExecutionSessionEngine:
        def __init__(self, session):
            self.session = session

        async def execute_signal(self, **kwargs):
            engine_calls.append(kwargs)
            signal = kwargs["signal"]
            account_id = kwargs["shadow_account_id"]
            order = TraderOrder(
                id="manual-order-1",
                trader_id=kwargs["trader_id"],
                signal_id=signal.id,
                decision_id=kwargs["decision_id"],
                source=signal.source,
                strategy_key="manual_buy",
                market_id=signal.market_id,
                market_question=signal.market_question,
                direction=signal.direction,
                token_id=signal.payload_json["selected_token_id"],
                identity_status="complete",
                mode="shadow",
                status="executed",
                notional_usd=25.0,
                entry_price=0.66,
                effective_price=0.66,
                payload_json={
                    "simulation_ledger": {
                        "account_id": account_id,
                        "trade_id": "simulation-trade-1",
                        "position_id": "simulation-position-1",
                        "cash_ledger_entry_id": "cash-entry-1",
                    }
                },
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            self.session.add(order)
            await self.session.commit()
            return SimpleNamespace(
                session_id="execution-session-1",
                status="completed",
                effective_price=0.66,
                error_message=None,
                orders_written=1,
                payload={},
                created_orders=[{"order_id": order.id, "status": "executed"}],
            )

    try:
        async with session_factory() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add_all(
                [
                    Trader(
                        id="trader-1",
                        name="Manual execution trader",
                        source_configs_json=[
                            {
                                "source_key": "crypto",
                                "strategy_key": "btc_eth_convergence",
                                "strategy_params": {},
                            }
                        ],
                        risk_limits_json={"max_spread_bps": 75.0},
                        metadata_json={},
                        mode="shadow",
                        is_enabled=True,
                        is_paused=False,
                        block_new_orders=False,
                        interval_seconds=60,
                        created_at=now,
                        updated_at=now,
                    ),
                    SimulationAccount(
                        id="shadow-account-1",
                        name="Manual execution account",
                        initial_capital=1000.0,
                        current_capital=1000.0,
                        total_pnl=0.0,
                        total_trades=0,
                    ),
                    TraderOrchestratorControl(
                        id="default",
                        is_enabled=True,
                        is_paused=False,
                        mode="shadow",
                        kill_switch=False,
                        settings_json={"shadow_account_id": "shadow-account-1"},
                        updated_at=now,
                    ),
                    ScannerSnapshot(
                        id=shared_state.TRADERS_SNAPSHOT_ID,
                        opportunities_json=[opportunity.model_dump(mode="json")],
                        running=True,
                        enabled=True,
                        interval_seconds=60,
                        updated_at=now,
                    ),
                ]
            )
            await session.commit()

            monkeypatch.setattr(routes_traders, "ExecutionSessionEngine", FakeExecutionSessionEngine)
            monkeypatch.setattr(routes_traders, "_assert_not_globally_paused", lambda: None)

            request = routes_traders.TraderManualBuyRequest(
                positions=[
                    routes_traders.ManualBuyPosition(
                        token_id="",
                        side="BUY",
                        price=0.66,
                        market_id="provider-market-1",
                        market_question="Will Ekaterina Alexandrova win?",
                        outcome="Ekaterina Alexandrova",
                    )
                ],
                size_usd=25.0,
                opportunity_id=opportunity.id,
                client_request_id="manual-request-engine-1",
                order_type="market",
            )
            result = await routes_traders.manual_buy(
                trader_id="trader-1",
                request=request,
                session=session,
            )
            replay = await routes_traders.manual_buy(
                trader_id="trader-1",
                request=request,
                session=session,
            )

            conflicting_replay = request.model_copy(update={"size_usd": 26.0})
            with pytest.raises(HTTPException) as conflict_exc:
                await routes_traders.manual_buy(
                    trader_id="trader-1",
                    request=conflicting_replay,
                    session=session,
                )

            token_conflict = request.model_copy(
                update={
                    "client_request_id": "manual-request-token-conflict",
                    "positions": [
                        request.positions[0].model_copy(update={"token_id": "1" * 72})
                    ],
                }
            )
            with pytest.raises(HTTPException) as token_exc:
                await routes_traders.manual_buy(
                    trader_id="trader-1",
                    request=token_conflict,
                    session=session,
                )
            token_conflict_decision = await session.get(
                TraderDecision,
                routes_traders._manual_buy_decision_id(
                    "trader-1",
                    token_conflict.client_request_id,
                ),
            )

            stale_request = request.model_copy(
                update={
                    "client_request_id": "manual-request-stale-opportunity",
                    "opportunity_id": "wallet-opportunity-stale",
                }
            )
            with pytest.raises(HTTPException) as stale_exc:
                await routes_traders.manual_buy(
                    trader_id="trader-1",
                    request=stale_request,
                    session=session,
                )
            stale_decision = await session.get(
                TraderDecision,
                routes_traders._manual_buy_decision_id(
                    "trader-1",
                    stale_request.client_request_id,
                ),
            )

            persisted_order = await session.get(TraderOrder, "manual-order-1")
            assert persisted_order is not None
            mismatched_payload = dict(persisted_order.payload_json or {})
            mismatched_ledger = dict(mismatched_payload["simulation_ledger"])
            mismatched_ledger["account_id"] = "shadow-account-other"
            mismatched_payload["simulation_ledger"] = mismatched_ledger
            persisted_order.payload_json = mismatched_payload
            await session.commit()

            persisted_decision = await session.get(
                TraderDecision,
                routes_traders._manual_buy_decision_id(
                    "trader-1",
                    request.client_request_id,
                ),
            )
            assert persisted_decision is not None
            with pytest.raises(HTTPException) as account_mismatch_exc:
                await routes_traders._manual_buy_response_for_decision(
                    session,
                    trader_id="trader-1",
                    decision=persisted_decision,
                    request_fingerprint=routes_traders._manual_buy_request_fingerprint(
                        "trader-1",
                        request,
                    ),
                    expected_mode="shadow",
                    expected_shadow_account_id="shadow-account-1",
                )

        assert result["status"] == "success"
        assert result["account_id"] == "shadow-account-1"
        assert result["session_id"] == "execution-session-1"
        assert replay["session_id"] == result["session_id"]
        assert replay["orders"][0]["order_id"] == result["orders"][0]["order_id"]
        assert len(engine_calls) == 1
        call = engine_calls[0]
        assert call["signal"].direction == "buy_no"
        assert call["signal"].payload_json["selected_token_id"] == "2" * 72
        assert call["shadow_account_id"] == "shadow-account-1"
        assert conflict_exc.value.status_code == 409
        assert "different request" in str(conflict_exc.value.detail)
        assert token_exc.value.status_code == 409
        assert "token changed" in str(token_exc.value.detail)
        assert token_conflict_decision is None
        assert stale_exc.value.status_code == 404
        assert stale_decision is None
        assert account_mismatch_exc.value.status_code == 409
        assert "different Shadow account" in str(account_mismatch_exc.value.detail)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_buy_route_no_fill_returns_failed_and_keeps_account_unchanged(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(
        Base,
        "routes_trader_manual_buy_no_fill",
    )

    class NoFillExecutionSessionEngine:
        def __init__(self, session):
            self.session = session

        async def execute_signal(self, **kwargs):
            return SimpleNamespace(
                session_id="execution-session-no-fill",
                status="completed",
                effective_price=None,
                error_message=None,
                orders_written=0,
                payload={},
                created_orders=[],
            )

    try:
        async with session_factory() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add_all(
                [
                    Trader(
                        id="trader-no-fill",
                        name="Manual no-fill trader",
                        source_configs_json=[],
                        risk_limits_json={"max_spread_bps": 75.0},
                        metadata_json={},
                        mode="shadow",
                        is_enabled=True,
                        is_paused=False,
                        block_new_orders=False,
                        interval_seconds=60,
                        created_at=now,
                        updated_at=now,
                    ),
                    SimulationAccount(
                        id="shadow-account-no-fill",
                        name="Manual no-fill account",
                        initial_capital=1000.0,
                        current_capital=1000.0,
                        total_pnl=0.0,
                        total_trades=0,
                    ),
                    TraderOrchestratorControl(
                        id="default",
                        is_enabled=True,
                        is_paused=False,
                        mode="shadow",
                        kill_switch=False,
                        settings_json={"shadow_account_id": "shadow-account-no-fill"},
                        updated_at=now,
                    ),
                    ScannerSnapshot(
                        id=shared_state.TRADERS_SNAPSHOT_ID,
                        opportunities_json=[_wallet_no_opportunity().model_dump(mode="json")],
                        running=True,
                        enabled=True,
                        interval_seconds=60,
                        updated_at=now,
                    ),
                ]
            )
            await session.commit()
            monkeypatch.setattr(
                routes_traders,
                "ExecutionSessionEngine",
                NoFillExecutionSessionEngine,
            )
            monkeypatch.setattr(routes_traders, "_assert_not_globally_paused", lambda: None)
            request = routes_traders.TraderManualBuyRequest(
                positions=[
                    routes_traders.ManualBuyPosition(
                        token_id="",
                        side="BUY",
                        price=0.66,
                        market_id="provider-market-1",
                        market_question="Will Ekaterina Alexandrova win?",
                        outcome="Ekaterina Alexandrova",
                    )
                ],
                size_usd=25.0,
                opportunity_id="wallet-opportunity-1",
                client_request_id="manual-request-no-fill",
                order_type="market",
            )

            with pytest.raises(HTTPException) as exc_info:
                await routes_traders.manual_buy(
                    trader_id="trader-no-fill",
                    request=request,
                    session=session,
                )

            decision = await session.get(
                TraderDecision,
                routes_traders._manual_buy_decision_id(
                    "trader-no-fill",
                    request.client_request_id,
                ),
            )
            account = await session.get(SimulationAccount, "shadow-account-no-fill")

        assert exc_info.value.status_code == 409
        assert "did not produce a committed order" in str(exc_info.value.detail)
        assert decision is not None
        assert decision.decision == "failed"
        assert account is not None
        assert account.current_capital == pytest.approx(1000.0, rel=1e-9)
        assert account.total_trades == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_buy_route_runtime_gates_block_before_reservation_and_execution(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(
        Base,
        "routes_trader_manual_buy_gates",
    )
    engine_calls: list[dict] = []

    class UnexpectedExecutionSessionEngine:
        def __init__(self, session):
            self.session = session

        async def execute_signal(self, **kwargs):
            engine_calls.append(kwargs)
            raise AssertionError("execution engine must not run while a runtime gate is closed")

    try:
        async with session_factory() as session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            trader_row = Trader(
                id="trader-gated",
                name="Manual gated trader",
                source_configs_json=[],
                risk_limits_json={},
                metadata_json={},
                mode="shadow",
                is_enabled=True,
                is_paused=False,
                block_new_orders=False,
                interval_seconds=60,
                created_at=now,
                updated_at=now,
            )
            control_row = TraderOrchestratorControl(
                id="default",
                is_enabled=True,
                is_paused=False,
                mode="shadow",
                kill_switch=False,
                settings_json={"shadow_account_id": "shadow-account-gated"},
                updated_at=now,
            )
            session.add_all(
                [
                    trader_row,
                    control_row,
                    SimulationAccount(
                        id="shadow-account-gated",
                        name="Manual gated account",
                        initial_capital=1000.0,
                        current_capital=1000.0,
                    ),
                    ScannerSnapshot(
                        id=shared_state.TRADERS_SNAPSHOT_ID,
                        opportunities_json=[_wallet_no_opportunity().model_dump(mode="json")],
                        running=True,
                        enabled=True,
                        interval_seconds=60,
                        updated_at=now,
                    ),
                ]
            )
            await session.commit()
            monkeypatch.setattr(
                routes_traders,
                "ExecutionSessionEngine",
                UnexpectedExecutionSessionEngine,
            )
            monkeypatch.setattr(routes_traders, "_assert_not_globally_paused", lambda: None)

            gate_cases = [
                ("trader_disabled", trader_row, "is_enabled", False),
                ("trader_paused", trader_row, "is_paused", True),
                ("trader_blocked", trader_row, "block_new_orders", True),
                ("orchestrator_disabled", control_row, "is_enabled", False),
                ("orchestrator_paused", control_row, "is_paused", True),
                ("kill_switch", control_row, "kill_switch", True),
            ]
            for case_name, row, field_name, blocked_value in gate_cases:
                setattr(row, field_name, blocked_value)
                await session.commit()
                request = routes_traders.TraderManualBuyRequest(
                    positions=[
                        routes_traders.ManualBuyPosition(
                            token_id="",
                            side="BUY",
                            price=0.66,
                            market_id="provider-market-1",
                            outcome="Ekaterina Alexandrova",
                        )
                    ],
                    size_usd=25.0,
                    opportunity_id="wallet-opportunity-1",
                    client_request_id=f"manual-gate-{case_name}",
                    order_type="market",
                )
                with pytest.raises(HTTPException) as exc_info:
                    await routes_traders.manual_buy(
                        trader_id="trader-gated",
                        request=request,
                        session=session,
                    )
                assert exc_info.value.status_code == 409
                assert await session.get(
                    TraderDecision,
                    routes_traders._manual_buy_decision_id(
                        "trader-gated",
                        request.client_request_id,
                    ),
                ) is None
                setattr(row, field_name, not blocked_value)
                await session.commit()

        assert engine_calls == []
    finally:
        await engine.dispose()
