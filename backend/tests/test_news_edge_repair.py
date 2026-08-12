import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import Market
from services.news.edge_estimator import EdgeEstimator, NewsEdge
from services.news.feed_service import NewsArticle
from services.news.semantic_matcher import MarketInfo, NewsMarketMatch
from services.strategies.news_edge import NewsEdgeStrategy
from utils.kelly import polymarket_taker_fee


def _article(article_id: str = "article-1") -> NewsArticle:
    return NewsArticle(
        article_id=article_id,
        title="Material event changes forecast",
        url=f"https://example.test/{article_id}",
        source="source-a",
        published=datetime.now(timezone.utc),
        summary="Direct and novel evidence.",
    )


def _match(*, yes_price: float = 0.40, category: str = "crypto") -> NewsMarketMatch:
    return NewsMarketMatch(
        article=_article(),
        market=MarketInfo(
            market_id="market-1",
            question="Will the event happen?",
            event_title="Event",
            category=category,
            yes_price=yes_price,
            no_price=1.0 - yes_price,
        ),
        similarity=0.90,
    )


def _market() -> Market:
    return Market(
        id="market-1",
        condition_id="condition-1",
        question="Will the event happen?",
        slug="market-1",
        clob_token_ids=["yes-token", "no-token"],
        outcome_prices=[0.40, 0.60],
        liquidity=10_000.0,
    )


def test_age_zero_is_not_replaced_by_later_truthy_age():
    assert NewsEdgeStrategy._extract_signal_age_minutes(
        {"age_minutes": 0, "news_age_minutes": 17}
    ) == 0.0


def test_edge_decay_halves_at_configured_half_life():
    assert NewsEdgeStrategy._decayed_edge_percent(12.0, 45.0, 45.0) == pytest.approx(6.0)


def test_ci_gate_and_probability_shrinkage_are_independent():
    strategy = NewsEdgeStrategy()
    strategy.configure(
        {
            "llm_shrinkage_k": 0.7,
            "require_ci_clears_market": True,
            "edge_half_life_minutes_by_category": {"default": 45},
        }
    )

    crossing = strategy._calculate_edge_metrics(
        direction="buy_yes",
        model_probability_yes=0.80,
        market_price_yes=0.60,
        entry_price=0.60,
        confidence=0.80,
        age_minutes=0.0,
        platform="polymarket",
        category="geopolitics",
        uncertainty_payload={"model_probability_ci_low": 0.55, "model_probability_ci_high": 0.85},
    )

    assert crossing["adjusted_probability_yes"] == pytest.approx(0.74)
    assert crossing["gross_edge_percent"] == pytest.approx(14.0)
    assert crossing["ci_passed"] is False


def test_fee_gate_uses_canonical_category_curve_before_minimum_edge():
    strategy = NewsEdgeStrategy()
    strategy.configure(
        {
            "min_edge_percent": 14.0,
            "llm_shrinkage_k": 1.0,
            "require_ci_clears_market": True,
            "require_verifier": False,
            "edge_half_life_minutes_by_category": {"default": 45},
        }
    )
    base_payload = {
        "edge_percent": 15.0,
        "model_probability": 0.65,
        "model_probability_ci_low": 0.60,
        "model_probability_ci_high": 0.70,
        "market_price": 0.50,
        "entry_price": 0.50,
        "direction": "buy_yes",
        "confidence": 0.90,
        "age_minutes": 0,
        "platform": "polymarket",
        "supporting_articles": [{"source": "a"}, {"source": "b"}],
    }

    no_fee_payload = {**base_payload, "category": "geopolitics"}
    crypto_payload = {**base_payload, "category": "crypto"}
    expected_crypto_net = 15.0 - polymarket_taker_fee(0.50, category="crypto") * 100.0

    assert strategy._payload_edge_metrics(crypto_payload)["fee_adjusted_edge_percent"] == pytest.approx(
        expected_crypto_net
    )
    assert strategy._passes_filters(no_fee_payload) is True
    assert strategy._passes_filters(crypto_payload) is False


def test_live_price_refresh_recalculates_edge_before_emit():
    strategy = NewsEdgeStrategy()
    strategy.configure(
        {
            "min_edge_percent": 5.0,
            "llm_shrinkage_k": 1.0,
            "require_ci_clears_market": False,
            "edge_half_life_minutes_by_category": {"crypto": 45, "default": 45},
        }
    )
    match = _match(yes_price=0.40)
    edge = NewsEdge(
        match=match,
        model_probability=0.60,
        market_price=0.40,
        edge_percent=20.0,
        direction="buy_yes",
        confidence=0.90,
        reasoning="test",
        estimated_at=datetime.now(timezone.utc),
    )

    metrics = strategy._refresh_edge_metrics(edge, _market(), {"yes-token": {"mid": 0.58}})

    assert metrics is not None
    assert metrics["entry_price"] == pytest.approx(0.58)
    assert metrics["gross_edge_percent"] == pytest.approx(2.0)
    assert metrics["fee_adjusted_edge_percent"] < 5.0


@pytest.mark.asyncio
async def test_estimator_cache_key_reuses_same_article_market_and_rounded_price(monkeypatch):
    estimator = EdgeEstimator()
    calls = 0

    async def fake_call_llm(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "probability_yes": 0.70,
            "confidence": 0.90,
            "reasoning": "direct evidence",
            "news_relevance": "high",
            "information_novelty": "breaking",
        }

    monkeypatch.setattr(estimator, "_call_llm", fake_call_llm)
    match = _match(yes_price=0.401)

    assert await estimator._estimate_semantic_match(match) is not None
    match.market.yes_price = 0.404
    assert await estimator._estimate_semantic_match(match) is not None
    assert calls == 1

    match.market.yes_price = 0.416
    assert await estimator._estimate_semantic_match(match) is not None
    assert calls == 2


def test_embedding_selection_tracks_article_ids_only_once():
    strategy = NewsEdgeStrategy()
    first = _article("first")
    second = _article("second")

    assert strategy._select_articles_for_embedding([first, second]) == [first, second]
    strategy._mark_articles_embedded([first, second])
    assert strategy._select_articles_for_embedding([first, second]) == []


def test_score_uses_net_edge_plus_small_confidence_term():
    strategy = NewsEdgeStrategy()
    assert strategy.compute_score(8.0, 0.60, 0.5, 1, {}) == pytest.approx(14.0)


def test_default_cache_ttl_is_at_least_three_scan_cycles():
    estimator = EdgeEstimator()
    assert estimator._cache_ttl_seconds >= 3 * 60
    assert math.isfinite(estimator._cache_ttl_seconds)
