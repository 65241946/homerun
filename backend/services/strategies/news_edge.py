# SEED TEMPLATE — not the live strategy. The DB `strategies.source_code` is the
# runtime master (loaded by the orchestrator + backtester); edits here only seed
# fresh installs / reset-to-factory. See services/opportunity_strategy_catalog.py.
"""
Strategy 10: News-Driven Edge Detection

Detects when breaking news creates an informational edge on a prediction
market — i.e., the LLM-estimated probability (given the news) diverges
from the current market price by more than a threshold.

Unlike the other 9 strategies which detect STRUCTURAL mispricings
(mathematical guarantees like YES+NO < $1), this strategy detects
INFORMATIONAL mispricings (the market hasn't priced in the news yet).

Pipeline:
  1. News feed service fetches articles from RSS/GDELT
  2. Semantic matcher embeds articles + markets, finds matches
  3. Edge detector estimates probability via LLM, computes edge
  4. This strategy converts edges into Opportunity objects

Because this is async (LLM calls), it runs differently from the sync
strategies. The scanner calls detect_async() instead of detect().
"""

from __future__ import annotations

import asyncio
import math

from utils.utcnow import utcnow  # replay-clock-aware "now" (honors backtest sim time)
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from models import Opportunity, Event, Market
from models.opportunity import MispricingType
from services.news.edge_estimator import NewsEdge, edge_estimator
from services.news.feed_service import news_feed_service
from services.news.semantic_matcher import MarketInfo, semantic_matcher
from services.strategies.base import (
    BaseStrategy,
    DecisionCheck,
    ScoringWeights,
    SizingConfig,
    ExitDecision,
    StrategyDecision,
    _trader_size_limits,
)
from services.data_events import DataEvent
from services.quality_filter import QualityFilterOverrides
from services.strategy_sdk import StrategySDK
from utils.converters import coerce_bool as _coerce_bool, to_float, to_confidence
from utils.kelly import kalshi_taker_fee, polymarket_taker_fee
from utils.signal_helpers import signal_payload

logger = logging.getLogger(__name__)

# Single-thread executor so all semantic_matcher + FAISS native calls run on
# the same OS thread.  PyTorch / FAISS use thread-local state; dispatching
# to arbitrary pool threads via the default executor causes segfaults.
_MATCHER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="semantic")

NEWS_EDGE_DEFAULT_CONFIG: dict[str, Any] = {
    "min_edge_percent": 5.0,
    "min_confidence": 0.45,
    "orchestrator_min_edge": 10.0,
    "require_verifier": True,
    "require_second_source": False,
    "min_supporting_articles": 2,
    "min_supporting_sources": 2,
    "max_signal_age_minutes": 60,
    "require_ci_clears_market": True,
    "llm_shrinkage_k": 0.7,
    "edge_half_life_minutes_by_category": {"default": 45.0},
}

NEWS_EDGE_CONFIG_SCHEMA: dict[str, Any] = {
    "param_fields": [
        {"key": "min_edge_percent", "label": "Min Edge (%)", "type": "number", "min": 0, "max": 100},
        {"key": "min_confidence", "label": "Min Confidence", "type": "number", "min": 0, "max": 1},
        {"key": "orchestrator_min_edge", "label": "Min Intent Edge (%)", "type": "number", "min": 0, "max": 100},
        {"key": "require_verifier", "label": "Require Verifier", "type": "boolean", "phase": "signal"},
        {"key": "require_second_source", "label": "Require Second Source", "type": "boolean", "phase": "signal"},
        {"key": "min_supporting_articles", "label": "Min Supporting Articles", "type": "integer", "min": 1, "max": 10, "phase": "signal"},
        {"key": "min_supporting_sources", "label": "Min Supporting Sources", "type": "integer", "min": 1, "max": 10, "phase": "signal"},
        {"key": "max_signal_age_minutes", "label": "Max Signal Age (Minutes)", "type": "integer", "min": 1, "max": 1440, "phase": "signal"},
        {"key": "require_ci_clears_market", "label": "Require CI Beyond Market", "type": "boolean", "phase": "signal"},
        {"key": "llm_shrinkage_k", "label": "LLM Probability Shrinkage", "type": "number", "min": 0, "max": 1, "phase": "signal"},
        {
            "key": "edge_half_life_minutes_by_category",
            "label": "Edge Half-life by Category",
            "type": "object",
            "phase": "signal",
            "description": "Category-specific decay half-life in minutes; default is used for unknown categories.",
        },
    ]
}


def news_edge_defaults() -> dict[str, Any]:
    defaults = dict(NEWS_EDGE_DEFAULT_CONFIG)
    defaults["edge_half_life_minutes_by_category"] = dict(
        NEWS_EDGE_DEFAULT_CONFIG["edge_half_life_minutes_by_category"]
    )
    return defaults


def news_edge_config_schema() -> dict[str, Any]:
    return dict(NEWS_EDGE_CONFIG_SCHEMA)


def _coerce_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(lo, min(hi, parsed))


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = default
    return max(lo, min(hi, parsed))


