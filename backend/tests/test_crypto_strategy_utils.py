from __future__ import annotations

import pytest

from services.strategy_helpers.crypto_strategy_utils import (
    default_max_market_data_age_ms,
    default_max_oracle_age_ms,
    default_min_seconds_left_for_entry,
    estimate_p_win,
    taker_fee_pct,
)
from utils.kelly import polymarket_taker_fee_pct


def test_estimate_p_win_is_monotonic_and_more_extreme_late():
    kwargs = {
        "base_scale": 1.0,
        "min_scale": 0.2,
        "prob_min": 0.30,
        "prob_max": 0.97,
    }

    negative = estimate_p_win(-0.4, 0.2, **kwargs)
    neutral = estimate_p_win(0.0, 0.2, **kwargs)
    positive_early = estimate_p_win(0.4, 0.2, **kwargs)
    positive_late = estimate_p_win(0.4, 0.8, **kwargs)

    assert negative < neutral < positive_early
    assert neutral == pytest.approx(0.5)
    assert positive_late > positive_early


def test_estimate_p_win_clamps_probability_bounds():
    kwargs = {
        "base_scale": 1.0,
        "min_scale": 0.2,
        "prob_min": 0.30,
        "prob_max": 0.90,
    }

    assert estimate_p_win(-100.0, 1.0, **kwargs) == pytest.approx(0.30)
    assert estimate_p_win(100.0, 1.0, **kwargs) == pytest.approx(0.90)


@pytest.mark.parametrize(
    ("timeframe", "min_seconds", "max_market_age_ms", "max_oracle_age_ms"),
    [
        ("5m", 35.0, 2500.0, 5000.0),
        ("15m", 60.0, 4000.0, 7500.0),
        ("1h", 180.0, 8000.0, 15000.0),
        ("4h", 600.0, 15000.0, 30000.0),
    ],
)
def test_timeframe_defaults_are_canonical(
    timeframe: str,
    min_seconds: float,
    max_market_age_ms: float,
    max_oracle_age_ms: float,
):
    assert default_min_seconds_left_for_entry(timeframe) == min_seconds
    assert default_max_market_data_age_ms(timeframe) == max_market_age_ms
    assert default_max_oracle_age_ms(timeframe) == max_oracle_age_ms


def test_crypto_taker_fee_still_delegates_to_canonical_curve():
    for price in (0.05, 0.25, 0.50, 0.85, 0.95):
        assert taker_fee_pct(price) == pytest.approx(
            polymarket_taker_fee_pct(price, category="crypto")
        )
