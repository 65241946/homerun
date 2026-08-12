"""Unit tests for the markout core (Gap 3).

Pure logic only -- no DB, no network, no clock.
"""

from __future__ import annotations

import math

import pytest

from backend.services.markout.curves import (
    Cohort,
    Denominator,
    FillObservation,
    MakerSide,
    TtlBucket,
    aggregate_markouts,
    classify_cohort,
    markout_bps,
    percentile,
    ttl_bucket_for,
)


# --------------------------------------------------------------------------
# Sign convention -- the thing most worth getting right
# --------------------------------------------------------------------------


def test_maker_sell_into_a_rising_market_is_adverse():
    # Maker sold at 0.50, price ran to 0.55 -> maker is down 5c/share.
    assert markout_bps(0.50, 0.55, MakerSide.SELL) == pytest.approx(-500.0)


def test_maker_buy_into_a_rising_market_is_favourable():
    assert markout_bps(0.50, 0.55, MakerSide.BUY) == pytest.approx(500.0)


def test_sides_are_exact_mirrors():
    buy = markout_bps(0.42, 0.37, MakerSide.BUY)
    sell = markout_bps(0.42, 0.37, MakerSide.SELL)
    assert buy == pytest.approx(-sell)


def test_flat_market_marks_out_at_zero():
    assert markout_bps(0.30, 0.30, MakerSide.BUY) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Denominator convention -- the binary-specific correction
# --------------------------------------------------------------------------


def test_equal_cent_moves_cost_the_maker_equally_under_per_notional():
    """A 1c adverse move is 1c of loss whether the contract trades at 2c or 90c.

    This is the reason PER_NOTIONAL is the default; the equity convention
    disagrees, as the companion test below shows.
    """
    cheap = markout_bps(0.02, 0.03, MakerSide.SELL)
    rich = markout_bps(0.90, 0.91, MakerSide.SELL)
    assert cheap == pytest.approx(rich) == pytest.approx(-100.0)


def test_per_mid_distorts_cheap_contracts():
    cheap = markout_bps(0.02, 0.03, MakerSide.SELL, Denominator.PER_MID)
    rich = markout_bps(0.90, 0.91, MakerSide.SELL, Denominator.PER_MID)
    assert cheap == pytest.approx(-5000.0)
    assert rich == pytest.approx(-111.11, rel=1e-3)
    assert abs(cheap) > abs(rich) * 40  # same cash loss, wildly different number


# --------------------------------------------------------------------------
# Input validation -- corrupt prices must not enter a curve
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5, float("nan"), float("inf")])
def test_prices_outside_the_open_unit_interval_are_rejected(bad):
    with pytest.raises(ValueError):
        markout_bps(bad, 0.5, MakerSide.BUY)
    with pytest.raises(ValueError):
        markout_bps(0.5, bad, MakerSide.BUY)


# --------------------------------------------------------------------------
# TTL bucketing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (-1, TtlBucket.EXPIRED),
        (0, TtlBucket.EXPIRED),
        (1, TtlBucket.UNDER_1H),
        (3_599, TtlBucket.UNDER_1H),
        (3_600, TtlBucket.H1_TO_1D),      # boundary is lower-inclusive on the upper bucket
        (86_399, TtlBucket.H1_TO_1D),
        (86_400, TtlBucket.D1_TO_7D),
        (604_799, TtlBucket.D1_TO_7D),
        (604_800, TtlBucket.OVER_7D),
    ],
)
def test_ttl_bucket_boundaries(seconds, expected):
    assert ttl_bucket_for(seconds) is expected


# --------------------------------------------------------------------------
# Cohort classification
# --------------------------------------------------------------------------


def test_fresh_wallet_taking_size_is_a_whale():
    assert classify_cohort(3_600, 75_000.0) is Cohort.WHALE_24H


def test_fresh_wallet_taking_small_size_is_not_a_whale():
    assert classify_cohort(3_600, 500.0) is Cohort.RETAIL


def test_old_wallet_taking_size_is_established_not_whale():
    assert classify_cohort(90 * 86_400, 75_000.0) is Cohort.ESTABLISHED_PLAYER


@pytest.mark.parametrize(
    "age,size",
    [(None, 1000.0), (3600, None), (None, None), (-1, 1000.0), (3600, -1.0)],
)
def test_missing_or_nonsense_inputs_classify_as_unknown(age, size):
    """Unknown is honest; a wrong cohort silently poisons every curve."""
    assert classify_cohort(age, size) is Cohort.UNKNOWN


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


def test_percentile_endpoints_and_median():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == pytest.approx(0.0)
    assert percentile(values, 0.5) == pytest.approx(2.0)
    assert percentile(values, 1.0) == pytest.approx(4.0)


def test_percentile_interpolates_between_samples():
    assert percentile([0.0, 10.0], 0.25) == pytest.approx(2.5)


def test_percentile_of_single_sample_is_that_sample():
    assert percentile([7.0], 0.95) == pytest.approx(7.0)


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _obs(mid_after: float, side: MakerSide = MakerSide.SELL) -> FillObservation:
    return FillObservation(
        market_id="mkt",
        mid_at_fill=0.50,
        mid_after=mid_after,
        maker_side=side,
        lag_seconds=60,
        cohort=Cohort.WHALE_24H,
        ttl_bucket=TtlBucket.H1_TO_1D,
    )


def test_aggregate_reports_mean_and_dispersion():
    stats = aggregate_markouts([_obs(0.51), _obs(0.52), _obs(0.53)])
    assert stats.count == 3
    assert stats.mean_bps == pytest.approx(-200.0)  # -100, -200, -300
    assert stats.std_bps == pytest.approx(100.0)
    assert stats.p50_bps == pytest.approx(-200.0)


def test_aggregate_rejects_empty_rather_than_reporting_neutral_flow():
    """A zero-filled stat would read as 'neutral flow' -- the exact silent
    failure this instrumentation exists to prevent."""
    with pytest.raises(ValueError):
        aggregate_markouts([])


def test_single_observation_has_zero_std_not_nan():
    stats = aggregate_markouts([_obs(0.51)])
    assert stats.count == 1
    assert stats.std_bps == 0.0
    assert math.isfinite(stats.mean_bps)


def test_toxicity_requires_both_a_negative_mean_and_enough_samples():
    thin = aggregate_markouts([_obs(0.55) for _ in range(5)])
    assert thin.mean_bps < 0
    assert not thin.is_toxic  # negative, but too few fills to widen the book on

    thick = aggregate_markouts([_obs(0.55) for _ in range(25)])
    assert thick.is_toxic


def test_profitable_flow_is_never_flagged_toxic():
    stats = aggregate_markouts([_obs(0.45) for _ in range(50)])
    assert stats.mean_bps > 0
    assert not stats.is_toxic
