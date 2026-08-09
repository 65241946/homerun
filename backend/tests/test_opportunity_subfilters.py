import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import routes  # noqa: E402
from api.routes import _derive_opportunity_sub_strategy  # noqa: E402


def test_derive_news_edge_direction():
    opp = {
        "strategy": "news_edge",
        "title": "News Edge: Example",
        "positions_to_take": [
            {
                "action": "BUY",
                "outcome": "YES",
                "_news_edge": {"direction": "buy_yes"},
            }
        ],
    }

    assert _derive_opportunity_sub_strategy(opp) == "buy_yes"


def test_derive_cross_platform_leg_shape():
    opp = {
        "strategy": "cross_platform",
        "title": "Cross-Platform: Example",
        "positions_to_take": [
            {"platform": "polymarket", "outcome": "YES"},
            {"platform": "kalshi", "outcome": "NO"},
        ],
    }

    assert _derive_opportunity_sub_strategy(opp) == "poly_yes_kalshi_no"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"category": "WNBA", "strategy": "stat_arb", "markets": []}, True),
        ({"category": None, "strategy": "sports_overreaction_fader", "markets": []}, True),
        (
            {
                "category": None,
                "strategy": "stat_arb",
                "markets": [{"sports_market_type": "moneyline"}],
            },
            True,
        ),
        (
            {
                "category": None,
                "strategy": "stat_arb",
                "markets": [{"game_start_time": "2026-08-10T01:00:00Z"}],
            },
            False,
        ),
        ({"category": "Politics", "strategy": "stat_arb", "markets": [{}]}, False),
    ],
)
def test_payload_matches_sports_category(payload, expected):
    assert routes._payload_matches_category(payload, "sports") is expected


def test_payload_non_sports_category_remains_exact():
    assert routes._payload_matches_category({"category": "Economy"}, "economy") is True
    assert routes._payload_matches_category({"category": "Fed"}, "economy") is False


@pytest.mark.asyncio
async def test_filtered_payloads_use_structured_sports_classification(monkeypatch):
    payloads = [
        {"id": "wnba", "category": "WNBA", "strategy": "stat_arb", "markets": []},
        {
            "id": "moneyline",
            "category": None,
            "strategy": "stat_arb",
            "markets": [{"sports_market_type": "moneyline"}],
        },
        {
            "id": "weather-time",
            "category": "Weather",
            "strategy": "tail_end_carry",
            "markets": [{"game_start_time": "2026-08-10T01:00:00Z"}],
        },
        {"id": "politics", "category": "Politics", "strategy": "stat_arb", "markets": []},
    ]
    monkeypatch.setattr(routes, "_read_opportunity_payloads", AsyncMock(return_value=payloads))

    result = await routes._list_filtered_opportunity_payloads(
        SimpleNamespace(execute=object()),
        min_profit=0.0,
        max_risk=1.0,
        strategy=None,
        min_liquidity=0.0,
        search=None,
        category="sports",
        sort_by=None,
        sort_dir="desc",
        exclude_strategy=None,
        sub_strategy=None,
        source="markets",
        sort_results=False,
    )

    assert [payload["id"] for payload in result] == ["wnba", "moneyline"]
