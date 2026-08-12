import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
            ((now - timedelta(hours=6)).timestamp(), 0.52),
            ((now - timedelta(hours=4)).timestamp(), 0.56),
            ((now - timedelta(hours=2)).timestamp(), 0.62),
            ((now - timedelta(minutes=30)).timestamp(), 0.70),
        ]
    }
    assert slow.detect([], [market], prices) == []

    fast = CertaintyShockStrategy()
    fast.configure({"exclude_market_keywords": []})
    fast.state["price_history"] = {
        "shock": [
            ((now - timedelta(minutes=14)).timestamp(), 0.52),
            ((now - timedelta(minutes=10)).timestamp(), 0.56),
            ((now - timedelta(minutes=6)).timestamp(), 0.62),
            ((now - timedelta(minutes=2)).timestamp(), 0.70),
        ]
    }
    opportunities = fast.detect([], [market], prices)
    assert len(opportunities) == 1
