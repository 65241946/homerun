"""
Edge Estimator -- LLM probability estimation with full evidence chain.

Improved version of edge_detector.py for the independent news workflow.
Produces persisted NewsWorkflowFinding objects with complete audit trail.

Improvements over edge_detector.py:
- Full evidence chain (article snippet, event graph, retrieval scores, reasoning)
- Calibration prompt improvements (from KalshiBench findings)
- news_relevance filter before edge calc
- Confidence calibration with explicit uncertainty acknowledgment

Pattern from: KalshiBench (calibration), Quant-tool (evidence trail).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from config import settings
from services.news.event_extractor import ExtractedEvent
from services.news.reranker import RerankedCandidate
from services.news.semantic_matcher import NewsMarketMatch
from utils.utcnow import utcnow

logger = logging.getLogger(__name__)

_LLM_CALL_TIMEOUT_SECONDS = 20.0

# ---------------------------------------------------------------------------
# Confidence shaping constants
# ---------------------------------------------------------------------------
# Multipliers applied to raw LLM confidence based on information novelty.
# 0.8 penalty for known information; 0.5 was too aggressive and filtered most established news
KNOWN_NOVELTY_CONFIDENCE_FACTOR = 0.80
NOVELTY_CONFIDENCE_MULTIPLIERS = {
    "breaking": 1.0,  # Just happened -- full confidence
    "recent": 0.90,  # Hours old -- slight haircut (was 0.85)
    "known": KNOWN_NOVELTY_CONFIDENCE_FACTOR,  # Widely known -- moderate haircut (was 0.5)
    "stale": 0.60,  # Old news -- moderate haircut (was 0.15; raised from sub-0.5 to avoid over-filtering)
}
# Minimum confidence threshold after novelty adjustment; below this the
# finding is rejected as low_confidence.
MIN_POST_NOVELTY_CONFIDENCE = 0.20


@dataclass
class WorkflowFinding:
    """A finding from the news workflow pipeline with full evidence chain."""

    id: str = ""
    article_id: str = ""
    market_id: str = ""
    article_title: str = ""
    article_source: str = ""
    article_url: str = ""
    market_question: str = ""
    market_price: float = 0.5
    model_probability: float = 0.5
    edge_percent: float = 0.0
    direction: str = "buy_yes"
    confidence: float = 0.0
    retrieval_score: float = 0.0
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    event_score: float = 0.0
    rerank_score: float = 0.0
    event_graph: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    reasoning: str = ""
    actionable: bool = False
    signal_key: Optional[str] = None
    cache_key: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class NewsEdge:
    """A semantic-match edge estimated by the canonical news estimator."""

    match: NewsMarketMatch
    model_probability: float
    market_price: float
    edge_percent: float
    direction: str
    confidence: float
    reasoning: str
    estimated_at: datetime

    @property
    def market_id(self) -> str:
        return self.match.market.market_id

    @property
    def article_title(self) -> str:
        return self.match.article.title


# ---------------------------------------------------------------------------
# LLM probability estimation schema (calibrated)
# ---------------------------------------------------------------------------

EDGE_ESTIMATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "probability_yes": {
            "type": "number",
            "minimum": 0.01,
            "maximum": 0.99,
            "description": (
                "Your estimated probability that this market resolves YES, "
                "as a decimal between 0.01 and 0.99. "
                "IMPORTANT: prediction markets are often efficient. "
                "Only diverge significantly from the current price if the "
                "news provides STRONG, CLEAR evidence of a probability shift."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "How confident you are in this estimate (0=complete guess, 1=certain). "
                "Consider: Is the news directly about this market? "
                "Is the information new (not already priced in)? "
                "Could there be other interpretations?"
            ),
        },
        "reasoning": {
            "type": "string",
            "description": (
                "Explain in 2-4 sentences how the news affects probability. "
                "Include: what the news says, how it relates to the market, "
                "and why you believe the market hasn't priced this in yet."
            ),
        },
        "news_relevance": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
            "description": (
                "How relevant this news article actually is to the market. "
                "'high' = directly about the market topic. "
                "'medium' = related but indirect. "
                "'low' = tangentially related. "
                "'none' = not relevant at all."
            ),
        },
        "information_novelty": {
            "type": "string",
            "enum": ["breaking", "recent", "known", "stale"],
            "description": (
                "How new is this information? "
                "'breaking' = just happened, unlikely priced in. "
                "'recent' = hours old, partially priced in. "
                "'known' = widely known, likely already priced in. "
                "'stale' = old news, definitely priced in."
            ),
        },
    },
    "required": [
        "probability_yes",
        "confidence",
        "reasoning",
        "news_relevance",
        "information_novelty",
    ],
}


class EdgeEstimator:
    """Estimates probability edges for reranked article-market pairs."""

    _CONCURRENCY = 3

    def __init__(self, cache_ttl_seconds: Optional[float] = None) -> None:
        scan_interval = max(1.0, float(getattr(settings, "NEWS_SCAN_INTERVAL_SECONDS", 60) or 60))
        self._cache_ttl_seconds = max(
            scan_interval * 3.0,
            float(cache_ttl_seconds) if cache_ttl_seconds is not None else 0.0,
        )
        self._llm_cache: dict[tuple[str, str, float], tuple[float, dict[str, Any]]] = {}
        self._cached_edges: list[NewsEdge] = []

    @staticmethod
    def _cache_key(article_id: str, market_id: str, market_price: float) -> tuple[str, str, float]:
        return (str(article_id), str(market_id), round(float(market_price), 2))

    async def _cached_llm_result(
        self,
        *,
        article_id: str,
        market_id: str,
        market_price: float,
        article_title: str,
        article_summary: str,
        market_question: str,
        event_title: str,
        category: str,
        yes_price: float,
        no_price: float,
        model: Optional[str],
    ) -> Optional[dict[str, Any]]:
        key = self._cache_key(article_id, market_id, market_price)
        now = time.monotonic()
        cached = self._llm_cache.get(key)
        if cached is not None:
            expires_at, result = cached
            if expires_at > now:
                return dict(result)
            self._llm_cache.pop(key, None)

        result = await self._call_llm(
            article_title=article_title,
            article_summary=article_summary,
            market_question=market_question,
            event_title=event_title,
            category=category,
            yes_price=yes_price,
            no_price=no_price,
            model=model,
        )
        if result is not None:
            self._llm_cache[key] = (now + self._cache_ttl_seconds, dict(result))
        return result

    def get_cached_edges(self) -> list[NewsEdge]:
        """Return the latest semantic-match estimates without new LLM calls."""
        return list(self._cached_edges)

    def clear_cached_edges(self) -> int:
        """Clear displayed semantic-match estimates, preserving the LLM TTL cache."""
        count = len(self._cached_edges)
        self._cached_edges = []
        return count

    async def analyze_single_match(
        self,
        match: NewsMarketMatch,
        model: Optional[str] = None,
    ) -> Optional[NewsEdge]:
        edge = await self._estimate_semantic_match(match, model=model)
        if edge is None:
            return None
        key = (edge.match.article.article_id, edge.match.market.market_id)
        self._cached_edges = [
            existing
            for existing in self._cached_edges
            if (existing.match.article.article_id, existing.match.market.market_id) != key
        ]
        self._cached_edges.append(edge)
        self._cached_edges.sort(key=lambda item: item.edge_percent, reverse=True)
        return edge

    async def detect_edges(
        self,
        matches: list[NewsMarketMatch],
        model: Optional[str] = None,
    ) -> list[NewsEdge]:
        """Estimate semantic matches through the same calibrated LLM path."""
        if not matches:
            self._cached_edges = []
            return []

        deduplicated: dict[tuple[str, str], NewsMarketMatch] = {}
        for match in matches:
            key = (match.article.article_id, match.market.market_id)
            existing = deduplicated.get(key)
            if existing is None or match.similarity > existing.similarity:
                deduplicated[key] = match

        unique_matches = list(deduplicated.values())[: settings.NEWS_MAX_OPPORTUNITIES_PER_SCAN]
        semaphore = asyncio.Semaphore(self._CONCURRENCY)

        async def _one(match: NewsMarketMatch) -> Optional[NewsEdge]:
            async with semaphore:
                try:
                    return await self._estimate_semantic_match(match, model=model)
                except Exception as exc:
                    logger.debug("Edge estimation failed: %s", exc)
                    return None

        results = await asyncio.gather(*[_one(match) for match in unique_matches])
        edges = [edge for edge in results if edge is not None]
        edges.sort(key=lambda item: item.edge_percent, reverse=True)
        self._cached_edges = edges
        logger.info("Edge estimation: %d edges from %d matches", len(edges), len(unique_matches))
        return edges

    async def _estimate_semantic_match(
        self,
        match: NewsMarketMatch,
        model: Optional[str] = None,
    ) -> Optional[NewsEdge]:
        market = match.market
        article = match.article
        market_price = self._normalize_probability(market.yes_price, fallback=0.5)
        result = await self._cached_llm_result(
            article_id=article.article_id,
            market_id=market.market_id,
            market_price=market_price,
            article_title=article.title,
            article_summary=article.summary,
            market_question=market.question,
            event_title=market.event_title,
            category=market.category,
            yes_price=market_price,
            no_price=self._normalize_probability(market.no_price, fallback=1.0 - market_price),
            model=model,
        )
        if result is None:
            return None

        relevance = str(result.get("news_relevance") or "").strip().lower()
        novelty = str(result.get("information_novelty") or "known").strip().lower()
        if relevance in {"none", "low"} or novelty == "stale":
            return None

        probability_yes = self._normalize_probability(result.get("probability_yes"), fallback=market_price)
        confidence = self._normalize_confidence(result.get("confidence"), fallback=0.0)
        confidence *= NOVELTY_CONFIDENCE_MULTIPLIERS.get(novelty, 0.5)
        confidence = self._normalize_confidence(confidence, fallback=0.0)
        if confidence < MIN_POST_NOVELTY_CONFIDENCE:
            return None

        edge_percent = abs(probability_yes - market_price) * 100.0
        return NewsEdge(
            match=match,
            model_probability=probability_yes,
            market_price=market_price,
            edge_percent=edge_percent,
            direction="buy_yes" if probability_yes > market_price else "buy_no",
            confidence=confidence,
            reasoning=str(result.get("reasoning") or "").strip() or "Model returned no reasoning.",
            estimated_at=utcnow(),
        )

    @staticmethod
    def _normalize_probability(value: object, fallback: float = 0.5) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = fallback
        if parsed > 1.0:
            parsed = parsed / 100.0
        if parsed < 0.0:
            parsed = 0.0
        if parsed > 1.0:
            parsed = 1.0
        return parsed

    @staticmethod
    def _normalize_confidence(value: object, fallback: float = 0.0) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = fallback
        if parsed > 1.0:
            parsed = parsed / 100.0
        if parsed < 0.0:
            parsed = 0.0
        if parsed > 1.0:
            parsed = 1.0
        return parsed

    async def estimate_batch(
        self,
        article_title: str,
        article_summary: str,
        article_source: str,
        article_url: str,
        article_id: str,
        event: ExtractedEvent,
        reranked: list[RerankedCandidate],
        min_edge_percent: float = 8.0,
        min_confidence: float = 0.6,
        model: Optional[str] = None,
        allow_llm: bool = True,
        max_llm_calls: Optional[int] = None,
    ) -> list[WorkflowFinding]:
        """Estimate edges for a batch of reranked candidates.

        Returns both actionable and non-actionable findings so debug views can
        show why candidates were filtered.
        """
        if not reranked:
            return []

        sem = asyncio.Semaphore(self._CONCURRENCY)
        findings: list[WorkflowFinding] = []

        llm_budget = max(0, max_llm_calls if max_llm_calls is not None else len(reranked)) if allow_llm else 0

        async def _one(i: int, rc: RerankedCandidate) -> Optional[WorkflowFinding]:
            async with sem:
                return await self._estimate_one(
                    article_title=article_title,
                    article_summary=article_summary,
                    article_source=article_source,
                    article_url=article_url,
                    article_id=article_id,
                    event=event,
                    rc=rc,
                    model=model,
                    allow_llm=i < llm_budget,
                )

        results = await asyncio.gather(*[_one(i, rc) for i, rc in enumerate(reranked)])

        for finding in results:
            if finding is None:
                continue
            rejection_reasons = (
                finding.evidence.get("rejection_reasons") if isinstance(finding.evidence, dict) else None
            )
            has_rejection = isinstance(rejection_reasons, list) and len(rejection_reasons) > 0
            if (
                not has_rejection
                and finding.edge_percent > 0.0
                and finding.confidence > 0.0
                and finding.edge_percent >= min_edge_percent
                and finding.confidence >= min_confidence
            ):
                finding.actionable = True
            findings.append(finding)

        findings.sort(key=lambda f: f.edge_percent, reverse=True)
        return findings

    async def _estimate_one(
        self,
        article_title: str,
        article_summary: str,
        article_source: str,
        article_url: str,
        article_id: str,
        event: ExtractedEvent,
        rc: RerankedCandidate,
        model: Optional[str] = None,
        allow_llm: bool = True,
    ) -> Optional[WorkflowFinding]:
        """Estimate edge for a single article-market pair."""
        c = rc.candidate

        # Build evidence chain
        evidence = {
            "retrieval": {
                "keyword_score": round(c.keyword_score, 4),
                "semantic_score": round(c.semantic_score, 4),
                "event_score": round(c.event_score, 4),
                "combined_score": round(c.combined_score, 4),
            },
            "rerank": {
                "relevance": round(rc.relevance, 4),
                "rationale": rc.rationale,
                "rerank_score": round(rc.rerank_score, 4),
            },
            "event": {
                "type": event.event_type,
                "actors": event.actors,
                "action": event.action,
                "key_entities": event.key_entities,
            },
        }

        # Include entity overlap data from pre-LLM filtering for debugging.
        # This helps diagnose why certain article-market pairs were scored
        # higher or lower and whether entity-type mismatches contributed.
        entity_overlap = getattr(rc, "entity_overlap", None)
        if entity_overlap and isinstance(entity_overlap, dict):
            # Serialize sets to lists for JSON compatibility
            serializable_overlap = {}
            for k, v in entity_overlap.items():
                if isinstance(v, set):
                    serializable_overlap[k] = sorted(v)
                else:
                    serializable_overlap[k] = v
            evidence["entity_overlap"] = serializable_overlap

        event_graph = {
            "event_type": event.event_type,
            "actors": event.actors,
            "action": event.action,
            "date": event.date,
            "region": event.region,
            "impact_direction": event.impact_direction,
            "key_entities": event.key_entities,
        }

        def _rejected(reason: str, reasoning: Optional[str] = None) -> WorkflowFinding:
            merged_evidence = dict(evidence)
            existing_reasons = merged_evidence.get("rejection_reasons")
            reason_list = list(existing_reasons) if isinstance(existing_reasons, list) else []
            if reason not in reason_list:
                reason_list.append(reason)
            merged_evidence["rejection_reasons"] = reason_list
            return WorkflowFinding(
                id=uuid.uuid4().hex[:16],
                article_id=article_id,
                market_id=c.market_id,
                article_title=article_title,
                article_source=article_source,
                article_url=article_url,
                market_question=c.question,
                market_price=float(c.yes_price or 0.5),
                model_probability=float(c.yes_price or 0.5),
                edge_percent=0.0,
                direction="buy_yes",
                confidence=0.0,
                retrieval_score=float(c.combined_score or 0.0),
                semantic_score=float(c.semantic_score or 0.0),
                keyword_score=float(c.keyword_score or 0.0),
                event_score=float(c.event_score or 0.0),
                rerank_score=float(rc.rerank_score or 0.0),
                event_graph=event_graph,
                evidence=merged_evidence,
                reasoning=reasoning or f"Rejected before actionable edge: {reason}.",
                actionable=False,
            )

        if not allow_llm:
            return _rejected("llm_budget_exhausted")

        # Try LLM estimation
        llm_result = await self._cached_llm_result(
            article_id=article_id,
            market_id=c.market_id,
            market_price=float(c.yes_price or 0.5),
            article_title=article_title,
            article_summary=article_summary,
            market_question=c.question,
            event_title=c.event_title,
            category=c.category,
            yes_price=c.yes_price,
            no_price=c.no_price,
            model=model,
        )

        if llm_result is None:
            return _rejected("llm_unavailable_or_failed")

        news_relevance = str(llm_result.get("news_relevance") or "").strip().lower()
        novelty = str(llm_result.get("information_novelty") or "known").strip().lower()
        prob_yes = self._normalize_probability(llm_result.get("probability_yes"), fallback=float(c.yes_price or 0.5))
        confidence = self._normalize_confidence(llm_result.get("confidence"), fallback=0.0)
        reasoning = str(llm_result.get("reasoning") or "").strip()
        if not reasoning:
            reasoning = "Model returned no reasoning."

        evidence["llm"] = {
            "probability_yes": prob_yes,
            "confidence": confidence,
            "news_relevance": news_relevance,
            "information_novelty": novelty,
            "raw": llm_result,
        }

        # Filter irrelevant
        if news_relevance in {"none", "low"}:
            return _rejected(
                "low_news_relevance",
                reasoning=f"{reasoning} Filtered: news_relevance={news_relevance}.",
            )

        # Filter stale info (likely already priced in)
        if novelty == "stale":
            return _rejected(
                "stale_information",
                reasoning=f"{reasoning} Filtered: information_novelty=stale.",
            )

        # Compute edge
        market_price = float(c.yes_price or 0.5)
        edge = abs(prob_yes - market_price) * 100
        direction = "buy_yes" if prob_yes > market_price else "buy_no"

        # Novelty-adjusted confidence
        # Uses module-level NOVELTY_CONFIDENCE_MULTIPLIERS so the factors are
        # tunable without code changes.  The "known" factor was raised from
        # 0.5 -> 0.8 because markets still misprice information that is
        # "known" but whose implications haven't been fully absorbed (e.g.
        # consensus shifts, reinterpretations of existing data).  The "recent"
        # factor was raised from 0.85 -> 0.90 for similar reasons -- hours-old
        # news is often still under-priced.
        confidence *= NOVELTY_CONFIDENCE_MULTIPLIERS.get(novelty, 0.5)
        confidence = self._normalize_confidence(confidence, fallback=0.0)
        if confidence < MIN_POST_NOVELTY_CONFIDENCE:
            return _rejected(
                "low_confidence",
                reasoning=f"{reasoning} Filtered: confidence={confidence:.2f} after novelty adjustment.",
            )

        return WorkflowFinding(
            id=uuid.uuid4().hex[:16],
            article_id=article_id,
            market_id=c.market_id,
            article_title=article_title,
            article_source=article_source,
            article_url=article_url,
            market_question=c.question,
            market_price=market_price,
            model_probability=prob_yes,
            edge_percent=edge,
            direction=direction,
            confidence=confidence,
            retrieval_score=float(c.combined_score or 0.0),
            semantic_score=float(c.semantic_score or 0.0),
            keyword_score=float(c.keyword_score or 0.0),
            event_score=float(c.event_score or 0.0),
            rerank_score=float(rc.rerank_score or 0.0),
            event_graph=event_graph,
            evidence=evidence,
            reasoning=reasoning,
        )

    async def _call_llm(
        self,
        article_title: str,
        article_summary: str,
        market_question: str,
        event_title: str,
        category: str,
        yes_price: float,
        no_price: float,
        model: Optional[str] = None,
    ) -> Optional[dict]:
        """Call LLM for probability estimation."""
        try:
            from services.ai import get_llm_manager
            from services.ai.llm_provider import LLMMessage

            manager = get_llm_manager()
            if not manager.is_available():
                return None
        except Exception:
            return None

        system_prompt = (
            "You are a calibrated prediction market forecaster. "
            "Given a news article and a prediction market question, estimate "
            "the probability that the market resolves YES.\n\n"
            "CALIBRATION GUIDELINES (from KalshiBench research):\n"
            "- Prediction markets are often efficient. The current price "
            "reflects information from many participants.\n"
            "- LLMs tend to be OVERCONFIDENT. When uncertain, stay closer "
            "to the current market price.\n"
            "- Only deviate significantly (>10%) from the market price if "
            "the news provides STRONG, CLEAR, and NOVEL evidence.\n"
            "- Consider whether this information is already priced in.\n"
            "- A 90%+ confidence rating should be reserved for near-certain outcomes."
        )

        user_prompt = (
            f"MARKET QUESTION: {market_question}\n"
            f"EVENT: {event_title}\n"
            f"CATEGORY: {category}\n"
            f"CURRENT YES PRICE: ${yes_price:.2f}\n"
            f"CURRENT NO PRICE: ${no_price:.2f}\n\n"
            f"NEWS ARTICLE:\n"
            f"  Title: {article_title}\n"
        )
        if article_summary:
            user_prompt += f"  Summary: {article_summary[:500]}\n"

        user_prompt += (
            "\nBased on this news, what is the probability that the market "
            "resolves YES? Consider the current market price as a strong baseline."
        )

        try:
            return await asyncio.wait_for(
                manager.structured_output(
                    messages=[
                        LLMMessage(role="system", content=system_prompt),
                        LLMMessage(role="user", content=user_prompt),
                    ],
                    schema=EDGE_ESTIMATION_SCHEMA,
                    model=model,
                    purpose="news_workflow_edge_estimation",
                ),
                timeout=_LLM_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.debug(
                "Edge estimation LLM call timed out after %.1fs",
                _LLM_CALL_TIMEOUT_SECONDS,
            )
            return None
        except Exception as e:
            logger.debug("Edge estimation LLM call failed: %s", e)
            return None


# Singleton
edge_estimator = EdgeEstimator()
