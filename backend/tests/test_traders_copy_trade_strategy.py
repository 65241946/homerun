"""Regression tests for ``TradersCopyTradeStrategy._build_copy_opportunity``.

Pin the post-fix contract from plan 0018: the strategy emits the
canonical ``buy_yes``/``buy_no`` for binary YES/NO outcomes and an
empty string otherwise so the session_engine fallback resolver
(``_resolve_leg_direction``) reconstructs the direction from
``(side, outcome)``. Emitting a synthetic bare ``"buy"`` here was
the upstream cause of stuck shadow positions on the Sandbox
``traders_copy_trade`` bot.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.strategies.traders_copy_trade as copy_trade_module
from services.strategies.traders_copy_trade import (
    TradersCopyTradeStrategy,
    traders_copy_trade_config_schema,
    traders_copy_trade_defaults,
    validate_traders_copy_trade_config,
)


def _payload(
    *,
    outcome: str,
    token_id: str = "token-leader",
    price: float = 0.62,
    confidence: float = 0.7,
) -> dict:
    return {
        "copy_event": {
            "wallet_address": "0xabc",
            "token_id": token_id,
            "side": "BUY",
            "size": 25.0,
            "price": price,
            "tx_hash": "0xhash",
            "order_hash": "0xorder",
            "log_index": 1,
            "block_number": 123,
            "timestamp": "2026-05-09T10:00:00+00:00",
            "detected_at": "2026-05-09T10:00:01+00:00",
            "latency_ms": 12.5,
            "confidence": confidence,
            "outcome": outcome,
            "market_id": "market-1",
            "market_question": "Test market",
            "market_slug": "test-market",
            "signal_type": "single_wallet_buy",
        },
        "market": {
            "market_id": "market-1",
            "id": "market-1",
            "outcome": outcome,
            "token_id": token_id,
            "question": "Test market",
            "slug": "test-market",
            "liquidity": 10_000.0,
        },
        "source_trade": {
            "wallet_address": "0xabc",
            "side": "BUY",
            "source_notional_usd": 1_000.0,
            "size": 25.0,
            "price": price,
            "tx_hash": "0xhash",
            "order_hash": "0xorder",
            "log_index": 1,
            "detected_at": "2026-05-09T10:00:01+00:00",
        },
        "source_item_id": "src-1",
        "dedupe_key": "dedupe-1",
    }


def _evaluation_signal(*, now: datetime, age_seconds: float) -> SimpleNamespace:
    detected_at = now - timedelta(seconds=age_seconds)
    copy_event = {
        "wallet_address": "0xabc",
        "token_id": "token-leader",
        "side": "BUY",
        "size": 25.0,
        "price": 0.62,
        "tx_hash": "0xhash",
        "detected_at": detected_at.isoformat(),
        "confidence": 0.7,
    }
    source_trade = {
        "wallet_address": "0xabc",
        "side": "BUY",
        "source_notional_usd": 15.5,
        "price": 0.62,
        "tx_hash": "0xhash",
        "detected_at": detected_at.isoformat(),
    }
    return SimpleNamespace(
        source="traders",
        strategy_type="traders_copy_trade",
        confidence=0.7,
        entry_price=0.62,
        payload_json={
            "selected_token_id": "token-leader",
            "strategy_context": {
                "copy_event": copy_event,
                "source_trade": source_trade,
            },
            "source_trade": source_trade,
        },
    )


def _evaluate_at_age(monkeypatch, *, age_seconds: float, params: dict) -> object:
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(copy_trade_module, "utcnow", lambda: now)
    return TradersCopyTradeStrategy().evaluate(
        _evaluation_signal(now=now, age_seconds=age_seconds),
        {
            "params": params,
            "mode": "shadow",
            "trader": {"risk_limits": {"max_trade_notional_usd": 100.0}},
        },
    )


def test_build_copy_opportunity_emits_buy_yes_for_yes_outcome():
    strategy = TradersCopyTradeStrategy()
    opportunity = strategy._build_copy_opportunity(_payload(outcome="Yes"))
    assert opportunity is not None
    assert len(opportunity.positions_to_take) == 1
    assert opportunity.positions_to_take[0]["direction"] == "buy_yes"


def test_build_copy_opportunity_emits_buy_no_for_no_outcome():
    strategy = TradersCopyTradeStrategy()
    opportunity = strategy._build_copy_opportunity(_payload(outcome="No"))
    assert opportunity is not None
    assert len(opportunity.positions_to_take) == 1
    assert opportunity.positions_to_take[0]["direction"] == "buy_no"


def test_build_copy_opportunity_emits_empty_direction_for_non_binary_outcome():
    strategy = TradersCopyTradeStrategy()
    opportunity = strategy._build_copy_opportunity(_payload(outcome="Fighter A"))
    assert opportunity is not None
    assert len(opportunity.positions_to_take) == 1
    assert opportunity.positions_to_take[0]["direction"] == ""
    assert opportunity.positions_to_take[0]["token_id"] == "token-leader"


def test_copy_delay_shifts_signal_freshness_window(monkeypatch):
    params = {"copy_delay_seconds": 30, "max_signal_age_seconds": 5}

    within_window = _evaluate_at_age(monkeypatch, age_seconds=32, params=params)
    after_window = _evaluate_at_age(monkeypatch, age_seconds=40, params=params)

    assert within_window.decision == "selected"
    assert after_window.decision == "skipped"
    assert "max_age" in after_window.reason
    max_age_check = next(check for check in after_window.checks if check.key == "max_age")
    assert "max=35s" in max_age_check.detail


@pytest.mark.parametrize(
    ("age_seconds", "expected_decision"),
    [(4, "selected"), (6, "skipped")],
)
def test_zero_copy_delay_preserves_existing_freshness_behavior(monkeypatch, age_seconds, expected_decision):
    decision = _evaluate_at_age(
        monkeypatch,
        age_seconds=age_seconds,
        params={"copy_delay_seconds": 0, "max_signal_age_seconds": 5},
    )
    assert decision.decision == expected_decision


def test_pathological_copy_delay_logs_explicit_warning(caplog):
    with caplog.at_level(logging.WARNING):
        validate_traders_copy_trade_config(
            {
                "copy_delay_seconds": 30,
                "max_signal_age_seconds_hard_ceiling": 30,
            }
        )

    assert "copy_delay_seconds" in caplog.text
    assert "hard ceiling" in caplog.text


def test_copy_trade_edge_uses_confidence_ev_instead_of_midpoint_distance():
    strategy = TradersCopyTradeStrategy()

    high_price = strategy._build_copy_opportunity(
        _payload(outcome="Yes", price=0.99, confidence=0.70)
    )
    low_price = strategy._build_copy_opportunity(
        _payload(outcome="Yes", price=0.01, confidence=0.70, token_id="token-low")
    )

    assert high_price is not None
    assert low_price is not None
    assert high_price.roi_percent == 0.0
    assert low_price.roi_percent > high_price.roi_percent
    assert low_price.roi_percent == pytest.approx(6900.0)
    assert high_price.expected_payout == pytest.approx(0.70)
    assert low_price.expected_payout == pytest.approx(0.70)


def test_removed_midpoint_edge_parameters_are_not_exposed():
    assert "edge_midpoint" not in traders_copy_trade_defaults()
    assert "edge_multiplier" not in traders_copy_trade_defaults()
    schema_keys = {field["key"] for field in traders_copy_trade_config_schema()["param_fields"]}
    assert "edge_midpoint" not in schema_keys
    assert "edge_multiplier" not in schema_keys
