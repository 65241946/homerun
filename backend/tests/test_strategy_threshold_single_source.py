import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import Market
from services.strategies.ctf_basic_arb import CTFBasicArbStrategy
from services.strategies.flash_crash_reversion import FlashCrashReversionStrategy
from services.strategies.news_edge import NewsEdgeStrategy
from services.strategies.news_momentum_breakout import NewsMomentumBreakoutStrategy
from services.strategies.stat_arb import StatArbStrategy
from services.strategies.traders_confluence import TradersConfluenceStrategy
from services.strategies.weather_distribution import WeatherDistributionStrategy


def _signal(*, source: str, edge: float, confidence: float, risk_score: float = 0.2, payload=None):
    body = {"risk_score": risk_score, "markets": []}
    body.update(payload or {})
    return SimpleNamespace(
        source=source,
        signal_type="test",
        strategy_type="test",
        edge_percent=edge,
        confidence=confidence,
        payload_json=body,
    )


def _check(decision, key: str):
    return next(check for check in decision.checks if check.key == key)


def test_ctf_detect_and_evaluate_share_configured_edge_threshold():
    strategy = CTFBasicArbStrategy()
    strategy.configure(
        {
            "min_edge_percent": 0.8,
            "gas_buffer_usd": 0.0,
            "min_liquidity": 0.0,
        }
    )
    market = Market(
        id="ctf-market",
        condition_id="0x" + ("1" * 64),
        question="Will the CTF bundle settle?",
        slug="ctf-market",
        clob_token_ids=["ctf-yes", "ctf-no"],
        outcome_prices=[0.5, 0.5],
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        liquidity=10_000.0,
    )
    opportunities = strategy.detect(
        [],
        [market],
        {
            "ctf-yes": {"bid": 0.522, "ask": 0.60},
            "ctf-no": {"bid": 0.522, "ask": 0.60},
        },
    )
    assert len(opportunities) == 1

    decision = strategy.evaluate(
        _signal(
            source="scanner",
            edge=opportunities[0].roi_percent,
            confidence=opportunities[0].confidence,
            risk_score=opportunities[0].risk_score,
            payload={"is_guaranteed": True},
        ),
        {"params": {}, "trader": None},
    )
    assert decision.decision == "selected"
    assert "min=0.80" in _check(decision, "edge").detail

    strategy.configure({"min_edge_percent": 2.0, "gas_buffer_usd": 0.0, "min_liquidity": 0.0})
    assert strategy.detect(
        [],
        [market],
        {
            "ctf-yes": {"bid": 0.522, "ask": 0.60},
            "ctf-no": {"bid": 0.522, "ask": 0.60},
        },
    ) == []
    blocked = strategy.evaluate(
        _signal(source="scanner", edge=opportunities[0].roi_percent, confidence=0.78, risk_score=0.18),
        {"params": {}, "trader": None},
    )
    assert _check(blocked, "edge").passed is False
    assert "min=2.00" in _check(blocked, "edge").detail


def test_news_edge_detect_and_evaluate_share_configured_thresholds():
    strategy = NewsEdgeStrategy()
    detector_payload = {
        "edge_percent": 6.0,
        "model_probability": 0.56,
        "model_probability_ci_low": 0.51,
        "model_probability_ci_high": 0.61,
        "market_price": 0.50,
        "category": "geopolitics",
        "confidence": 0.80,
        "supporting_articles": [
            {"source": "source-a"},
            {"source": "source-b"},
        ],
    }
    signal = _signal(
        source="news",
        edge=4.0,
        confidence=0.80,
        risk_score=0.50,
        payload={"model_probability": 0.70},
    )

    strategy.configure({"min_edge_percent": 5.0, "min_confidence": 0.45, "require_verifier": False})
    assert strategy._passes_filters(detector_payload) is False
    blocked = strategy.evaluate(signal, {"params": {}, "trader": None})
    assert _check(blocked, "edge").passed is False
    assert "min=5.00" in _check(blocked, "edge").detail

    strategy.configure({"min_edge_percent": 4.0, "min_confidence": 0.75, "require_verifier": False})
    assert strategy._passes_filters(detector_payload) is True
    selected = strategy.evaluate(signal, {"params": {}, "trader": None})
    assert selected.decision == "selected"
    assert "min=4.00" in _check(selected, "edge").detail
    assert "min=0.75" in _check(selected, "confidence").detail


def test_custom_evaluate_liquidity_fallbacks_use_detection_config():
    flash = FlashCrashReversionStrategy()
    flash_signal = _signal(
        source="scanner",
        edge=5.0,
        confidence=0.8,
        payload={"strategy_type": flash.strategy_type},
    )
    flash_signal.liquidity = 2_000.0
    flash_signal.strategy_type = flash.strategy_type
    flash_checks = flash.custom_checks(flash_signal, {}, {}, flash_signal.payload_json)
    assert _check(SimpleNamespace(checks=flash_checks), "liquidity").passed is False
    assert "min=2500" in _check(SimpleNamespace(checks=flash_checks), "liquidity").detail

    breakout = NewsMomentumBreakoutStrategy()
    breakout_signal = _signal(
        source="scanner",
        edge=5.0,
        confidence=0.8,
        payload={"strategy_type": breakout.strategy_type},
    )
    breakout_signal.liquidity = 2_500.0
    breakout_signal.strategy_type = breakout.strategy_type
    breakout_checks = breakout.custom_checks(breakout_signal, {}, {}, breakout_signal.payload_json)
    assert _check(SimpleNamespace(checks=breakout_checks), "liquidity").passed is False
    assert "min=3000" in _check(SimpleNamespace(checks=breakout_checks), "liquidity").detail


def test_pipeline_fallbacks_use_each_strategy_config_source():
    cases = [
        (StatArbStrategy(), "scanner", 4.0, 0.8, "edge", "min=5.00"),
        (TradersConfluenceStrategy(), "traders", 3.0, 0.44, "confidence", "min=0.45"),
        (WeatherDistributionStrategy(), "weather", 4.0, 0.49, "edge", "min=5.00"),
    ]
    for strategy, source, edge, confidence, check_key, expected_detail in cases:
        decision = strategy.evaluate(
            _signal(source=source, edge=edge, confidence=confidence),
            {"params": {}, "trader": None},
        )
        check = _check(decision, check_key)
        assert check.passed is False
        assert expected_detail in check.detail
