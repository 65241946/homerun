import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.strategies.certainty_shock as certainty_shock_module
from models import Market
from services.strategies.certainty_shock import CertaintyShockStrategy


def _market(*, market_id: str, question: str, end_date: datetime) -> Market:
    return Market(
        id=market_id,
        condition_id=f"condition-{market_id}",
        question=question,
        slug=market_id,
        clob_token_ids=[f"{market_id}-yes", f"{market_id}-no"],
        outcome_prices=[0.75, 0.25],
        active=True,
        closed=False,
        liquidity=10_000.0,
        end_date=end_date,
    )


def test_excluded_crypto_keywords_use_word_boundaries(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(certainty_shock_module, "utcnow", lambda: now)

    non_crypto = CertaintyShockStrategy()
    non_crypto.configure({"exclude_market_keywords": ["eth", "sol"]})
    weather = _market(
        market_id="weather",
        question="Will it rain whether the front moves south?",
        end_date=now + timedelta(days=1),
    )
    solution = _market(
        market_id="solution",
        question="Will the solution summit pass?",
        end_date=now + timedelta(days=1),
    )
    non_crypto.detect([], [weather, solution], {})
    assert "weather" in non_crypto.state["price_history"]
    assert "solution" in non_crypto.state["price_history"]

    crypto = CertaintyShockStrategy()
    crypto.configure({"exclude_market_keywords": ["eth", "sol"]})
    eth = _market(
        market_id="eth",
        question="ETH above $4000?",
        end_date=now + timedelta(days=1),
    )
    crypto.detect([], [eth], {})
    assert "eth" not in crypto.state["price_history"]

    default_crypto = CertaintyShockStrategy()
    solana = _market(
        market_id="solana",
        question="Solana price above $300?",
        end_date=now + timedelta(days=1),
    )
    default_crypto.detect([], [solana], {})
    assert "solana" not in default_crypto.state["price_history"]


def test_default_shock_lookback_is_fifteen_minutes():
    assert CertaintyShockStrategy.default_config["shock_lookback_seconds"] == 900


def test_slow_six_hour_drift_is_not_a_shock_but_fifteen_minute_move_is(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(certainty_shock_module, "utcnow", lambda: now)
    market = _market(
        market_id="shock",
        question="Will the proposal pass by August 13, 2026?",
        end_date=now + timedelta(days=1),
    )
    prices = {"shock-yes": {"mid": 0.75}, "shock-no": {"mid": 0.25}}

    slow = CertaintyShockStrategy()
    slow.configure({"exclude_market_keywords": []})
    slow.state["price_history"] = {
        "shock": [
            ((now - timedelta(hours=6)).timestamp(), 0.52, 0.48),
            ((now - timedelta(minutes=14)).timestamp(), 0.71, 0.29),
            ((now - timedelta(minutes=10)).timestamp(), 0.72, 0.28),
            ((now - timedelta(minutes=6)).timestamp(), 0.73, 0.27),
            ((now - timedelta(minutes=2)).timestamp(), 0.74, 0.26),
        ]
    }
    slow_history = slow.state["price_history"]["shock"] + [(now.timestamp(), 0.75, 0.25)]
    assert slow._recent_move_share(
        slow_history,
        side_index=1,
        scan_time=now.timestamp(),
        recent_window_seconds=900,
    ) < 0.6
    assert slow.detect([], [market], prices) == []

    fast = CertaintyShockStrategy()
    fast.configure({"exclude_market_keywords": []})
    fast.state["price_history"] = {
        "shock": [
            ((now - timedelta(hours=6)).timestamp(), 0.52, 0.48),
            ((now - timedelta(minutes=14)).timestamp(), 0.52, 0.48),
            ((now - timedelta(minutes=10)).timestamp(), 0.56, 0.44),
            ((now - timedelta(minutes=6)).timestamp(), 0.62, 0.38),
            ((now - timedelta(minutes=2)).timestamp(), 0.70, 0.30),
        ]
    }
    opportunities = fast.detect([], [market], prices)
    assert len(opportunities) == 1
    assert opportunities[0].strategy_context["shock_recent_share"] == 1.0


def test_no_shock_uses_no_price_series_for_move_retrace_and_target(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(certainty_shock_module, "utcnow", lambda: now)
    market = _market(
        market_id="no-shock",
        question="Will the proposal fail by August 13, 2026?",
        end_date=now + timedelta(days=1),
    )
    prices = {
        "no-shock-yes": {"mid": 0.50},
        "no-shock-no": {"mid": 0.75},
    }
    strategy = CertaintyShockStrategy()
    strategy.configure({"exclude_market_keywords": []})
    strategy.state["price_history"] = {
        "no-shock": [
            ((now - timedelta(minutes=14)).timestamp(), 0.50, 0.52),
            ((now - timedelta(minutes=10)).timestamp(), 0.50, 0.56),
            ((now - timedelta(minutes=6)).timestamp(), 0.50, 0.62),
            ((now - timedelta(minutes=2)).timestamp(), 0.50, 0.70),
        ]
    }

    opportunities = strategy.detect([], [market], prices)

    assert len(opportunities) == 1
    position = opportunities[0].positions_to_take[0]
    assert position["outcome"] == "NO"
    assert position["price"] == 0.75
    assert "NO repricing upward" in position["rationale"]


def test_dead_zone_validation_raises_expected_move_to_edge_floor():
    strategy = CertaintyShockStrategy()
    strategy.configure({"min_edge_percent": 10.0, "shock_min_expected_move": 0.03})
    assert strategy.config["shock_min_expected_move"] == 0.10

    strategy.configure({"min_edge_percent": 10.0, "shock_min_expected_move": 0.20})
    assert strategy.config["shock_min_expected_move"] == 0.20


def test_risk_gate_default_is_effective_at_point_six():
    strategy = CertaintyShockStrategy()
    signal = SimpleNamespace(
        source="scanner",
        edge_percent=5.0,
        confidence=0.80,
        payload_json={"risk_score": 0.61, "markets": []},
    )

    decision = strategy.evaluate(signal, {"params": {}, "trader": None})

    assert strategy.default_config["max_risk_score"] == 0.60
    assert strategy.pipeline_defaults["max_risk_score"] == 0.60
    assert decision.decision == "skipped"
    assert any(check.key == "risk_score" and not check.passed for check in decision.checks)


def test_deadline_is_cached_by_market_signature(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    market = _market(
        market_id="deadline",
        question="Will this happen?",
        end_date=now + timedelta(days=1),
    )
    strategy = CertaintyShockStrategy()
    calls = 0
    original = strategy._extract_deadline

    def counted_extract(candidate):
        nonlocal calls
        calls += 1
        return original(candidate)

    monkeypatch.setattr(strategy, "_extract_deadline", counted_extract)
    assert strategy._cached_deadline(market) == market.end_date
    assert strategy._cached_deadline(market) == market.end_date
    assert calls == 1


def test_history_is_not_accumulated_before_deadline_gate(monkeypatch):
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(certainty_shock_module, "utcnow", lambda: now)
    market = Market(
        id="no-deadline",
        condition_id="condition-no-deadline",
        question="Will this happen?",
        slug="no-deadline",
        clob_token_ids=["no-deadline-yes", "no-deadline-no"],
        outcome_prices=[0.75, 0.25],
        active=True,
        closed=False,
        liquidity=10_000.0,
        end_date=None,
    )
    strategy = CertaintyShockStrategy()
    strategy.configure({"exclude_market_keywords": []})

    strategy.detect([], [market], {})

    assert "no-deadline" not in strategy.state["price_history"]


def test_market_state_gc_removes_history_and_deadline_cache():
    strategy = CertaintyShockStrategy()
    strategy.state["market_last_seen"] = {"stale": 100.0, "live": 950.0}
    strategy.state["price_history"] = {
        "stale": [(100.0, 0.50, 0.50)],
        "live": [(950.0, 0.60, 0.40)],
    }
    strategy.state["deadline_cache"] = {
        "stale": (("", "stale"), None),
        "live": (("", "live"), None),
    }

    strategy._gc_market_state(scan_time=1000.0, retention_seconds=600.0)

    assert set(strategy.state["market_last_seen"]) == {"live"}
    assert set(strategy.state["price_history"]) == {"live"}
    assert set(strategy.state["deadline_cache"]) == {"live"}