def validate_news_edge_config(config: Any) -> dict[str, Any]:
    cfg = news_edge_defaults()
    raw = config if isinstance(config, dict) else {}
    cfg.update({str(k): v for k, v in raw.items() if str(k) != "_schema"})

    cfg["min_edge_percent"] = _coerce_float(cfg.get("min_edge_percent"), 5.0, 0.0, 100.0)
    cfg["min_confidence"] = _coerce_float(cfg.get("min_confidence"), 0.45, 0.0, 1.0)
    cfg["orchestrator_min_edge"] = _coerce_float(cfg.get("orchestrator_min_edge"), 10.0, 0.0, 100.0)
    cfg["require_verifier"] = _coerce_bool(cfg.get("require_verifier"), True)
    cfg["require_second_source"] = _coerce_bool(cfg.get("require_second_source"), False)
    cfg["min_supporting_articles"] = _coerce_int(cfg.get("min_supporting_articles"), 2, 1, 10)
    cfg["min_supporting_sources"] = _coerce_int(cfg.get("min_supporting_sources"), 2, 1, 10)
    cfg["max_signal_age_minutes"] = _coerce_int(cfg.get("max_signal_age_minutes"), 60, 1, 1440)
    cfg["require_ci_clears_market"] = _coerce_bool(cfg.get("require_ci_clears_market"), True)
    cfg["llm_shrinkage_k"] = _coerce_float(cfg.get("llm_shrinkage_k"), 0.7, 0.0, 1.0)
    raw_half_lives = cfg.get("edge_half_life_minutes_by_category")
    if not isinstance(raw_half_lives, dict):
        raw_half_lives = {}
    half_lives: dict[str, float] = {}
    for raw_category, raw_minutes in raw_half_lives.items():
        category = str(raw_category or "").strip().lower()
        if category:
            half_lives[category] = _coerce_float(raw_minutes, 45.0, 1.0, 10_080.0)
    half_lives.setdefault("default", 45.0)
    cfg["edge_half_life_minutes_by_category"] = half_lives
    return StrategySDK.normalize_strategy_retention_config(cfg)


