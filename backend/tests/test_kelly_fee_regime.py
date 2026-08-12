from __future__ import annotations

import math

import pytest

from services.strategies.crypto_strategy_utils import taker_fee_pct
from utils.kelly import (
    DEFAULT_FEE_RATE,
    polymarket_maker_fee,
    polymarket_taker_fee,
    polymarket_taker_fee_legacy_quartic,
    polymarket_taker_fee_pct,
)


@pytest.mark.parametrize(
    ("price", "expected_fee"),
    [
        (0.0, 0.0),
        (0.1, 0.0063),
        (0.5, 0.0175),
        (0.9, 0.0063),
        (1.0, 0.0),
    ],
)
def test_crypto_fee_curve_matches_current_schedule(price: float, expected_fee: float) -> None:
    assert polymarket_taker_fee(price, category="crypto") == pytest.approx(expected_fee)


def test_category_rates_and_unknown_category_are_conservative() -> None:
    price = 0.5
    crypto = polymarket_taker_fee(price, category="crypto")
    sports = polymarket_taker_fee(price, category="sports")
    politics = polymarket_taker_fee(price, category="politics")
    geopolitics = polymarket_taker_fee(price, category="geopolitics")
    unknown = polymarket_taker_fee(price, category="not-a-real-category")

    assert crypto > sports > politics > geopolitics == 0.0
    assert unknown == pytest.approx(DEFAULT_FEE_RATE * price * (1.0 - price))
    assert unknown != 0.0


def test_explicit_fee_rate_wins_and_maker_fee_is_zero() -> None:
    assert polymarket_taker_fee(0.5, category="geopolitics", fee_rate=0.09) == pytest.approx(0.0225)
    assert polymarket_maker_fee(0.5, category="crypto", fee_rate=0.09) == 0.0


def test_legacy_quartic_remains_available_for_historical_comparison() -> None:
    expected = 0.5 * 0.25 * (0.5 * (1.0 - 0.5)) ** 2
    assert polymarket_taker_fee_legacy_quartic(0.5) == pytest.approx(expected)
    assert not math.isclose(
        polymarket_taker_fee_legacy_quartic(0.5),
        polymarket_taker_fee(0.5, category="crypto"),
    )


@pytest.mark.parametrize("price", [0.1, 0.37, 0.5, 0.9])
def test_crypto_strategy_fee_helper_delegates_to_kelly(price: float) -> None:
    assert taker_fee_pct(price) == polymarket_taker_fee_pct(price, category="crypto")
