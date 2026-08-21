"""Markout computation core.

Markout answers one question for a market maker: *after my quote was hit, did
the price move against me?* Persistently negative markout means the flow is
toxic and the quote must widen or pull.

Sign convention (always from the MAKER's perspective):

    positive markout  -> the maker profited after the fill
    negative markout  -> adverse selection; the taker was informed

Denominator convention (important, and different from equities):

A prediction-market contract settles at exactly 0 or 1, so one share is always
$1 of notional regardless of where it trades. Normalising a price move by the
mid -- the equity convention -- would make a 1c move on a 2c contract look like
5000bp and a 1c move on a 90c contract look like 11bp, even though both cost the
maker the same one cent per share. We therefore default to ``PER_NOTIONAL``:
markout is quoted in bps of $1 settlement value. ``PER_MID`` is available for
comparison against equity-style literature but should not drive quoting.

This module is pure: no DB, no network, no clock. Callers supply observations;
persistence and ingest live above.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

__all__ = [
    "Cohort",
    "Denominator",
    "FillObservation",
    "MakerSide",
    "MarkoutStats",
    "TtlBucket",
    "aggregate_markouts",
    "classify_cohort",
    "markout_bps",
    "percentile",
    "ttl_bucket_for",
]


# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------


class MakerSide(str, Enum):
    """Which side of the fill the maker was on."""

    BUY = "buy"    # maker bought; maker gains if price rises
    SELL = "sell"  # maker sold; maker gains if price falls


class Denominator(str, Enum):
    """How a price move is scaled into bps. See module docstring."""

    PER_NOTIONAL = "per_notional"  # bps of $1 settlement value (default, correct for binaries)
    PER_MID = "per_mid"            # bps of the mid at t0 (equity convention, comparison only)


class Cohort(str, Enum):
    """Counterparty classification. Signal, never a verdict -- see spec 5.2."""

    WHALE_24H = "whale_24h"                    # freshly funded wallet taking size
    ESTABLISHED_PLAYER = "established_player"  # long-lived wallet
    RETAIL = "retail"
    UNKNOWN = "unknown"                        # insufficient data to classify


class TtlBucket(str, Enum):
    """Remaining-time bucket. Adverse selection scales sharply with time to
    resolution, so markout is never pooled across these."""

    UNDER_1H = "bucket_3600"
    H1_TO_1D = "bucket_3600_86400"
    D1_TO_7D = "bucket_1d_7d"
    OVER_7D = "bucket_7d_plus"
    EXPIRED = "expired"


_DAY = 86_400
_WEEK = 7 * _DAY

# Cohort thresholds. Deliberately module-level so they can be tuned from config
# without editing call sites.
WHALE_MAX_WALLET_AGE_SECONDS = _DAY
WHALE_MIN_POSITION_USD = 50_000.0
ESTABLISHED_MIN_WALLET_AGE_SECONDS = 30 * _DAY


def ttl_bucket_for(remaining_seconds: float) -> TtlBucket:
    """Map seconds-to-resolution onto a :class:`TtlBucket`.

    Boundaries are half-open lower-inclusive: exactly 3600s lands in
    ``H1_TO_1D``, not ``UNDER_1H``.
    """
    if remaining_seconds <= 0:
        return TtlBucket.EXPIRED
    if remaining_seconds < 3_600:
        return TtlBucket.UNDER_1H
    if remaining_seconds < _DAY:
        return TtlBucket.H1_TO_1D
    if remaining_seconds < _WEEK:
        return TtlBucket.D1_TO_7D
    return TtlBucket.OVER_7D


def classify_cohort(
    wallet_age_seconds: float | None,
    position_notional_usd: float | None,
) -> Cohort:
    """Classify a counterparty from wallet age and size taken.

    Returns :attr:`Cohort.UNKNOWN` when either input is missing rather than
    guessing -- an unknown cohort is honest, a wrong one silently poisons every
    curve it lands in.
    """
    if wallet_age_seconds is None or position_notional_usd is None:
        return Cohort.UNKNOWN
    if wallet_age_seconds < 0 or position_notional_usd < 0:
        return Cohort.UNKNOWN

    if (
        wallet_age_seconds < WHALE_MAX_WALLET_AGE_SECONDS
        and position_notional_usd >= WHALE_MIN_POSITION_USD
    ):
        return Cohort.WHALE_24H
    if wallet_age_seconds >= ESTABLISHED_MIN_WALLET_AGE_SECONDS:
        return Cohort.ESTABLISHED_PLAYER
    return Cohort.RETAIL


# --------------------------------------------------------------------------
# Core computation
# --------------------------------------------------------------------------


def markout_bps(
    mid_at_fill: float,
    mid_after: float,
    maker_side: MakerSide,
    denominator: Denominator = Denominator.PER_NOTIONAL,
) -> float:
    """Signed markout in basis points, from the maker's perspective.

    Args:
        mid_at_fill: market mid at the moment of the fill, in probability space.
        mid_after: mid observed ``lag`` seconds later.
        maker_side: which side the maker was on.
        denominator: scaling convention; see module docstring.

    Raises:
        ValueError: if prices fall outside ``(0, 1)`` -- a binary contract mid
            of 0 or 1 is already resolved and cannot be marked out, and anything
            beyond that range indicates upstream corruption we must not average
            into a curve.
    """
    for name, value in (("mid_at_fill", mid_at_fill), ("mid_after", mid_after)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
        if not 0.0 < value < 1.0:
            raise ValueError(f"{name} must lie in (0, 1) for a binary contract, got {value!r}")

    drift = mid_after - mid_at_fill
    if maker_side is MakerSide.SELL:
        drift = -drift

    if denominator is Denominator.PER_MID:
        return drift / mid_at_fill * 10_000.0
    return drift * 10_000.0


@dataclass(frozen=True, slots=True)
class FillObservation:
    """One fill, already joined to the quote that preceded it.

    ``lag_seconds`` is the horizon at which ``mid_after`` was sampled; a single
    fill produces one observation per horizon on the markout curve.
    """

    market_id: str
    mid_at_fill: float
    mid_after: float
    maker_side: MakerSide
    lag_seconds: int
    cohort: Cohort = Cohort.UNKNOWN
    ttl_bucket: TtlBucket = TtlBucket.OVER_7D

    def markout(self, denominator: Denominator = Denominator.PER_NOTIONAL) -> float:
        return markout_bps(self.mid_at_fill, self.mid_after, self.maker_side, denominator)


@dataclass(frozen=True, slots=True)
class MarkoutStats:
    """Aggregated markout for one (cohort, ttl_bucket, lag) cell."""

    count: int
    mean_bps: float
    std_bps: float
    p05_bps: float
    p50_bps: float
    p95_bps: float

    @property
    def is_toxic(self) -> bool:
        """Whether this cell shows adverse selection worth acting on.

        Requires both a negative mean and enough samples to trust it. The
        20-sample floor is a guard against a handful of unlucky fills widening
        every quote in the book.
        """
        return self.count >= 20 and self.mean_bps < 0.0


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence.

    ``q`` is a fraction in ``[0, 1]``. Kept local so this package adds no
    third-party dependency.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sequence is undefined")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must lie in [0, 1], got {q!r}")
    if len(sorted_values) == 1:
        return float(sorted_values[0])

    pos = q * (len(sorted_values) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return float(sorted_values[int(pos)])
    weight = pos - low
    return float(sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight)


def aggregate_markouts(
    observations: Iterable[FillObservation],
    denominator: Denominator = Denominator.PER_NOTIONAL,
) -> MarkoutStats:
    """Collapse observations into a single cell's statistics.

    Callers are expected to have already grouped by (cohort, ttl_bucket, lag);
    this function does not group, it only summarises.

    Raises:
        ValueError: if ``observations`` is empty. An empty cell is a caller bug
            -- returning a zero-filled stat would read as "neutral flow" and is
            exactly the silent failure this instrumentation exists to prevent.
    """
    values = sorted(obs.markout(denominator) for obs in observations)
    if not values:
        raise ValueError("cannot aggregate an empty observation set")

    n = len(values)
    mean = math.fsum(values) / n
    if n > 1:
        variance = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
        std = math.sqrt(variance)
    else:
        std = 0.0

    return MarkoutStats(
        count=n,
        mean_bps=mean,
        std_bps=std,
        p05_bps=percentile(values, 0.05),
        p50_bps=percentile(values, 0.50),
        p95_bps=percentile(values, 0.95),
    )
