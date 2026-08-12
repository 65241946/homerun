import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.strategies.base import ScoringWeights
from services.strategies.news_momentum_breakout import NewsMomentumBreakoutStrategy


@pytest.mark.parametrize(
    ("baseline", "passing_price", "failing_price"),
    [
        (0.20, 0.25, 0.249),
        (0.70, 0.875, 0.874),
    ],
)
def test_relative_breakout_threshold_is_symmetric_across_price_levels(
    baseline,
    passing_price,
    failing_price,
):
    strategy = NewsMomentumBreakoutStrategy()

    assert strategy._breakout_threshold_passes(
        baseline,
        passing_price,
        relative_threshold=0.25,
        absolute_fallback=0.10,
    )
    assert not strategy._breakout_threshold_passes(
        baseline,
        failing_price,
        relative_threshold=0.25,
        absolute_fallback=0.10,
    )


def test_absolute_breakout_threshold_remains_fallback_when_relative_is_disabled():
    strategy = NewsMomentumBreakoutStrategy()
    assert strategy._breakout_threshold_passes(
        0.20,
        0.30,
        relative_threshold=0.0,
        absolute_fallback=0.10,
    )


def test_history_capacity_uses_five_second_refresh_estimate():
    assert NewsMomentumBreakoutStrategy._history_maxlen(1800.0) == 360
    assert NewsMomentumBreakoutStrategy._history_maxlen(601.0) == 121


def test_market_state_gc_removes_stale_history_and_emit_keys():
    strategy = NewsMomentumBreakoutStrategy()
    strategy.state["price_history"] = {
        "stale": deque([(1.0, 0.4, 0.6)], maxlen=360),
        "live": deque([(950.0, 0.5, 0.5)], maxlen=360),
    }
    # Stale pre-upgrade history has no last_seen entry; GC derives it from the row timestamp.
    strategy.state["last_seen"] = {"live": 950.0}
    strategy.state["last_emit"] = {
        ("stale", "YES"): 100.0,
        ("live", "NO"): 950.0,
    }

    strategy._gc_market_state(now=1000.0, stale_history_seconds=300.0)

    assert set(strategy.state["price_history"]) == {"live"}
    assert set(strategy.state["last_seen"]) == {"live"}
    assert set(strategy.state["last_emit"]) == {("live", "NO")}


def _position(*, current_high: float = 0.80):
    return SimpleNamespace(
        entry_price=0.40,
        age_minutes=50.0,
        highest_price=current_high,
        strategy_context={
            "_tracked_high": current_high,
            "_tracked_high_age": 0.0,
            "_scale_out_targets_hit": [0, 1],
        },
        config={
            "momentum_stall_minutes": 45.0,
            "stall_giveback_fraction": 0.5,
            "take_profit_pct": 500.0,
            "stop_loss_pct": 100.0,
            "trailing_stop_pct": 0.0,
            "max_hold_minutes": 1000.0,
            "min_hold_minutes": 0.0,
        },
    )


def test_stall_exit_triggers_after_peak_profit_giveback():
    strategy = NewsMomentumBreakoutStrategy()
    decision = strategy.should_exit(
        _position(),
        {"current_price": 0.59, "market_tradable": True},
    )

    assert decision.action == "close"
    assert "giveback 0.6000" in decision.reason


def test_stall_exit_holds_above_giveback_level():
    strategy = NewsMomentumBreakoutStrategy()
    decision = strategy.should_exit(
        _position(),
        {"current_price": 0.61, "market_tradable": True},
    )

    assert decision.action == "hold"


def test_scale_out_and_trailing_stop_units_are_percent_consistent():
    config = NewsMomentumBreakoutStrategy.scale_out_config
    assert [(target.trigger_bps, target.exit_fraction) for target in config.targets] == [
        (25.0, 0.33),
        (45.0, 0.33),
    ]
    assert config.trailing_stop_bps == 1200.0
    assert NewsMomentumBreakoutStrategy.default_config["trailing_stop_pct"] == 12.0


def test_compute_score_reads_declarative_scoring_weights():
    strategy = NewsMomentumBreakoutStrategy()
    strategy.scoring_weights = ScoringWeights(
        edge_weight=2.0,
        confidence_weight=3.0,
        risk_penalty=4.0,
        liquidity_weight=5.0,
        liquidity_divisor=2000.0,
        market_count_bonus=6.0,
    )

    score = strategy.compute_score(
        edge=10.0,
        confidence=0.5,
        risk_score=0.25,
        market_count=2,
        payload={"_signal_liquidity": 1000.0},
    )

    assert score == pytest.approx(35.0)


def test_new_defaults_are_present_and_target_is_reduced():
    config = NewsMomentumBreakoutStrategy.default_config
    assert config["breakout_threshold_rel"] == 0.25
    assert config["stall_giveback_fraction"] == 0.5
    assert config["target_distance_to_one_fraction"] == 0.35
