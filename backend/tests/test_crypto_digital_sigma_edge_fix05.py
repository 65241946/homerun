from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.strategies.crypto_digital_sigma_edge as sigma_module
from services.strategies.crypto_digital_sigma_edge import CryptoDigitalSigmaEdgeStrategy
from utils.kelly import polymarket_taker_fee


NOW_MS = 2_000_000_000_000.0
END_MS = NOW_MS + 150_000.0


def _market(**overrides) -> dict:
    row = {
        "condition_id": "sigma-market",
        "id": "sigma-market",
        "slug": "btc-up-or-down-5m",
        "question": "BTC up or down?",
        "asset": "BTC",
        "timeframe": "5m",
        "end_time": datetime.fromtimestamp(END_MS / 1000.0, tz=timezone.utc).isoformat(),
        "price_to_beat": 100.0,
        "oracle_prices_by_source": {
            "chainlink": {"price": 101.0, "age_ms": 200.0},
        },
        "oracle_history": [
            {"t": NOW_MS - 20_000.0, "p": 100.8},
            {"t": NOW_MS - 10_000.0, "p": 100.9},
            {"t": NOW_MS, "p": 101.0},
        ],
        "best_bid": 0.54,
        "best_ask": 0.55,
        "spread": 0.01,
        "liquidity": 10_000.0,
        "up_price": 0.55,
        "down_price": 0.45,
        "clob_token_ids": ["0x" + "a" * 60, "0x" + "b" * 60],
    }
    row.update(overrides)
    return row


def _strategy(**config) -> CryptoDigitalSigmaEdgeStrategy:
    strategy = CryptoDigitalSigmaEdgeStrategy()
    strategy.configure(config)
    return strategy


def test_missing_zscore_reject_policy_fails_before_vol_estimation(monkeypatch):
    calls = 0

    def _vol(*args, **kwargs):
        nonlocal calls
        calls += 1
        return 0.001, 12, 240.0

    monkeypatch.setattr(sigma_module, "realized_vol_per_sec", _vol)
    strategy = _strategy(missing_recent_move_zscore_policy="reject")
    assert strategy._evaluate_market(_market(), now_ms=NOW_MS) is None
    assert calls == 0


def test_missing_zscore_reduces_confidence_and_uses_canonical_fee(monkeypatch):
    monkeypatch.setattr(
        sigma_module,
        "realized_vol_per_sec",
        lambda *args, **kwargs: (0.001, 12, 240.0),
    )
    strategy = _strategy(
        missing_recent_move_zscore_policy="reduce_confidence",
        missing_recent_move_confidence_multiplier=0.80,
    )
    opp = strategy._evaluate_market(_market(), now_ms=NOW_MS)
    assert opp is not None
    ctx = opp.strategy_context
    assert opp.confidence == pytest.approx(ctx["fair_value"] * 0.80)
    assert ctx["taker_fee"] == pytest.approx(
        polymarket_taker_fee(ctx["executable_cost"], category="crypto")
    )
    assert ctx["net_edge"] == pytest.approx(
        ctx["fair_value"] - ctx["executable_cost"] - ctx["taker_fee"]
    )


def test_realized_vol_is_cached_by_market_history_identity(monkeypatch):
    calls = 0

    def _vol(*args, **kwargs):
        nonlocal calls
        calls += 1
        return 0.001, 12, 240.0

    monkeypatch.setattr(sigma_module, "realized_vol_per_sec", _vol)
    strategy = _strategy(min_edge=1.0)
    row = _market(recent_move_zscore=0.0)
    assert strategy._evaluate_market(row, now_ms=NOW_MS) is None
    assert strategy._evaluate_market(row, now_ms=NOW_MS + 1_000.0) is None
    assert calls == 1
