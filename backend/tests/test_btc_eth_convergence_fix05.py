from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.strategies.btc_eth_convergence import BtcEthConvergenceStrategy


def _convergence_row(*, seconds_left: float = 30.0, oracle_age_ms: float | None = 200.0) -> dict:
    now = datetime.now(timezone.utc)
    now_ms = time.time() * 1000.0
    row = {
        "id": "btc-convergence-row",
        "condition_id": "btc-convergence-row",
        "slug": "btc-up-or-down-15m",
        "question": "Will Bitcoin go up in the next 15 minutes?",
        "asset": "BTC",
        "timeframe": "15m",
        "timeframe_seconds": 900,
        "seconds_left": seconds_left,
        "start_time": (now - timedelta(seconds=900 - seconds_left)).isoformat(),
        "end_time": (now + timedelta(seconds=seconds_left)).isoformat(),
        "is_live": True,
        "is_current": True,
        "up_price": 0.90,
        "down_price": 0.10,
        "outcome_prices": [0.90, 0.10],
        "clob_token_ids": ["tok_yes", "tok_no"],
        "liquidity": 10_000.0,
        "spread": 0.01,
        "oracle_diff_pct": 1.0,
        "oracle_price": 78_780.0,
        "price_to_beat": 78_000.0,
        "oracle_source": "binance_direct",
        "market_data_age_ms": 500.0,
    }
    if oracle_age_ms is not None:
        row["oracle_age_seconds"] = oracle_age_ms / 1000.0
        row["oracle_prices_by_source"] = {
            "binance_direct": {
                "source": "binance_direct",
                "price": 78_780.0,
                "updated_at_ms": now_ms - oracle_age_ms,
            }
        }
    return row


def test_convergence_emits_favored_side_inside_final_window():
    strategy = BtcEthConvergenceStrategy()
    opportunities = strategy._detect_from_crypto_markets([_convergence_row()])

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    position = opportunity.positions_to_take[0]
    context = position["_crypto_context"]
    assert position["outcome"] == "YES"
    assert position["price"] == pytest.approx(0.90)
    assert context["selected_direction"] == "buy_yes"
    assert context["p_win"] > position["price"]
    assert context["gross_edge_percent"] > context["edge_percent"] > 0.0
    assert context["convergence_score"] <= strategy.default_config["convergence_max_score"]


def test_convergence_rejects_outside_final_window():
    strategy = BtcEthConvergenceStrategy()
    assert strategy._detect_from_crypto_markets([_convergence_row(seconds_left=300.0)]) == []
    assert strategy._filter_diagnostics["rejections"][0]["gate"] == "convergence_window"


def test_convergence_ml_prior_adjusts_confidence_symmetrically():
    neutral_row = _convergence_row()
    neutral_row["machine_learning"] = {"probability_yes": 0.50}
    directional_row = _convergence_row()
    directional_row["machine_learning"] = {"probability_yes": 0.97}

    neutral = BtcEthConvergenceStrategy()._detect_from_crypto_markets([neutral_row])[0]
    directional = BtcEthConvergenceStrategy()._detect_from_crypto_markets([directional_row])[0]

    assert neutral.confidence < directional.confidence


@pytest.mark.parametrize("oracle_age_ms", [None, 30_000.0])
def test_convergence_rejects_missing_or_stale_oracle_age(oracle_age_ms: float | None):
    strategy = BtcEthConvergenceStrategy()
    assert strategy._detect_from_crypto_markets([_convergence_row(oracle_age_ms=oracle_age_ms)]) == []
    assert any(
        rejection["gate"] == "oracle_freshness"
        for rejection in strategy._filter_diagnostics["rejections"]
    )


def test_convergence_oracle_flip_exits_before_binary_resolution_hold():
    strategy = BtcEthConvergenceStrategy()
    position = SimpleNamespace(
        config={},
        strategy_context={"oracle_diff_pct": 0.8, "seconds_left": 30.0, "timeframe": "15m"},
    )

    decision = strategy.should_exit(
        position,
        {
            "is_resolved": False,
            "oracle_diff_pct": -0.2,
            "seconds_left": 20.0,
            "current_price": 0.88,
        },
    )

    assert decision.action == "close"
    assert "Oracle direction flipped" in decision.reason
    assert decision.close_price == pytest.approx(0.88)


def test_convergence_skipped_evaluation_does_not_build_snapshot():
    strategy = BtcEthConvergenceStrategy()
    original_builder = strategy._build_decision_payload
    strategy._build_decision_payload = Mock(wraps=original_builder)
    signal = SimpleNamespace(
        market_id="skip-convergence",
        direction="buy_yes",
        edge_percent=0.0,
        confidence=0.0,
        entry_price=0.90,
        payload_json={},
    )

    decision = strategy.evaluate(signal, {"params": {"debug_decision_payload": False}})

    assert decision.decision == "skipped"
    assert decision.payload == {}
    strategy._build_decision_payload.assert_not_called()
