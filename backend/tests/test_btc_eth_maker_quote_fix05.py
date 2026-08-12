from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.strategies.btc_eth_maker_quote import BtcEthMakerQuoteStrategy


@pytest.mark.parametrize(
    ("oracle_diff_pct", "expected_skew_sign"),
    [(2.0, 1), (0.5, 1), (-2.0, -1), (-0.5, -1)],
)
def test_maker_skew_is_capped_directional_and_weights_sum_to_one(
    oracle_diff_pct: float,
    expected_skew_sign: int,
):
    skew, yes_weight, no_weight = BtcEthMakerQuoteStrategy._maker_skew({}, oracle_diff_pct)

    assert abs(skew) <= BtcEthMakerQuoteStrategy.default_config["maker_quote_skew_max"]
    assert (skew > 0) is (expected_skew_sign > 0)
    assert yes_weight + no_weight == pytest.approx(1.0)
    assert (yes_weight > no_weight) is (expected_skew_sign > 0)


def test_maker_combined_cost_gate_boundary():
    assert BtcEthMakerQuoteStrategy._maker_combined_cost_ok(
        0.49,
        0.49,
        min_combined_edge=0.015,
        hedge_taker_fee_estimate=0.005,
    )
    assert not BtcEthMakerQuoteStrategy._maker_combined_cost_ok(
        0.49,
        0.49,
        min_combined_edge=0.015,
        hedge_taker_fee_estimate=0.0051,
    )


def test_maker_score_uses_spread_and_thin_book_bonus_with_cap():
    thin = BtcEthMakerQuoteStrategy._maker_score({}, spread=0.02, liquidity=500.0)
    deep = BtcEthMakerQuoteStrategy._maker_score({}, spread=0.02, liquidity=5_000.0)
    capped = BtcEthMakerQuoteStrategy._maker_score({}, spread=1.0, liquidity=500.0)

    assert thin == pytest.approx(48.0)
    assert deep == pytest.approx(33.0)
    assert capped == BtcEthMakerQuoteStrategy.default_config["maker_quote_max_score"]


def _plan_payload(seconds_left: float = 90.0) -> dict:
    return {
        "market_id": "maker-market",
        "oracle_diff_pct": 1.0,
        "seconds_left": seconds_left,
        "timeframe": "5m",
        "size_usd": 100.0,
        "markets": [
            {
                "id": "maker-market",
                "condition_id": "maker-market",
                "question": "Will Bitcoin go up?",
                "clob_token_ids": ["yes-token", "no-token"],
                "outcome_prices": [0.50, 0.50],
                "timeframe": "5m",
                "seconds_left": seconds_left,
            }
        ],
    }


def test_maker_plan_applies_window_size_inventory_and_fee_rules():
    strategy = BtcEthMakerQuoteStrategy()
    signal = SimpleNamespace(market_id="maker-market", market_question="Will Bitcoin go up?")

    plan = strategy._build_maker_quote_execution_plan_override(
        signal=signal,
        payload=_plan_payload(),
        live_market={},
        params={},
        regime="closing",
    )

    assert plan is not None
    yes_leg, no_leg = plan["legs"]
    assert yes_leg["limit_price"] > no_leg["limit_price"]
    assert yes_leg["notional_weight"] + no_leg["notional_weight"] == pytest.approx(1.0)
    assert yes_leg["notional_usd"] == no_leg["notional_usd"] == 25.0
    assert plan["constraints"]["session_timeout_seconds"] == 45
    assert plan["constraints"]["hedge_timeout_seconds"] == 5
    assert plan["constraints"]["max_unhedged_notional_usd"] == pytest.approx(0.50)
    assert plan["metadata"]["maker_fee_estimate_usd"] == 0.0
    assert plan["metadata"]["hedge_taker_fee_estimate_usd"] > 0.0


def test_maker_plan_refuses_to_rest_inside_resolution_risk_window():
    strategy = BtcEthMakerQuoteStrategy()
    signal = SimpleNamespace(market_id="maker-market", market_question="Will Bitcoin go up?")

    plan = strategy._build_maker_quote_execution_plan_override(
        signal=signal,
        payload=_plan_payload(seconds_left=40.0),
        live_market={},
        params={},
        regime="closing",
    )

    assert plan is None


def test_maker_skipped_evaluation_does_not_build_snapshot():
    strategy = BtcEthMakerQuoteStrategy()
    original_builder = strategy._build_decision_payload
    strategy._build_decision_payload = Mock(wraps=original_builder)
    signal = SimpleNamespace(
        market_id="skip-maker",
        direction="buy_yes",
        edge_percent=0.0,
        confidence=0.0,
        entry_price=0.50,
        payload_json={},
    )

    decision = strategy.evaluate(signal, {"params": {"debug_decision_payload": False}})

    assert decision.decision == "skipped"
    assert decision.payload == {}
    strategy._build_decision_payload.assert_not_called()
