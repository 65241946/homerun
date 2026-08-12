"""Kelly criterion and fee-aware edge calculation for prediction markets."""

from __future__ import annotations
import math


FEE_RATE_BY_CATEGORY: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other / general": 0.05,
    "other": 0.05,
    "general": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}
DEFAULT_FEE_RATE = 0.07


def kelly_fraction(p_estimated: float, p_market: float, fraction: float = 0.25) -> float:
    """Quarter-Kelly fraction for a binary prediction market bet.

    Args:
        p_estimated: Your estimated true probability of the event
        p_market: Market-implied probability (contract price)
        fraction: Kelly fraction (0.25 = quarter-Kelly, default)

    Returns:
        Fraction of bankroll to wager (0 if no edge)
    """
    if p_estimated <= p_market or p_market <= 0 or p_market >= 1:
        return 0.0
    f_star = (p_estimated - p_market) / (1.0 - p_market)
    return max(0.0, min(1.0, fraction * f_star))


def kelly_size(
    p_estimated: float,
    p_market: float,
    bankroll: float,
    fraction: float = 0.25,
    min_size: float = 1.0,
    max_size: float = 500.0,
) -> float:
    """Position size in USD using fractional Kelly criterion.

    Args:
        p_estimated: Your estimated true probability
        p_market: Market price (implied probability)
        bankroll: Total available capital
        fraction: Kelly fraction (default 0.25 = quarter-Kelly)
        min_size: Minimum position size
        max_size: Maximum position size

    Returns:
        Position size in USD
    """
    f = kelly_fraction(p_estimated, p_market, fraction)
    size = bankroll * f
    if size < min_size:
        return 0.0  # Below minimum, don't trade
    return min(size, max_size)


def polymarket_taker_fee(
    p: float,
    *,
    category: str | None = None,
    fee_rate: float | None = None,
) -> float:
    """Current Polymarket taker fee in USD for one share at price ``p``.

    An explicit ``fee_rate`` takes precedence over the category schedule.
    Unknown or missing categories use the highest current rate so fee-aware
    entry gates remain conservative.
    """
    p_clamped = max(0.0, min(1.0, float(p or 0.0)))
    if fee_rate is not None:
        resolved_fee_rate = max(0.0, float(fee_rate))
    else:
        category_key = str(category or "").strip().lower()
        resolved_fee_rate = FEE_RATE_BY_CATEGORY.get(category_key, DEFAULT_FEE_RATE)
    return resolved_fee_rate * p_clamped * (1.0 - p_clamped)


def polymarket_maker_fee(
    p: float,
    *,
    category: str | None = None,
    fee_rate: float | None = None,
) -> float:
    """Current Polymarket maker fee per share; makers pay zero."""
    del p, category, fee_rate
    return 0.0


def polymarket_taker_fee_legacy_quartic(p: float) -> float:
    """Pre-2026-01 fee curve for historical backtest comparison only.

    New trading and backtest code must not call this legacy helper.
    """
    p_clamped = max(0.0, min(1.0, float(p or 0.0)))
    return p_clamped * 0.25 * (p_clamped * (1.0 - p_clamped)) ** 2


def polymarket_taker_fee_pct(
    p: float,
    *,
    category: str | None = None,
    fee_rate: float | None = None,
) -> float:
    """Current Polymarket taker fee as a fraction of contract price."""
    p_clamped = max(0.0, min(1.0, float(p or 0.0)))
    if p_clamped <= 0.0:
        return 0.0
    return polymarket_taker_fee(
        p_clamped,
        category=category,
        fee_rate=fee_rate,
    ) / p_clamped


def kalshi_taker_fee(p: float, contracts: int = 1, fee_rate: float = 0.07) -> float:
    """Kalshi taker fee.

    Fee = ceil(fee_rate * contracts * price * (1-price))
    Range: ~0.6% at tails to ~1.75% at p=0.50.
    """
    return math.ceil(fee_rate * contracts * p * (1.0 - p) * 100) / 100


def fee_adjusted_edge(p_estimated: float, p_market: float, platform: str = "polymarket", side: str = "buy") -> float:
    """Calculate edge after platform fees.

    Args:
        p_estimated: Your estimated true probability
        p_market: Market price
        platform: "polymarket" or "kalshi"
        side: "buy" (taker) or "sell" (maker, 0 fee on polymarket)

    Returns:
        Net edge after fees (as fraction, not percent)
    """
    gross_edge = p_estimated - p_market

    if platform == "polymarket":
        if side == "buy":
            # This generic helper receives no market metadata; use the conservative default rate.
            fee = polymarket_taker_fee(p_market)
        else:
            fee = 0.0  # Makers pay zero
    elif platform == "kalshi":
        fee = kalshi_taker_fee(p_market)
    else:
        fee = 0.0

    return gross_edge - fee


def breakeven_edge(p_market: float, platform: str = "polymarket") -> float:
    """Minimum edge needed to break even after fees.

    Returns edge as fraction (multiply by 100 for percent).
    """
    if platform == "polymarket":
        # This generic helper receives no market metadata; use the conservative default rate.
        fee = polymarket_taker_fee(p_market)
    elif platform == "kalshi":
        fee = kalshi_taker_fee(p_market)
    else:
        fee = 0.0
    return fee
