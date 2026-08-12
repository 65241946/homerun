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
    confidence: float | None = 0.7,
    side: str = "BUY",
    end_date: str | None = None,
) -> dict:
    payload = {
        "copy_event": {
            "wallet_address": "0xabc",
            "token_id": token_id,
            "side": side,
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
            "signal_type": f"single_wallet_{side.lower()}",
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
            "side": side,
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
    if end_date is not None:
        payload["market"]["end_date"] = end_date
    return payload


def _evaluation_signal(
    *,
    now: datetime,
    age_seconds: float,
    side: str = "BUY",
    price: float = 0.62,
    confidence: float | None = 0.7,
) -> SimpleNamespace:
    detected_at = now - timedelta(seconds=age_seconds)
    copy_event = {
        "wallet_address": "0xabc",
        "token_id": "token-leader",
        "side": side,
        "size": 25.0,
        "price": price,
        "tx_hash": "0xhash",
        "detected_at": detected_at.isoformat(),
        "confidence": confidence,
    }
    source_trade = {
        "wallet_address": "0xabc",
        "side": side,
        "source_notional_usd": 15.5,
        "price": price,
        "tx_hash": "0xhash",
        "detected_at": detected_at.isoformat(),
    }
    signal = SimpleNamespace(
        source="traders",
        strategy_type="traders_copy_trade",
        confidence=confidence,
        entry_price=price,
        payload_json={
            "selected_token_id": "token-leader",
            "strategy_context": {
                "copy_event": copy_event,
                "source_trade": source_trade,
            },
            "source_trade": source_trade,
        },
    )
    if confidence is None:
        del signal.confidence
        copy_event.pop("confidence", None)
    return signal


def _evaluate_at_age(monkeypatch, *, age_seconds: float, params: dict) -> object:
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(copy_trade_module, "utcnow", lambda: now)
    strategy = TradersCopyTradeStrategy()
    strategy.configure(params)
    return strategy.evaluate(
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


def test_missing_confidence_uses_min_confidence_default_in_detect_and_evaluate(monkeypatch):
    strategy = TradersCopyTradeStrategy()
    strategy.configure({})
    opportunity = strategy._build_copy_opportunity(
        _payload(outcome="Yes", price=0.40, confidence=None)
    )

    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(copy_trade_module, "utcnow", lambda: now)
    decision = strategy.evaluate(
        _evaluation_signal(now=now, age_seconds=1, price=0.40, confidence=None),
        {"mode": "shadow", "trader": {"risk_limits": {"max_trade_notional_usd": 100.0}}},
    )

    assert opportunity is not None
    assert opportunity.confidence == pytest.approx(0.45)
    assert opportunity.expected_payout == pytest.approx(0.45)
    assert opportunity.roi_percent == pytest.approx(12.5)
    assert decision.decision == "selected"
    confidence_check = next(check for check in decision.checks if check.key == "confidence")
    assert confidence_check.score == pytest.approx(0.45)


def test_copy_trade_risk_defaults_and_budget_descriptions_are_reconciled():
    defaults = traders_copy_trade_defaults()
    assert defaults["leader_allocation_cap_pct"] == pytest.approx(25.0)
    assert defaults["max_copy_drawdown_pct"] == pytest.approx(50.0)
    assert defaults["require_live_context"] is False

    fields = {field["key"]: field for field in traders_copy_trade_config_schema()["param_fields"]}
    for key in (
        "max_copy_daily_loss_usd",
        "max_copy_source_exposure_usd",
        "max_leader_exposure_usd",
    ):
        assert "上线实盘前必须按账户规模设置" in fields[key]["description"]
    assert "live" in fields["require_live_context"]["description"].lower()


def test_resolution_date_uses_real_market_end_date_only():
    strategy = TradersCopyTradeStrategy()
    real_end = "2026-05-10T12:30:00+00:00"

    with_end = strategy._build_copy_opportunity(
        _payload(outcome="Yes", end_date=real_end)
    )
    without_end = strategy._build_copy_opportunity(_payload(outcome="Yes"))

    assert with_end is not None
    assert with_end.resolution_date == datetime.fromisoformat(real_end)
    assert without_end is not None
    assert without_end.resolution_date is None
    assert "resolution_date" not in without_end.model_fields_set


def test_copy_event_timestamp_is_parsed_once(monkeypatch):
    strategy = TradersCopyTradeStrategy()
    payload = _payload(outcome="Yes")
    timestamp = payload["copy_event"]["timestamp"]
    original_to_utc = copy_trade_module._to_utc
    parsed_values: list[object] = []

    def _counting_to_utc(value):
        parsed_values.append(value)
        return original_to_utc(value)

    monkeypatch.setattr(copy_trade_module, "_to_utc", _counting_to_utc)
    assert strategy._build_copy_opportunity(payload) is not None
    assert parsed_values.count(timestamp) == 1


def test_require_live_context_rejects_missing_liquidity_and_entry_drift(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(copy_trade_module, "utcnow", lambda: now)
    strategy = TradersCopyTradeStrategy()
    strategy.configure({"require_live_context": True})

    decision = strategy.evaluate(
        _evaluation_signal(now=now, age_seconds=1),
        {
            "mode": "live",
            "live_market": {},
            "trader": {"risk_limits": {"max_trade_notional_usd": 100.0}},
        },
    )

    assert decision.decision == "skipped"
    failed_keys = {check.key for check in decision.checks if not check.passed}
    assert {"live_liquidity", "entry_drift"}.issubset(failed_keys)


def test_config_validation_runs_on_configure_not_per_signal(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(copy_trade_module, "utcnow", lambda: now)
    original_validate = copy_trade_module.validate_traders_copy_trade_config
    call_count = 0

    def _counting_validate(config):
        nonlocal call_count
        call_count += 1
        return original_validate(config)

    monkeypatch.setattr(copy_trade_module, "validate_traders_copy_trade_config", _counting_validate)
    strategy = TradersCopyTradeStrategy()
    strategy.configure({"min_confidence": 0.45})
    signal = _evaluation_signal(now=now, age_seconds=1)
    context = {"mode": "shadow", "trader": {"risk_limits": {"max_trade_notional_usd": 100.0}}}

    assert strategy.evaluate(signal, context).decision == "selected"
    assert strategy.evaluate(signal, context).decision == "selected"
    assert call_count == 1


def test_copy_sells_mirror_inventory_reduction_path(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(copy_trade_module, "utcnow", lambda: now)
    strategy = TradersCopyTradeStrategy()
    strategy.configure({"copy_sells": True})

    opportunity = strategy._build_copy_opportunity(_payload(outcome="Yes", side="SELL"))
    decision = strategy.evaluate(
        _evaluation_signal(now=now, age_seconds=1, side="SELL"),
        {
            "mode": "live",
            "copy_inventory_context": {
                "token_inventory": {"token-leader": {"size": 4.0}},
            },
            "trader": {"risk_limits": {"max_trade_notional_usd": 100.0}},
        },
    )

    assert opportunity is not None
    assert opportunity.positions_to_take[0]["action"] == "SELL"
    assert decision.decision == "selected"
    assert next(check for check in decision.checks if check.key == "sell_inventory").passed
    assert next(check for check in decision.checks if check.key == "sell_inventory_fraction").passed
    assert decision.size_usd == pytest.approx(4.0 * 0.62)
