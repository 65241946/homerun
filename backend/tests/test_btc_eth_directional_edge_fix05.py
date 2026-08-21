from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.strategies.btc_eth_directional_edge import BtcEthDirectionalEdgeStrategy
from services.strategies.crypto_strategy_utils import fee_aware_min_edge_pct


def _fresh_directional_row() -> dict:
    now = datetime.now(timezone.utc)
    now_ms = time.time() * 1000.0
    return {
        "id": "btc-directional-row",
        "condition_id": "btc-directional-row",
        "slug": "btc-up-or-down-15m",
        "question": "Will Bitcoin go up in the next 15 minutes?",
        "asset": "BTC",
        "timeframe": "15m",
        "timeframe_seconds": 900,
        "seconds_left": 240.0,
        "start_time": (now - timedelta(seconds=660)).isoformat(),
        "end_time": (now + timedelta(seconds=240)).isoformat(),
        "is_live": True,
        "is_current": True,
        "up_price": 0.45,
        "down_price": 0.55,
        "outcome_prices": [0.45, 0.55],
        "clob_token_ids": ["tok_yes", "tok_no"],
        "liquidity": 10_000.0,
        "spread": 0.01,
        "oracle_diff_pct": 1.0,
        "oracle_price": 78_780.0,
        "price_to_beat": 78_000.0,
        "oracle_source": "binance_direct",
        "oracle_age_seconds": 0.2,
        "oracle_prices_by_source": {
            "binance_direct": {
                "source": "binance_direct",
                "price": 78_780.0,
                "updated_at_ms": now_ms - 200.0,
            },
        },
    }


def test_directional_probability_is_monotonic_and_late_signal_is_more_extreme():
    strategy = BtcEthDirectionalEdgeStrategy
    low = strategy._directional_probability({}, oracle_diff_pct=0.2, elapsed_ratio=0.2)
    high = strategy._directional_probability({}, oracle_diff_pct=0.8, elapsed_ratio=0.2)
    late = strategy._directional_probability({}, oracle_diff_pct=0.2, elapsed_ratio=0.9)

    assert 0.5 < low < high <= strategy.default_config["directional_oracle_prob_max"]
    assert late > low


def test_directional_execution_fee_gate_rejects_and_passes_at_half_point_boundary():
    entry_price = 0.50
    fee_hurdle = fee_aware_min_edge_pct(entry_price, multiplier=2.0)

    rejected = BtcEthDirectionalEdgeStrategy._execution_net_edge(fee_hurdle + 0.49, entry_price)
    accepted = BtcEthDirectionalEdgeStrategy._execution_net_edge(fee_hurdle + 0.50, entry_price)

    assert rejected < 0.50
    assert accepted == pytest.approx(0.50)


@pytest.mark.parametrize(
    ("p_win", "entry_price", "expected_size"),
    [
        (0.55, 0.50, 8.36),
        (0.60, 0.50, 9.12),
        (0.80, 0.50, 12.16),
        (0.60, 0.58, 7.9619047619),
    ],
)
def test_directional_quarter_kelly_size_table(p_win: float, entry_price: float, expected_size: float):
    size = BtcEthDirectionalEdgeStrategy._directional_kelly_size(
        {},
        base_size=10.0,
        max_size=100.0,
        p_win=p_win,
        entry_price=entry_price,
        confidence=0.8,
        risk_score=0.2,
    )
    assert size == pytest.approx(expected_size)


def test_directional_detect_uses_probability_edge_and_populates_guardrail_context():
    strategy = BtcEthDirectionalEdgeStrategy()
    opportunities = strategy._detect_from_crypto_markets([_fresh_directional_row()])

    assert len(opportunities) == 1
    position = opportunities[0].positions_to_take[0]
    crypto_context = position["_crypto_context"]
    assert crypto_context["p_win"] == pytest.approx(
        position["price"] + crypto_context["edge_percent"] / 100.0
    )
    assert crypto_context["model_prob_yes"] + crypto_context["model_prob_no"] == pytest.approx(1.0)
    assert crypto_context["up_price"] == pytest.approx(0.45)
    assert crypto_context["down_price"] == pytest.approx(0.55)
    assert crypto_context["directional_phase"] == "late"


def test_skipped_evaluation_does_not_build_decision_payload():
    strategy = BtcEthDirectionalEdgeStrategy()
    original_builder = strategy._build_decision_payload
    strategy._build_decision_payload = Mock(wraps=original_builder)
    signal = SimpleNamespace(
        market_id="skip-market",
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