class NewsEdgeStrategy(BaseStrategy):
    """
    Strategy 10: News-Driven Edge Detection

    Detects informational mispricings by matching news articles to
    markets and estimating probability shifts via LLM.
    """

    strategy_type = "news_edge"
    name = "News Edge"
    description = "Detect news-driven mispricings via semantic matching + LLM probability estimation"
    mispricing_type = "news_information"
    source_key = "news"
    requires_news_data = True
    allow_deduplication = False
    subscriptions = ["news_update"]

    quality_filter_overrides = QualityFilterOverrides(
        min_roi=3.0,
        max_resolution_months=6.0,
    )

    DEFAULT_CONFIG = news_edge_defaults()
    default_config = dict(DEFAULT_CONFIG)

    def __init__(self) -> None:
        super().__init__()
        self._config: dict[str, Any] = validate_news_edge_config(self.default_config)
        self._embedded_article_ids: set[str] = set()

    def configure(self, config: dict) -> None:
        self._config = validate_news_edge_config(config)

    def _select_articles_for_embedding(self, articles: list[Any]) -> list[Any]:
        current_article_ids = {str(article.article_id) for article in articles}
        self._embedded_article_ids.intersection_update(current_article_ids)
        return [
            article for article in articles if str(article.article_id) not in self._embedded_article_ids
        ]

    def _mark_articles_embedded(self, articles: list[Any]) -> None:
        self._embedded_article_ids.update(str(article.article_id) for article in articles)

    def detect(self, events: list[Event], markets: list[Market], prices: dict[str, dict]) -> list[Opportunity]:
        """Sync detect -- not used for this strategy.

        NewsEdgeStrategy requires async I/O (news fetching, LLM calls).
        The scanner calls detect_async() instead.
        """
        return []

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except Exception:
            return default
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return default
        return parsed

    @staticmethod
    def _coerce_optional_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except Exception:
            return None
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return None
        return parsed

    @classmethod
    def _resolve_uncertainty(
        cls,
        payload: dict[str, Any],
        model_probability_yes: float,
        confidence: float,
    ) -> tuple[float, float, float, float]:
        ci_low = cls._coerce_optional_float(payload.get("model_probability_ci_low"))
        ci_high = cls._coerce_optional_float(payload.get("model_probability_ci_high"))

        if ci_low is not None and ci_high is not None and ci_low <= ci_high:
            resolved_low = max(0.0, min(1.0, ci_low))
            resolved_high = max(0.0, min(1.0, ci_high))
        else:
            confidence_c = max(0.0, min(1.0, confidence))
            half_width = 0.03 + ((1.0 - confidence_c) * 0.32)
            resolved_low = max(0.0, model_probability_yes - half_width)
            resolved_high = min(1.0, model_probability_yes + half_width)

        interval_width = max(0.0, resolved_high - resolved_low)
        confidence_uncertainty = max(0.0, min(1.0, 1.0 - confidence))
        return resolved_low, resolved_high, interval_width, confidence_uncertainty

    @staticmethod
    def _extract_supporting_articles(payload: dict[str, Any]) -> list[dict[str, Any]]:
        supporting_articles = payload.get("supporting_articles")
        if isinstance(supporting_articles, list):
            return [item for item in supporting_articles if isinstance(item, dict)]
        metadata = payload.get("metadata_json")
        if isinstance(metadata, dict):
            nested = metadata.get("supporting_articles")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            finding_meta = metadata.get("finding")
            if isinstance(finding_meta, dict):
                evidence = finding_meta.get("evidence")
                if isinstance(evidence, dict):
                    cluster = evidence.get("cluster")
                    if isinstance(cluster, dict) and isinstance(cluster.get("article_refs"), list):
                        return [item for item in cluster.get("article_refs", []) if isinstance(item, dict)]
        evidence = payload.get("evidence")
        if isinstance(evidence, dict):
            cluster = evidence.get("cluster")
            if isinstance(cluster, dict) and isinstance(cluster.get("article_refs"), list):
                return [item for item in cluster.get("article_refs", []) if isinstance(item, dict)]
        fallback = {
            "article_id": str(payload.get("article_id") or ""),
            "title": str(payload.get("article_title") or ""),
            "url": str(payload.get("article_url") or ""),
            "source": str(payload.get("article_source") or ""),
        }
        if any(fallback.values()):
            return [fallback]
        return []

    @staticmethod
    def _count_supporting_sources(articles: list[dict[str, Any]]) -> int:
        sources: set[str] = set()
        for article in articles:
            source = str(article.get("source") or "").strip().lower()
            if source:
                sources.add(source)
        return len(sources)

    @staticmethod
    def _extract_rejection_reasons(payload: dict[str, Any]) -> set[str]:
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict):
            finding_meta = payload.get("finding")
            if isinstance(finding_meta, dict) and isinstance(finding_meta.get("evidence"), dict):
                evidence = finding_meta.get("evidence")
        if not isinstance(evidence, dict):
            metadata = payload.get("metadata_json")
            if isinstance(metadata, dict):
                finding_meta = metadata.get("finding")
                if isinstance(finding_meta, dict) and isinstance(finding_meta.get("evidence"), dict):
                    evidence = finding_meta.get("evidence")
        if not isinstance(evidence, dict):
            return set()
        reasons = evidence.get("rejection_reasons")
        if not isinstance(reasons, list):
            return set()
        return {str(reason).strip().lower() for reason in reasons if str(reason).strip()}

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def _extract_signal_age_minutes(cls, payload: dict[str, Any]) -> float | None:
        for key in (
            "age_minutes",
            "news_age_minutes",
            "minutes_since_published",
            "article_age_minutes",
        ):
            if key not in payload or payload.get(key) is None:
                continue
            explicit_age = cls._coerce_optional_float(payload.get(key))
            if explicit_age is not None and explicit_age >= 0:
                return float(explicit_age)

        now = utcnow()
        for key in (
            "article_published_at",
            "published_at",
            "created_at",
            "detected_at",
            "updated_at",
        ):
            parsed = cls._parse_dt(payload.get(key))
            if parsed is None:
                continue
            age = (now - parsed).total_seconds() / 60.0
            if age >= 0:
                return float(age)
        return None

    @staticmethod
    def _decayed_edge_percent(edge_percent: float, age_minutes: float | None, half_life_minutes: float) -> float:
        if age_minutes is None or age_minutes <= 0.0:
            return max(0.0, edge_percent)
        half_life = max(1.0, half_life_minutes)
        return max(0.0, edge_percent) * math.exp(-math.log(2.0) * age_minutes / half_life)

    def _edge_half_life_minutes(self, category: str | None) -> float:
        configured = self._config.get("edge_half_life_minutes_by_category")
        half_lives = configured if isinstance(configured, dict) else {"default": 45.0}
        category_key = str(category or "").strip().lower()
        return self._coerce_float(half_lives.get(category_key, half_lives.get("default", 45.0)), 45.0)

    @staticmethod
    def _canonical_fee_adjusted_edge_pct(
        edge_percent: float,
        entry_price: float,
        *,
        platform: str,
        category: str | None,
    ) -> float:
        platform_key = str(platform or "polymarket").strip().lower()
        if platform_key == "polymarket":
            fee = polymarket_taker_fee(entry_price, category=category)
        elif platform_key == "kalshi":
            fee = kalshi_taker_fee(entry_price)
        else:
            fee = 0.0
        return edge_percent - (fee * 100.0)

    @staticmethod
    def _ci_is_beyond_market(direction: str, ci_low: float, ci_high: float, market_price_yes: float) -> bool:
        if direction == "buy_yes":
            return ci_low > market_price_yes
        return ci_high < market_price_yes

    def _calculate_edge_metrics(
        self,
        *,
        direction: str,
        model_probability_yes: float,
        market_price_yes: float,
        entry_price: float,
        confidence: float,
        age_minutes: float | None,
        platform: str,
        category: str | None,
        uncertainty_payload: dict[str, Any],
    ) -> dict[str, float | bool]:
        model_probability_yes = max(0.0, min(1.0, model_probability_yes))
        market_price_yes = max(0.0, min(1.0, market_price_yes))
        entry_price = max(0.0, min(1.0, entry_price))
        shrinkage = float(self._config.get("llm_shrinkage_k", 0.7))
        adjusted_probability_yes = market_price_yes + shrinkage * (model_probability_yes - market_price_yes)
        adjusted_target = (
            adjusted_probability_yes if direction == "buy_yes" else (1.0 - adjusted_probability_yes)
        )
        gross_edge_percent = max(0.0, adjusted_target - entry_price) * 100.0
        ci_low, ci_high, ci_width, confidence_uncertainty = self._resolve_uncertainty(
            uncertainty_payload,
            model_probability_yes,
            confidence,
        )
        ci_passed = self._ci_is_beyond_market(direction, ci_low, ci_high, market_price_yes)
        decayed_edge_percent = self._decayed_edge_percent(
            gross_edge_percent,
            age_minutes,
            self._edge_half_life_minutes(category),
        )
        fee_adjusted_edge_percent = self._canonical_fee_adjusted_edge_pct(
            decayed_edge_percent,
            entry_price,
            platform=platform,
            category=category,
        )
        return {
            "adjusted_probability_yes": adjusted_probability_yes,
            "gross_edge_percent": gross_edge_percent,
            "decayed_edge_percent": decayed_edge_percent,
            "fee_adjusted_edge_percent": fee_adjusted_edge_percent,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "ci_width": ci_width,
            "confidence_uncertainty": confidence_uncertainty,
            "ci_passed": ci_passed,
        }

    def _payload_edge_metrics(self, payload: dict[str, Any]) -> dict[str, float | bool]:
        direction = str(payload.get("direction") or "buy_yes").strip().lower()
        market_price_yes = max(0.0, min(1.0, self._coerce_float(payload.get("market_price"), 0.5)))
        raw_edge_percent = max(0.0, self._coerce_float(payload.get("edge_percent"), 0.0))
        raw_model_probability = self._coerce_optional_float(payload.get("model_probability"))
        if raw_model_probability is None:
            signed_edge = raw_edge_percent / 100.0
            raw_model_probability = (
                market_price_yes + signed_edge if direction == "buy_yes" else market_price_yes - signed_edge
            )
        entry_price = self._coerce_float(payload.get("entry_price"), -1.0)
        if entry_price <= 0.0:
            entry_price = market_price_yes if direction == "buy_yes" else (1.0 - market_price_yes)
        category = payload.get("category") or payload.get("market_category")
        market_meta = payload.get("market")
        if not category and isinstance(market_meta, dict):
            category = market_meta.get("category")
        return self._calculate_edge_metrics(
            direction=direction,
            model_probability_yes=raw_model_probability,
            market_price_yes=market_price_yes,
            entry_price=entry_price,
            confidence=self._coerce_float(payload.get("confidence"), 0.0),
            age_minutes=self._extract_signal_age_minutes(payload),
            platform=str(payload.get("platform") or "polymarket"),
            category=str(category or "") or None,
            uncertainty_payload=payload,
        )

    def _passes_filters(self, payload: dict[str, Any]) -> bool:
        cfg = self._config
        edge_metrics = self._payload_edge_metrics(payload)
        edge_percent = float(edge_metrics["fee_adjusted_edge_percent"])
        confidence = self._coerce_float(payload.get("confidence"), 0.0)
        if edge_percent < float(cfg.get("min_edge_percent", 0.0) or 0.0):
            return False
        if bool(cfg.get("require_ci_clears_market", True)) and not bool(edge_metrics["ci_passed"]):
            return False
        if confidence < float(cfg.get("min_confidence", 0.0) or 0.0):
            return False

        supporting_articles = self._extract_supporting_articles(payload)
        article_count = len(supporting_articles)
        source_count = self._count_supporting_sources(supporting_articles)
        min_articles = max(1, int(cfg.get("min_supporting_articles", 1) or 1))
        min_sources = max(1, int(cfg.get("min_supporting_sources", 1) or 1))
        if article_count < min_articles:
            return False
        if source_count < min_sources:
            return False
        if bool(cfg.get("require_second_source", False)) and source_count < 2:
            return False
        max_signal_age_minutes = max(1.0, float(cfg.get("max_signal_age_minutes", 60.0) or 60.0))
        signal_age_minutes = self._extract_signal_age_minutes(payload)
        if signal_age_minutes is not None and signal_age_minutes > max_signal_age_minutes:
            return False
        if bool(cfg.get("require_verifier", False)):
            rejection_reasons = self._extract_rejection_reasons(payload)
            if "verifier_failed" in rejection_reasons:
                return False
            rerank = payload.get("evidence", {}).get("rerank") if isinstance(payload.get("evidence"), dict) else None
            if not isinstance(rerank, dict):
                finding_meta = payload.get("finding")
                if isinstance(finding_meta, dict):
                    finding_evidence = finding_meta.get("evidence")
                    if isinstance(finding_evidence, dict) and isinstance(finding_evidence.get("rerank"), dict):
                        rerank = finding_evidence.get("rerank")
            rationale = str(rerank.get("rationale") or "").strip().lower() if isinstance(rerank, dict) else ""
            if "[unverified_budget_skip]" in rationale:
                return False
        return True

    @staticmethod
    def _market_from_payload(payload: dict[str, Any]) -> Market:
        """Deserialize a news intent dict into a typed Market object.

        Intent dicts crossing the DataEvent boundary carry all the fields
        needed to construct a Market. We do this immediately at the boundary
        so every downstream code path (create_opportunity, fee model, risk
        scoring) sees a typed object — never a raw dict.
        """
        market_id = str(payload.get("market_id") or "")
        question = payload.get("market_question") or ""
        direction = str(payload.get("direction") or "buy_yes").strip().lower()
        entry_price = NewsEdgeStrategy._coerce_float(payload.get("entry_price"), -1.0)
        market_price = NewsEdgeStrategy._coerce_float(payload.get("market_price"), 0.5)
        if entry_price <= 0.0:
            entry_price = market_price if direction == "buy_yes" else (1.0 - market_price)
        entry_price = max(0.0, min(1.0, entry_price))
        # outcome_prices: [yes_price, no_price] — derive from direction + entry
        if direction == "buy_yes":
            yes_price = entry_price
            no_price = round(1.0 - entry_price, 6)
        else:
            no_price = entry_price
            yes_price = round(1.0 - entry_price, 6)
        market_meta = payload.get("market")
        if not isinstance(market_meta, dict):
            market_meta = {}
        raw_token_ids = market_meta.get("token_ids")
        if not isinstance(raw_token_ids, list):
            raw_token_ids = payload.get("token_ids")
        if not isinstance(raw_token_ids, list):
            raw_token_ids = []
        token_ids = [str(token_id).strip() for token_id in raw_token_ids if str(token_id or "").strip()]
        liquidity = NewsEdgeStrategy._coerce_float(
            payload.get("liquidity") or payload.get("market_liquidity") or market_meta.get("liquidity"),
            500.0,
        )
        platform = str(payload.get("platform") or market_meta.get("platform") or "polymarket").strip() or "polymarket"
        return Market(
            id=market_id,
            condition_id=str(market_meta.get("condition_id") or market_id),
            question=question,
            slug=payload.get("market_slug") or market_meta.get("slug") or market_id,
            event_slug=payload.get("event_slug") or market_meta.get("event_slug") or "",
            outcome_prices=[yes_price, no_price],
            clob_token_ids=token_ids,
            liquidity=liquidity,
            platform=platform,
        )

    def _payload_to_opportunity(self, payload: dict[str, Any]) -> Optional[Opportunity]:
        market_id = str(payload.get("market_id") or "").strip()
        if not market_id:
            return None

        direction = str(payload.get("direction") or "buy_yes").strip().lower()
        question = str(payload.get("market_question") or "").strip()
        confidence = self._coerce_float(payload.get("confidence"), 0.0)
        model_probability_yes = self._coerce_float(payload.get("model_probability"), 0.5)
        model_probability_yes = max(0.0, min(1.0, model_probability_yes))
        edge_metrics = self._payload_edge_metrics(payload)
        adjusted_probability_yes = float(edge_metrics["adjusted_probability_yes"])
        fee_adjusted_edge = float(edge_metrics["fee_adjusted_edge_percent"])
        ci_low = float(edge_metrics["ci_low"])
        ci_high = float(edge_metrics["ci_high"])
        ci_width = float(edge_metrics["ci_width"])
        confidence_uncertainty = float(edge_metrics["confidence_uncertainty"])
        entry_price = self._coerce_float(payload.get("entry_price"), -1.0)
        market_price_yes = self._coerce_float(payload.get("market_price"), 0.5)
        if entry_price <= 0.0:
            entry_price = market_price_yes if direction == "buy_yes" else (1.0 - market_price_yes)
        entry_price = max(0.0, min(1.0, entry_price))

        side = "YES" if direction == "buy_yes" else "NO"
        target_price = adjusted_probability_yes if direction == "buy_yes" else (1.0 - adjusted_probability_yes)
        target_price = max(0.0, min(1.0, target_price))
        if target_price <= 0.0 and entry_price > 0.0:
            target_price = max(0.0, min(1.0, entry_price + (fee_adjusted_edge / 100.0)))

        metadata = {
            k: v
            for k, v in payload.items()
            if k
            not in {
                "id",
                "market_id",
                "market_question",
                "market_price",
                "model_probability",
                "direction",
                "entry_price",
                "edge_percent",
                "confidence",
                "status",
                "created_at",
            }
        }
        metadata["model_probability_ci_low"] = ci_low
        metadata["model_probability_ci_high"] = ci_high
        metadata["model_probability_ci_width"] = ci_width
        metadata["confidence_uncertainty"] = confidence_uncertainty
        metadata["adjusted_model_probability"] = adjusted_probability_yes
        metadata["fee_adjusted_edge_percent"] = fee_adjusted_edge

        market = self._market_from_payload(payload)
        opp = self.create_opportunity(
            title=f"News Edge: {question[:50]}",
            description=f"News-driven {side} at ${entry_price:.2f} (net edge: {fee_adjusted_edge:.1f}%)",
            total_cost=entry_price,
            expected_payout=target_price,
            markets=[market],
            positions=[
                {
                    "action": "BUY",
                    "outcome": side,
                    "price": entry_price,
                    "news_metadata": metadata,
                }
            ],
            is_guaranteed=False,
            skip_fee_model=True,
            custom_roi_percent=fee_adjusted_edge,
            custom_risk_score=1.0 - confidence,
            confidence=confidence,
        )
        if opp is not None:
            opp.mispricing_type = MispricingType.NEWS_INFORMATION
            opp.strategy_context = {
                **dict(getattr(opp, "strategy_context", {}) or {}),
                "model_probability": model_probability_yes,
                "adjusted_model_probability": adjusted_probability_yes,
                "model_probability_ci_low": ci_low,
                "model_probability_ci_high": ci_high,
                "model_probability_ci_width": ci_width,
                "confidence_uncertainty": confidence_uncertainty,
                "market_price_yes": market_price_yes,
                "direction": direction,
            }
        return opp

    async def on_event(self, event: DataEvent) -> list[Opportunity]:
        if event.event_type != "news_update":
            return []
        findings = event.payload.get("findings") or []
        intents = event.payload.get("intents") or []
        raw_payloads = findings if findings else intents
        if not raw_payloads:
            return []

        opportunities: list[Opportunity] = []
        for payload in raw_payloads:
            if not isinstance(payload, dict):
                continue
            if not self._passes_filters(payload):
                continue
            opp = self._payload_to_opportunity(payload)
            if opp is not None:
                opportunities.append(opp)
        return opportunities

    async def detect_async(
        self,
        events: list[Event],
        markets: list[Market],
        prices: dict[str, dict],
    ) -> list[Opportunity]:
        """Async detection: fetch news, match, estimate, generate opportunities."""
        if not settings.NEWS_EDGE_ENABLED:
            return []

        try:
            # Step 1: Fetch new articles
            await news_feed_service.fetch_all()
            all_articles = news_feed_service.get_articles(max_age_hours=settings.NEWS_ARTICLE_TTL_HOURS)

            if not all_articles:
                logger.debug("News Edge: no articles available")
                return []

            # Step 2: Build market index from scanner's market data
            market_infos = self._build_market_infos(events, markets, prices)
            if not market_infos:
                return []

            loop = asyncio.get_running_loop()

            if not semantic_matcher._initialized:
                await loop.run_in_executor(_MATCHER_EXECUTOR, semantic_matcher.initialize)

            await loop.run_in_executor(_MATCHER_EXECUTOR, semantic_matcher.update_market_index, market_infos)

            # Step 3: Embed only article ids not processed by this strategy instance.
            articles_to_embed = self._select_articles_for_embedding(all_articles)
            if articles_to_embed:
                await loop.run_in_executor(
                    _MATCHER_EXECUTOR,
                    semantic_matcher.embed_articles,
                    articles_to_embed,
                )
                self._mark_articles_embedded(articles_to_embed)

            # Step 4: Match articles to markets
            matches = await loop.run_in_executor(
                _MATCHER_EXECUTOR,
                semantic_matcher.match_articles_to_markets,
                all_articles,
                3,
                settings.NEWS_SIMILARITY_THRESHOLD,
            )

            if not matches:
                logger.debug("News Edge: no matches found")
                return []

            logger.info(
                "News Edge: %d articles, %d markets, %d matches",
                len(all_articles),
                len(market_infos),
                len(matches),
            )

            # Step 5: Estimate edges via LLM
            edges = await edge_estimator.detect_edges(matches)

            # Step 6: Convert edges to Opportunity objects
            opportunities = []
            live_price_edge_drops = 0
            for edge in edges:
                market = next((candidate for candidate in markets if candidate.id == edge.market_id), None)
                if market is None:
                    continue
                live_metrics = self._refresh_edge_metrics(edge, market, prices)
                if live_metrics is None:
                    live_price_edge_drops += 1
                    continue
                if edge.confidence < float(self._config.get("min_confidence", 0.45)):
                    continue
                if bool(self._config.get("require_ci_clears_market", True)) and not bool(
                    live_metrics["ci_passed"]
                ):
                    continue
                if float(live_metrics["fee_adjusted_edge_percent"]) < float(
                    self._config.get("min_edge_percent", 5.0)
                ):
                    live_price_edge_drops += 1
                    continue
                opp = self._edge_to_opportunity(edge, market, events, live_metrics)
                if opp:
                    opportunities.append(opp)

            logger.info(
                "News Edge: %d opportunities generated; %d dropped after live-price edge refresh",
                len(opportunities),
                live_price_edge_drops,
            )
            return opportunities

        except Exception as e:
            logger.error("News Edge strategy error: %s", e, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_market_infos(
        self,
        events: list[Event],
        markets: list[Market],
        prices: dict[str, dict],
    ) -> list[MarketInfo]:
        """Convert scanner's market data into MarketInfo for the matcher."""
        # Build event lookup
        event_map: dict[str, Event] = {}
        for event in events:
            for m in event.markets:
                event_map[m.id] = event

        infos: list[MarketInfo] = []
        for market in markets:
            if market.closed or not market.active:
                continue
            if market.liquidity < settings.MIN_LIQUIDITY_HARD:
                continue

            # Get live prices
            yes_price = market.yes_price
            no_price = market.no_price
            if market.clob_token_ids:
                if len(market.clob_token_ids) > 0:
                    tid = market.clob_token_ids[0]
                    if tid in prices:
                        yes_price = prices[tid].get("mid", yes_price)
                if len(market.clob_token_ids) > 1:
                    tid = market.clob_token_ids[1]
                    if tid in prices:
                        no_price = prices[tid].get("mid", no_price)

            event = event_map.get(market.id)
            infos.append(
                MarketInfo(
                    market_id=market.id,
                    question=market.question,
                    event_title=event.title if event else "",
                    category=event.category if event else "",
                    yes_price=yes_price,
                    no_price=no_price,
                    liquidity=market.liquidity,
                    slug=market.slug,
                    end_date=market.end_date.isoformat() if market.end_date else None,
                )
            )

        return infos

    def _refresh_edge_metrics(
        self,
        edge: NewsEdge,
        market: Market,
        prices: dict[str, dict],
    ) -> dict[str, Any] | None:
        """Refresh the tradable side price and recompute all edge gates."""
        side = "YES" if edge.direction == "buy_yes" else "NO"
        entry_price = self._coerce_float(StrategySDK.get_live_price(market, prices, side=side), 0.0)
        if entry_price <= 0.0 or entry_price >= 1.0:
            return None
        market_price_yes = entry_price if edge.direction == "buy_yes" else (1.0 - entry_price)
        article_age_minutes = self._extract_signal_age_minutes(
            {"article_published_at": edge.match.article.published}
        )
        metrics = self._calculate_edge_metrics(
            direction=edge.direction,
            model_probability_yes=edge.model_probability,
            market_price_yes=market_price_yes,
            entry_price=entry_price,
            confidence=edge.confidence,
            age_minutes=article_age_minutes,
            platform=str(getattr(market, "platform", "polymarket") or "polymarket"),
            category=edge.match.market.category or None,
            uncertainty_payload={},
        )
        token_index = 0 if edge.direction == "buy_yes" else 1
        token_ids = market.clob_token_ids or []
        metrics.update(
            {
                "side": side,
                "entry_price": entry_price,
                "market_price_yes": market_price_yes,
                "target_price": (
                    float(metrics["adjusted_probability_yes"])
                    if edge.direction == "buy_yes"
                    else 1.0 - float(metrics["adjusted_probability_yes"])
                ),
                "token_id": token_ids[token_index] if len(token_ids) > token_index else None,
                "article_age_minutes": article_age_minutes,
            }
        )
        return metrics

    def _edge_to_opportunity(
        self,
        edge: NewsEdge,
        market: Market,
        events: list[Event],
        live_metrics: dict[str, Any],
    ) -> Optional[Opportunity]:
        """Convert a refreshed NewsEdge into an Opportunity."""
        mi = edge.match.market

        # Find the event
        event = None
        for e in events:
            if any(m.id == mi.market_id for m in e.markets):
                event = e
                break

        side = str(live_metrics["side"])
        entry_price = float(live_metrics["entry_price"])
        target_price = float(live_metrics["target_price"])
        token_id = live_metrics.get("token_id")
        ci_low = float(live_metrics["ci_low"])
        ci_high = float(live_metrics["ci_high"])
        ci_width = float(live_metrics["ci_width"])
        confidence_uncertainty = float(live_metrics["confidence_uncertainty"])
        adjusted_probability_yes = float(live_metrics["adjusted_probability_yes"])
        roi = float(live_metrics["fee_adjusted_edge_percent"])

        # Profit calculation: if we buy at entry_price and the true probability
        # is target_price, our expected value is target_price per share.
        # Expected profit = target_price - entry_price (per $1 of shares).
        expected_payout = target_price
        total_cost = entry_price

        sizing = StrategySDK.resolve_position_sizing(
            liquidity_usd=market.liquidity,
            liquidity_fraction=0.05,
            hard_cap_usd=500.0,
            signal=None,
            default_min_size=float(settings.MIN_POSITION_SIZE),
        )
        min_liquidity = float(sizing.get("liquidity_usd", 0.0) or 0.0)
        max_position = float(sizing.get("max_position_size", 0.0) or 0.0)
        if not bool(sizing.get("is_tradeable", False)):
            return None

        # Risk scoring for news-driven trades (higher risk than pure arb)
        risk_score = 0.4  # Base: news trades are inherently riskier
        risk_factors = [
            "News-driven directional bet (not structural arbitrage)",
            f"Model confidence: {edge.confidence:.0%}",
            f"Semantic similarity: {edge.match.similarity:.2f}",
        ]

        if edge.confidence < 0.7:
            risk_score += 0.2
            risk_factors.append("Low model confidence")
        if edge.match.similarity < 0.6:
            risk_score += 0.1
            risk_factors.append("Moderate semantic match quality")

        risk_score = min(risk_score, 1.0)

        positions = [
            {
                "action": "BUY",
                "outcome": side,
                "price": entry_price,
                "token_id": token_id,
                "_news_edge": {
                    "article_title": edge.match.article.title,
                    "article_url": edge.match.article.url,
                    "article_source": edge.match.article.source,
                    "model_probability": edge.model_probability,
                    "adjusted_model_probability": adjusted_probability_yes,
                    "model_probability_ci_low": ci_low,
                    "model_probability_ci_high": ci_high,
                    "model_probability_ci_width": ci_width,
                    "market_price": float(live_metrics["market_price_yes"]),
                    "edge_percent": roi,
                    "gross_edge_percent": float(live_metrics["gross_edge_percent"]),
                    "decayed_edge_percent": float(live_metrics["decayed_edge_percent"]),
                    "article_age_minutes": live_metrics.get("article_age_minutes"),
                    "direction": edge.direction,
                    "confidence": edge.confidence,
                    "confidence_uncertainty": confidence_uncertainty,
                    "reasoning": edge.reasoning,
                    "similarity": edge.match.similarity,
                    "match_method": edge.match.match_method,
                },
            }
        ]

        opp = self.create_opportunity(
            title=f"News Edge: {market.question[:50]}...",
            description=(
                f"News suggests {side} at ${entry_price:.2f} "
                f"(adjusted model: {adjusted_probability_yes:.0%}, "
                f"market: {float(live_metrics['market_price_yes']):.0%}, "
                f"net edge: {roi:.1f}%). "
                f"Source: {edge.match.article.title[:80]}"
            ),
            total_cost=total_cost,
            expected_payout=expected_payout,
            markets=[market],
            positions=positions,
            event=event,
            is_guaranteed=False,
            custom_roi_percent=roi,
            custom_risk_score=risk_score,
            confidence=edge.confidence,
        )
        if opp is not None:
            opp.risk_factors = risk_factors
            opp.min_liquidity = min_liquidity
            opp.max_position_size = max_position
            opp.mispricing_type = MispricingType.NEWS_INFORMATION
            opp.strategy_context = {
                **dict(getattr(opp, "strategy_context", {}) or {}),
                "model_probability": edge.model_probability,
                "adjusted_model_probability": adjusted_probability_yes,
                "model_probability_ci_low": ci_low,
                "model_probability_ci_high": ci_high,
                "model_probability_ci_width": ci_width,
                "confidence_uncertainty": confidence_uncertainty,
                "market_price_yes": float(live_metrics["market_price_yes"]),
                "direction": edge.direction,
            }

        return opp

    scoring_weights = ScoringWeights()
    sizing_config = SizingConfig()

    def custom_checks(self, signal: Any, context: dict, params: dict, payload: dict) -> list[DecisionCheck]:
        source = str(getattr(signal, "source", "") or "").strip().lower()
        source_ok = source in {"news"}
        return [
            DecisionCheck("source", "News-capable source", source_ok, detail="news"),
        ]

    def compute_score(
        self, edge: float, confidence: float, risk_score: float, market_count: int, payload: dict
    ) -> float:
        # These relative weights need calibration against shadow/backtest outcomes.
        return (edge * 1.0) + (confidence * 10.0)

    def compute_size(
        self, base_size: float, max_size: float, edge: float, confidence: float, risk_score: float, market_count: int
    ) -> float:
        return max(1.0, min(max_size, base_size * (1.0 + confidence)))

    def evaluate(self, signal: Any, context: dict) -> StrategyDecision:
        params = context.get("params") or {}
        payload = signal_payload(signal)
        strategy_context = payload.get("strategy_context") if isinstance(payload.get("strategy_context"), dict) else {}
        positions = payload.get("positions_to_take") if isinstance(payload.get("positions_to_take"), list) else []
        first_position = positions[0] if positions and isinstance(positions[0], dict) else {}
        news_meta = first_position.get("_news_edge") if isinstance(first_position.get("_news_edge"), dict) else {}

        configured_min_edge = to_float(
            self._config.get("min_edge_percent"),
            NEWS_EDGE_DEFAULT_CONFIG["min_edge_percent"],
        )
        configured_min_confidence = to_confidence(
            self._config.get("min_confidence"),
            NEWS_EDGE_DEFAULT_CONFIG["min_confidence"],
        )
        d = self.pipeline_defaults
        min_edge = to_float(
            params.get("min_edge_percent", configured_min_edge),
            configured_min_edge,
        )
        min_conf = to_confidence(
            params.get("min_confidence", configured_min_confidence),
            configured_min_confidence,
        )
        max_risk = to_confidence(
            params.get("max_risk_score", d.get("max_risk_score", 0.68)),
            d.get("max_risk_score", 0.68),
        )
        base_size, max_size = _trader_size_limits(context)

        edge = max(0.0, to_float(getattr(signal, "edge_percent", 0.0), 0.0))
        confidence = to_confidence(getattr(signal, "confidence", 0.0), 0.0)
        risk_score = to_confidence(payload.get("risk_score", 0.5), 0.5)
        market_count = len(payload.get("markets") or [])
        model_probability = self._coerce_float(
            strategy_context.get("model_probability", news_meta.get("model_probability")),
            self._coerce_float(payload.get("model_probability"), 0.5),
        )
        model_probability = max(0.0, min(1.0, model_probability))
        uncertainty_payload = {
            "model_probability_ci_low": strategy_context.get(
                "model_probability_ci_low",
                news_meta.get("model_probability_ci_low", payload.get("model_probability_ci_low")),
            ),
            "model_probability_ci_high": strategy_context.get(
                "model_probability_ci_high",
                news_meta.get("model_probability_ci_high", payload.get("model_probability_ci_high")),
            ),
        }
        ci_low, ci_high, ci_width, confidence_uncertainty = self._resolve_uncertainty(
            uncertainty_payload,
            model_probability,
            confidence,
        )

        checks = [
            DecisionCheck("edge", "Edge threshold", edge >= min_edge, score=edge, detail=f"min={min_edge:.2f}"),
            DecisionCheck(
                "confidence",
                "Confidence threshold",
                confidence >= min_conf,
                score=confidence,
                detail=f"min={min_conf:.2f}",
            ),
            DecisionCheck(
                "risk_score",
                "Risk score ceiling",
                risk_score <= max_risk,
                score=risk_score,
                detail=f"max={max_risk:.2f}",
            ),
        ]
        checks.extend(self.custom_checks(signal, context, params, payload))
        score = self.compute_score(edge, confidence, risk_score, market_count, payload)

        decision_payload = {
            "model_probability": model_probability,
            "model_probability_ci_low": ci_low,
            "model_probability_ci_high": ci_high,
            "model_probability_ci_width": ci_width,
            "confidence_uncertainty": confidence_uncertainty,
            "edge_percent": edge,
            "confidence": confidence,
            "risk_score": risk_score,
        }

        if not all(c.passed for c in checks):
            failed = [c for c in checks if not c.passed]
            return StrategyDecision(
                decision="skipped",
                reason=f"{self.name}: {', '.join(c.key for c in failed)}",
                score=score,
                checks=checks,
                payload=decision_payload,
            )

        size = self.compute_size(base_size, max_size, edge, confidence, risk_score, market_count)
        return StrategyDecision(
            decision="selected",
            reason=f"{self.name}: signal selected (edge={edge:.2f}%, conf={confidence:.2f})",
            score=score,
            size_usd=size,
            checks=checks,
            payload=decision_payload,
        )

    def should_exit(self, position: Any, market_state: dict) -> ExitDecision:
        """News edge decays quickly -- exit on time or standard TP/SL."""
        if market_state.get("is_resolved"):
            return self.default_exit_check(position, market_state)
        config = getattr(position, "config", None) or {}
        age_minutes = float(getattr(position, "age_minutes", 0) or 0)
        max_hold = float(config.get("max_hold_minutes", 60) or 60)
        if age_minutes > max_hold:
            current_price = market_state.get("current_price")
            return ExitDecision("close", f"News cycle decay ({age_minutes:.0f} min)", close_price=current_price)
        return self.default_exit_check(position, market_state)

    # ------------------------------------------------------------------
    # Platform gate hooks
    # ------------------------------------------------------------------

    def on_blocked(self, signal, reason: str, context: dict) -> None:
        logger.info("%s: signal blocked — %s (market=%s)", self.name, reason, getattr(signal, "market_id", "?"))

    def on_size_capped(self, original_size: float, capped_size: float, reason: str) -> None:
        logger.info("%s: size capped $%.0f → $%.0f — %s", self.name, original_size, capped_size, reason)
