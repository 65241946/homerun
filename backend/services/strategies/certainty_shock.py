# SEED TEMPLATE — not the live strategy. The DB `strategies.source_code` is the
# runtime master (loaded by the orchestrator + backtester); edits here only seed
# fresh installs / reset-to-factory. See services/opportunity_strategy_catalog.py.
"""
Strategy: Certainty Shock

Detects rapid one-sided repricing in a binary market within a short
lookback window when a deadline is near, and bets that the move continues
toward final certainty (target_exit_price) rather than retracing.

This is a directional, event-driven strategy — not arbitrage. It assumes
that recent large moves are information-driven (a participant with new
information bid the price up sharply) and that the price will continue
toward resolution rather than reverting.

Was previously a sub-branch of stat_arb. Split out so its performance is
tracked independently — historically it was a weak performer (0 wins on
small live trade count) and conflating its signals with the ensemble
stat-arb signals made attribution impossible.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from models import Event, Market, Opportunity
from .base import BaseStrategy, DecisionCheck, ExitDecision, ScoringWeights, SizingConfig, make_aware, utcnow
from services.quality_filter import QualityFilterOverrides

logger = logging.getLogger(__name__)


_DEADLINE_PATTERNS = [
    re.compile(r"\bby\s+(\w+\s+\d{1,2},?\s+\d{4}|\w+\s+\d{4})", re.IGNORECASE),
    re.compile(r"\bbefore\s+(\w+\s+\d{1,2},?\s+\d{4}|\w+\s+\d{4})", re.IGNORECASE),
    re.compile(r"\bin\s+(\w+\s+\d{4})", re.IGNORECASE),
    re.compile(r"\bby\s+(?:the\s+)?end\s+of\s+(\d{4})", re.IGNORECASE),
]

_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# All numeric tunables live in default_config below — module-level
# constants are reserved for algorithmic structure (regex, month maps).


class CertaintyShockStrategy(BaseStrategy):
    """Directional bet that a recent rapid repricing continues to resolution."""

    strategy_type = "certainty_shock"
    name = "Certainty Shock"
    description = "Bet that recent one-sided moves near deadline extend to settlement"
    mispricing_type = "within_market"
    subscriptions = ["market_data_refresh"]

    quality_filter_overrides = QualityFilterOverrides(
        min_roi=2.0,
        max_resolution_months=1.0,
    )

    default_config = {
        # Defaults are deliberately stricter than the original stat_arb
        # sub-branch: the old shock thresholds (0.18 absolute move, 0.12
        # retrace, 0.55 favored price) produced 0 wins in production. The
        # new defaults demand a bigger, cleaner shock (0.22 move, 0.08
        # retrace) on a clearer favorite (0.65+ entry).
        "min_edge_percent": 4.0,
        "min_confidence": 0.50,
        "max_risk_score": 0.60,
        # Shock detection knobs.
        "shock_lookback_seconds": 900,
        "shock_recent_window_seconds": 900,
        "shock_recent_share_min": 0.6,
        "shock_min_abs_move": 0.22,
        "shock_max_retrace": 0.08,
        "shock_min_favored_price": 0.65,
        "shock_target_certainty": 0.96,
        "shock_min_points": 5,            # min price-history snapshots needed
        "shock_max_favored_price": 0.97,  # ceiling on entry-side favored price
        "shock_extension_factor": 0.45,   # multiplier on lookback move for target exit
        "shock_min_expected_move": 0.04,  # never below min_edge_percent / 100
        # Bundle-level gates passed into create_opportunity.
        "min_liquidity_hard": 1500.0,
        "min_position_size": 50.0,
        # ROI ceiling — anything above this is almost certainly a bad model.
        "max_realistic_roi_pct": 30.0,
        # Time-to-deadline window. ``min`` is negative because we're willing
        # to enter slightly past the published deadline (some markets resolve
        # late but stay tradeable).
        "max_days_to_deadline": 7.0,
        "min_days_to_deadline": 0.5,
        # TP/SL ratio: tighter stop than profit so a 4% adverse move exits
        # before an 8% favorable move is required (R:R ~1:2).
        "take_profit_pct": 8.0,
        "stop_loss_pct": 4.0,
        "trailing_stop_pct": 6.0,
        "exclude_market_keywords": [
            "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
            "doge", "crypto",
        ],
    }

    pipeline_defaults = {
        "min_edge_percent": 3.0,
        "min_confidence": 0.50,
        "max_risk_score": 0.60,
    }

    scoring_weights = ScoringWeights(
        edge_weight=0.55,
        confidence_weight=30.0,
        risk_penalty=10.0,
    )
    sizing_config = SizingConfig(
        base_divisor=120.0,
        confidence_offset=0.65,
        risk_scale_factor=0.35,
        risk_floor=0.55,
        market_scale_factor=0.0,
        market_scale_cap=0,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def configure(self, config: dict) -> None:
        super().configure(config)
        try:
            min_edge_fraction = max(0.0, float(self.config.get("min_edge_percent", 4.0))) / 100.0
        except (TypeError, ValueError):
            min_edge_fraction = 0.04
        try:
            configured_expected_move = max(0.0, float(self.config.get("shock_min_expected_move", 0.04)))
        except (TypeError, ValueError):
            configured_expected_move = 0.04
        self.config["shock_min_expected_move"] = max(configured_expected_move, min_edge_fraction)

    @staticmethod
    def _normalize_excluded_keywords(value: Any) -> list[str]:
        if isinstance(value, str):
            candidates = [token.strip() for token in value.split(",")]
        elif isinstance(value, list):
            candidates = list(value)
        else:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            token = str(raw or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    @staticmethod
    def _market_text(market: Market) -> str:
        chunks: list[str] = []
        for value in (market.id, market.question, getattr(market, "slug", None),
                      getattr(market, "event_slug", None)):
            text = str(value or "").strip().lower()
            if text:
                chunks.append(text)
        return " | ".join(chunks)

    def _live_yes_price(self, market: Market, prices: dict[str, dict]) -> float:
        yes_price = market.yes_price
        if market.clob_token_ids and len(market.clob_token_ids) > 0:
            yes_token = market.clob_token_ids[0]
            if yes_token in prices:
                yes_price = prices[yes_token].get("mid", yes_price)
        return yes_price

    def _live_no_price(self, market: Market, prices: dict[str, dict]) -> float:
        no_price = market.no_price
        if market.clob_token_ids and len(market.clob_token_ids) > 1:
            no_token = market.clob_token_ids[1]
            if no_token in prices:
                no_price = prices[no_token].get("mid", no_price)
        return no_price

    def _extract_deadline(self, market: Market) -> Optional[datetime]:
        if market.end_date:
            return make_aware(market.end_date)
        for pattern in _DEADLINE_PATTERNS:
            match = pattern.search(market.question or "")
            if not match:
                continue
            parsed = self._parse_date_string(match.group(1).strip().rstrip(","))
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _deadline_signature(market: Market) -> tuple[str, str]:
        end_date = make_aware(market.end_date) if market.end_date else None
        return (end_date.isoformat() if end_date else "", str(market.question or ""))

    def _cached_deadline(self, market: Market) -> Optional[datetime]:
        cache: dict[str, tuple[tuple[str, str], Optional[datetime]]] = self.state.setdefault(
            "deadline_cache", {}
        )
        market_id = str(market.id)
        signature = self._deadline_signature(market)
        cached = cache.get(market_id)
        if cached is not None and cached[0] == signature:
            return cached[1]
        deadline = self._extract_deadline(market)
        cache[market_id] = (signature, deadline)
        return deadline

    @staticmethod
    def _recent_move_share(
        history: list[tuple[float, float, float]],
        *,
        side_index: int,
        scan_time: float,
        recent_window_seconds: int,
    ) -> float:
        if not history or side_index not in {1, 2}:
            return 0.0
        current_price = float(history[-1][side_index])
        total_move = max(0.0, current_price - min(float(point[side_index]) for point in history))
        if total_move <= 0.0:
            return 0.0
        recent_cutoff = scan_time - recent_window_seconds
        recent_prices = [float(point[side_index]) for point in history if point[0] >= recent_cutoff]
        if not recent_prices:
            return 0.0
        recent_move = max(0.0, current_price - min(recent_prices))
        return max(0.0, min(1.0, recent_move / total_move))

    def _gc_market_state(self, scan_time: float, retention_seconds: float) -> None:
        last_seen: dict[str, float] = self.state.setdefault("market_last_seen", {})
        cutoff = scan_time - retention_seconds
        stale_market_ids = {
            str(market_id)
            for market_id, seen_at in list(last_seen.items())
            if float(seen_at or 0.0) < cutoff
        }
        if not stale_market_ids:
            return
        price_history: dict[str, list[tuple[float, float, float]]] = self.state.setdefault("price_history", {})
        deadline_cache: dict[str, tuple[tuple[str, str], Optional[datetime]]] = self.state.setdefault(
            "deadline_cache", {}
        )
        for market_id in stale_market_ids:
            last_seen.pop(market_id, None)
            price_history.pop(market_id, None)
            deadline_cache.pop(market_id, None)

    def _parse_date_string(self, date_str: str) -> Optional[datetime]:
        parts = date_str.lower().split()
        if len(parts) == 1:
            try:
                year = int(parts[0])
            except ValueError:
                return None
            return datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        if len(parts) >= 2:
            month = _MONTH_MAP.get(parts[0])
            if month is None:
                return None
            if len(parts) == 2:
                try:
                    year = int(parts[1])
                except ValueError:
                    return None
                _, day = calendar.monthrange(year, month)
                return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
            try:
                day = int(parts[1].rstrip(","))
                year = int(parts[2])
            except (IndexError, ValueError):
                return None
            return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
        return None

    # ------------------------------------------------------------------
    # detect()
    # ------------------------------------------------------------------
    def detect(
        self,
        events: list[Event],
        markets: list[Market],
        prices: dict[str, dict],
    ) -> list[Opportunity]:
        config = dict(self.default_config)
        config.update(getattr(self, "config", {}) or {})

        excluded = self._normalize_excluded_keywords(config.get("exclude_market_keywords"))
        excluded_patterns = [re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE) for keyword in excluded]
        lookback = max(60, int(float(config.get("shock_lookback_seconds", 900) or 900)))
        recent_window = max(60, int(float(config.get("shock_recent_window_seconds", 900) or 900)))
        raw_recent_share_min = config.get("shock_recent_share_min")
        recent_share_min = max(0.0, min(1.0, float(0.6 if raw_recent_share_min is None else raw_recent_share_min)))
        min_abs_move = max(0.05, float(config.get("shock_min_abs_move", 0.22) or 0.22))
        max_retrace = max(0.0, float(config.get("shock_max_retrace", 0.08) or 0.08))
        min_favored = max(0.0, float(config.get("shock_min_favored_price", 0.65) or 0.65))
        target_cert = max(0.5, min(0.995, float(config.get("shock_target_certainty", 0.96) or 0.96)))
        max_days = max(0.0, float(config.get("max_days_to_deadline", 7.0) or 7.0))
        min_days = max(-1.0, float(config.get("min_days_to_deadline", 0.5) or 0.5))
        min_points = max(2, int(config.get("shock_min_points", 5) or 5))
        max_favored = float(config.get("shock_max_favored_price", 0.97) or 0.97)
        extension_factor = float(config.get("shock_extension_factor", 0.45) or 0.45)
        min_expected_move = float(config.get("shock_min_expected_move", 0.04) or 0.04)
        liquidity_hard = float(config.get("min_liquidity_hard", 1500.0) or 1500.0)
        position_size = float(config.get("min_position_size", 50.0) or 50.0)
        max_realistic_roi = float(config.get("max_realistic_roi_pct", 30.0) or 30.0)

        price_history = self.state.setdefault("price_history", {})
        market_last_seen = self.state.setdefault("market_last_seen", {})
        opportunities: list[Opportunity] = []
        now = utcnow()
        scan_time = now.timestamp()
        self._gc_market_state(scan_time, max(lookback, recent_window) * 2.0)

        for market in markets:
            if len(market.outcome_prices) != 2:
                continue
            if market.closed or not market.active:
                continue

            if excluded_patterns:
                text = self._market_text(market)
                if any(pattern.search(text) for pattern in excluded_patterns):
                    continue

            market_last_seen[str(market.id)] = scan_time
            deadline = self._cached_deadline(market)
            if deadline is None:
                continue
            days_remaining = (deadline - now).total_seconds() / 86400.0
            if days_remaining > max_days or days_remaining < min_days:
                continue

            yes_price = self._live_yes_price(market, prices)
            no_price = self._live_no_price(market, prices)

            history = price_history.setdefault(market.id, [])
            history.append((scan_time, yes_price, no_price))
            if len(history) > 100:
                price_history[market.id] = history[-100:]
                history = price_history[market.id]

            if len(history) < min_points:
                continue

            cutoff = scan_time - lookback
            window = [point for point in history if point[0] >= cutoff]
            if len(window) < min_points:
                continue

            yes_window = [float(point[1]) for point in window]
            no_window = [float(point[2]) for point in window]
            yes_move = max(0.0, yes_price - min(yes_window))
            no_move = max(0.0, no_price - min(no_window))
            if yes_move < min_abs_move and no_move < min_abs_move:
                continue

            if yes_move >= no_move:
                outcome = "YES"
                entry_price = yes_price
                token_id = market.clob_token_ids[0] if market.clob_token_ids else None
                move = yes_move
                side_index = 1
                side_window = yes_window
                shock_desc = "YES repricing upward"
            else:
                outcome = "NO"
                entry_price = no_price
                token_id = market.clob_token_ids[1] if len(market.clob_token_ids) > 1 else None
                move = no_move
                side_index = 2
                side_window = no_window
                shock_desc = "NO repricing upward"

            peak = max(side_window)
            trough = min(side_window)
            retrace = max(peak - entry_price, 0.0)

            if retrace > max_retrace:
                continue
            recent_share = self._recent_move_share(
                history,
                side_index=side_index,
                scan_time=scan_time,
                recent_window_seconds=recent_window,
            )
            if recent_share < recent_share_min:
                continue
            if entry_price < min_favored or entry_price > max_favored:
                continue

            target_exit_price = max(target_cert, entry_price + move * extension_factor)
            target_exit_price = max(0.01, min(0.995, target_exit_price))
            expected_move = target_exit_price - entry_price
            if expected_move < min_expected_move:
                continue

            positions = [{
                "action": "BUY",
                "outcome": outcome,
                "market": market.question[:50],
                "price": entry_price,
                "token_id": token_id,
                "rationale": (
                    f"{shock_desc}; lookback move {move:.3f}, retrace {retrace:.3f}, "
                    f"recent share {recent_share:.0%}, target ${target_exit_price:.3f}"
                ),
            }]

            # Realized capital efficiency if our directional view holds:
            # (target_price - entry_price) / entry_price.
            realistic_roi = (expected_move / max(entry_price, 0.001)) * 100.0

            opp = self.create_opportunity(
                title=f"Certainty Shock: {market.question[:50]}...",
                description=(
                    f"Rapid repricing detected near deadline ({days_remaining:.2f}d). "
                    f"{shock_desc}: peak=${peak:.3f}, trough=${trough:.3f}, "
                    f"current {outcome}=${entry_price:.3f}. Buy {outcome} @ ${entry_price:.3f}, "
                    f"target repricing ${target_exit_price:.3f}."
                ),
                total_cost=entry_price,
                expected_payout=target_exit_price,
                markets=[market],
                positions=positions,
                is_guaranteed=False,
                min_liquidity_hard=liquidity_hard,
                min_position_size=position_size,
                custom_roi_percent=realistic_roi,
            )
            if opp is None:
                continue
            # Hard ROI ceiling — reject bets whose claimed payout move is so
            # large that it implies the market is mispriced by more than
            # any honest calibration would support.
            if opp.roi_percent > max_realistic_roi:
                continue

            risk_score = 0.62 - min(move * 0.35, 0.20)
            if days_remaining <= 1.0:
                risk_score -= 0.05
            opp.risk_score = max(0.40, min(risk_score, 0.75))
            # Conviction signal = absolute expected move in cents/share
            # (target − entry, scaled ×100). Naturally bounded by [0, 100].
            conviction_edge = round(expected_move * 100.0, 3)
            try:
                opp.edge_percent = conviction_edge
            except (AttributeError, ValueError):
                pass
            opp.strategy_context["edge_percent"] = conviction_edge
            opp.strategy_context["shock_recent_share"] = recent_share
            opp.risk_factors.insert(0, "DIRECTIONAL BET — certainty shock can reverse before final settlement.")
            opp.risk_factors.append(f"Certainty shock: {shock_desc}, move={move:.1%}, retrace={retrace:.1%}")
            opp.risk_factors.append(f"Recent repricing share: {recent_share:.0%} over {recent_window}s")
            opp.risk_factors.append(f"Near expiry window: {days_remaining:.2f} days to deadline")
            opp.risk_factors.append(f"Target repricing edge: +${expected_move:.3f} per share")
            opportunities.append(opp)

        return opportunities

    def custom_checks(self, signal, context, params, payload):
        source = str(getattr(signal, "source", "") or "").strip().lower()
        return [
            DecisionCheck("source", "Signal source", source == "scanner",
                          detail=f"got={source}"),
        ]

    def should_exit(self, position: Any, market_state: dict) -> ExitDecision:
        """Standard TP/SL exit. Stop-loss must be tighter than take-profit
        because directional bets near deadline can reverse fast."""
        if market_state.get("is_resolved"):
            return self.default_exit_check(position, market_state)
        config = getattr(position, "config", None) or {}
        config = dict(config)
        for key, fallback in (
            ("take_profit_pct", 8.0),
            ("stop_loss_pct", 4.0),
            ("trailing_stop_pct", 6.0),
        ):
            try:
                default_value = float((getattr(self, "config", None) or {}).get(key, fallback))
            except (TypeError, ValueError):
                default_value = fallback
            config.setdefault(key, default_value)
        position.config = config
        return self.default_exit_check(position, market_state)

    def on_blocked(self, signal, reason: str, context: dict) -> None:
        logger.info("%s: signal blocked — %s (market=%s)", self.name, reason,
                    getattr(signal, "market_id", "?"))

    def on_size_capped(self, original_size: float, capped_size: float, reason: str) -> None:
        logger.info("%s: size capped $%.0f → $%.0f — %s", self.name,
                    original_size, capped_size, reason)
