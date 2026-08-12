from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import Event, Market
from services.strategies.sports_overreaction_fader import (
    SportsOverreactionFaderStrategy,
    _is_sports_market,
)
from services.opportunity_strategy_catalog import build_system_opportunity_strategy_rows
from services.strategy_sdk import StrategySDK
from services.strategy_loader import validate_strategy_source


def _market(**overrides) -> Market:
    payload = {
        "id": "market-1",
        "condition_id": "condition-1",
        "question": "Will inflation fall below 3%?",
        "slug": "inflation-below-three",
    }
    payload.update(overrides)
    return Market(**payload)


def test_sports_classifier_uses_boundaries_and_structured_metadata():
    assert _is_sports_market(_market()) is False
    assert _is_sports_market(_market(sports_market_type="moneyline")) is True

    event = Event(
        id="event-1",
        slug="league-final",
        title="League final",
        category="Sports",
        markets=[],
    )
    assert _is_sports_market(_market(question="Who wins the final?"), event) is True


def test_factory_sports_source_validates_for_db_runtime_loader():
    row = next(
        item
        for item in build_system_opportunity_strategy_rows()
        if item["slug"] == "sports_overreaction_fader"
    )

    validation = validate_strategy_source(row["source_code"], row["class_name"])

    assert validation["valid"] is True, validation["errors"]


def test_missing_token_trade_tape_does_not_fail_confirmatory_flow_gate(monkeypatch):
    live_prices = {"YES": 0.70, "NO": 0.30}
    monkeypatch.setattr(StrategySDK, "is_ws_feed_started", staticmethod(lambda: True))
    monkeypatch.setattr(
        StrategySDK,
        "get_live_price",
        staticmethod(lambda market, prices, side: live_prices[side]),
    )
    monkeypatch.setattr(
        StrategySDK,
        "get_spread_bps",
        staticmethod(lambda market, prices, side: 50.0),
    )
    monkeypatch.setattr(
        StrategySDK,
        "get_price_change",
        staticmethod(lambda token_id, lookback_seconds: {}),
    )
    monkeypatch.setattr(
        StrategySDK,
        "get_trade_volume",
        staticmethod(lambda token_id, lookback_seconds: {"total": 0.0, "trade_count": 0}),
    )
    monkeypatch.setattr(
        StrategySDK,
        "get_buy_sell_imbalance",
        staticmethod(lambda token_id, lookback_seconds: 0.0),
    )

    market = _market(
        question="NFL: Team A vs Team B",
        slug="nfl-team-a-team-b",
        sports_market_type="moneyline",
        clob_token_ids=["yes-token", "no-token"],
        outcome_prices=[0.70, 0.30],
        liquidity=10_000.0,
        end_date=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    strategy = SportsOverreactionFaderStrategy()

    assert strategy.detect([], [market], {}) == []
    live_prices.update({"YES": 0.60, "NO": 0.40})

    opportunities = strategy.detect([], [market], {})

    assert len(opportunities) == 1
    assert opportunities[0].strategy_context["trade_tape_available"] is True
    assert opportunities[0].strategy_context["flow_data_available"] is False

    diagnostics = strategy.get_filter_diagnostics()
    assert diagnostics is not None
    assert diagnostics["markets_scanned"] == 1
    assert diagnostics["sports_markets"] == 1
    assert diagnostics["flow_checks_without_data"] == 1
    assert diagnostics["signals_emitted"] == 1
