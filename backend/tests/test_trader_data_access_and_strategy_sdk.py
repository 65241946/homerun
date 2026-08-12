import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.trader_data_access as trader_data_access
import services.traders_firehose_pipeline as traders_firehose_pipeline
from services.strategy_sdk import StrategySDK
from services.strategies.traders_confluence import TradersConfluenceStrategy
from services.traders_sdk import TradersSDK


@pytest.mark.asyncio
async def test_trader_data_access_strategy_filtered_signals_delegates_pipeline(monkeypatch):
    rows = [{"id": "sig-1"}]
    loader = AsyncMock(return_value=rows)
    monkeypatch.setattr(
        traders_firehose_pipeline,
        "get_strategy_filtered_trader_opportunities",
        loader,
    )

    result = await trader_data_access.get_strategy_filtered_trader_signals(
        limit=23,
        include_filtered=True,
    )

    loader.assert_awaited_once_with(limit=23, include_filtered=True)
    assert result == rows


@pytest.mark.asyncio
async def test_strategy_pipeline_loads_source_context_before_filtering(monkeypatch):
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(
        traders_firehose_pipeline.StrategySDK,
        "get_trader_firehose_signals",
        loader,
    )

    result = await traders_firehose_pipeline.get_strategy_filtered_trader_opportunities(
        limit=10,
        include_filtered=False,
    )

    loader.assert_awaited_once_with(
        limit=250,
        include_filtered=False,
        include_source_context=True,
    )
    assert result == []


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ({"side": "BUY"}, "buy"),
        ({"direction": "SELL"}, "sell"),
        ({"outcome": "YES"}, "buy"),
        ({"outcome": "NO"}, "sell"),
        ({"signal_type": "multi_wallet_buy"}, "buy"),
        ({"signal_type": "multi_wallet_sell"}, "sell"),
    ],
)
def test_infer_trader_side_accepts_canonical_confluence_fields(signal, expected):
    assert StrategySDK.infer_trader_side(signal) == expected


def test_traders_confluence_opportunity_preserves_cached_execution_tokens():
    strategy = TradersConfluenceStrategy()
    rows = [
        {
            "id": "signal-with-tokens",
            "market_id": "0xmarket",
            "market_question": "Will both teams score?",
            "market_slug": "both-teams-score",
            "signal_type": "multi_wallet_buy",
            "outcome": "YES",
            "side": "buy",
            "tier": "high",
            "strength": 0.7,
            "conviction_score": 70.0,
            "wallet_count": 3,
            "cluster_adjusted_wallet_count": 3,
            "avg_entry_price": 0.42,
            "is_active": True,
            "is_tradeable": True,
            "wallets": ["0xa", "0xb", "0xc"],
            "source_flags": {"from_pool": True, "qualified": True},
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "outcome_labels": ["Yes", "No"],
        }
    ]

    opportunities = strategy.build_opportunities_from_firehose(rows)

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.markets[0]["clob_token_ids"] == ["yes-token", "no-token"]
    assert opportunity.positions_to_take[0]["token_id"] == "yes-token"
    assert opportunity.execution_plan is not None
    assert opportunity.execution_plan.legs[0].token_id == "yes-token"


@pytest.mark.asyncio
async def test_traders_confluence_detect_async_loads_source_context(monkeypatch):
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(StrategySDK, "get_trader_firehose_signals", loader)

    result = await TradersConfluenceStrategy().detect_async([], [], {})

    loader.assert_awaited_once_with(
        limit=250,
        include_filtered=True,
        include_source_context=True,
    )
    assert result == []


@pytest.mark.asyncio
async def test_strategy_sdk_trader_signal_wrappers_passthrough(monkeypatch):
    firehose_rows = [{"id": "firehose-row"}]
    strategy_rows = [{"id": "strategy-row"}]
    confluence_rows = [{"id": "confluence-row"}]

    firehose_loader = AsyncMock(return_value=firehose_rows)
    strategy_loader = AsyncMock(return_value=strategy_rows)
    confluence_loader = AsyncMock(return_value=confluence_rows)

    monkeypatch.setattr(trader_data_access, "get_trader_firehose_signals", firehose_loader)
    monkeypatch.setattr(trader_data_access, "get_strategy_filtered_trader_signals", strategy_loader)
    monkeypatch.setattr(trader_data_access, "get_trader_confluence_signals", confluence_loader)

    firehose = await StrategySDK.get_trader_firehose_signals(
        limit=111,
        include_filtered=True,
        include_source_context=False,
    )
    strategy = await StrategySDK.get_trader_strategy_signals(
        limit=77,
        include_filtered=False,
    )
    confluence = await StrategySDK.get_trader_confluence_signals(
        min_strength=0.42,
        min_tier="HIGH",
        limit=12,
    )

    firehose_loader.assert_awaited_once_with(
        limit=111,
        include_filtered=True,
        include_source_context=False,
    )
    strategy_loader.assert_awaited_once_with(limit=77, include_filtered=False)
    confluence_loader.assert_awaited_once_with(min_strength=0.42, min_tier="HIGH", limit=12)
    assert firehose == firehose_rows
    assert strategy == strategy_rows
    assert confluence == confluence_rows


@pytest.mark.asyncio
async def test_traders_sdk_signal_wrappers_passthrough(monkeypatch):
    firehose_rows = [{"id": "firehose-row"}]
    strategy_rows = [{"id": "strategy-row"}]
    confluence_rows = [{"id": "confluence-row"}]

    firehose_loader = AsyncMock(return_value=firehose_rows)
    strategy_loader = AsyncMock(return_value=strategy_rows)
    confluence_loader = AsyncMock(return_value=confluence_rows)

    monkeypatch.setattr(trader_data_access, "get_trader_firehose_signals", firehose_loader)
    monkeypatch.setattr(trader_data_access, "get_strategy_filtered_trader_signals", strategy_loader)
    monkeypatch.setattr(trader_data_access, "get_trader_confluence_signals", confluence_loader)

    firehose = await TradersSDK.get_firehose_signals(
        limit=111,
        include_filtered=True,
        include_source_context=False,
    )
    strategy = await TradersSDK.get_strategy_filtered_signals(
        limit=77,
        include_filtered=False,
    )
    confluence = await TradersSDK.get_confluence_signals(
        min_strength=0.42,
        min_tier="HIGH",
        limit=12,
    )

    firehose_loader.assert_awaited_once_with(
        limit=111,
        include_filtered=True,
        include_source_context=False,
    )
    strategy_loader.assert_awaited_once_with(limit=77, include_filtered=False)
    confluence_loader.assert_awaited_once_with(min_strength=0.42, min_tier="HIGH", limit=12)
    assert firehose == firehose_rows
    assert strategy == strategy_rows
    assert confluence == confluence_rows


@pytest.mark.asyncio
async def test_strategy_sdk_trader_directory_wrappers_passthrough(monkeypatch):
    pooled_rows = [{"address": "0xpool"}]
    tracked_rows = [{"address": "0xtracked"}]
    group_rows = [{"id": "grp-1"}]
    tag_rows = [{"name": "whale", "wallet_count": 2}]
    wallets_by_tag = [{"address": "0xtag"}]

    pooled_loader = AsyncMock(return_value=pooled_rows)
    tracked_loader = AsyncMock(return_value=tracked_rows)
    groups_loader = AsyncMock(return_value=group_rows)
    tags_loader = AsyncMock(return_value=tag_rows)
    by_tag_loader = AsyncMock(return_value=wallets_by_tag)

    monkeypatch.setattr(trader_data_access, "get_pooled_traders", pooled_loader)
    monkeypatch.setattr(trader_data_access, "get_tracked_traders", tracked_loader)
    monkeypatch.setattr(trader_data_access, "get_trader_groups", groups_loader)
    monkeypatch.setattr(trader_data_access, "get_trader_tags", tags_loader)
    monkeypatch.setattr(trader_data_access, "get_traders_by_tag", by_tag_loader)

    pooled = await StrategySDK.get_pooled_traders(
        limit=19,
        tier="high",
        include_blacklisted=False,
        tracked_only=True,
    )
    tracked = await StrategySDK.get_tracked_traders(
        limit=29,
        include_recent_activity=True,
        activity_hours=12,
    )
    groups = await StrategySDK.get_trader_groups(
        include_members=True,
        member_limit=7,
    )
    tags = await StrategySDK.get_trader_tags()
    by_tag = await StrategySDK.get_traders_by_tag("whale", limit=33)

    pooled_loader.assert_awaited_once_with(
        limit=19,
        tier="high",
        include_blacklisted=False,
        tracked_only=True,
    )
    tracked_loader.assert_awaited_once_with(
        limit=29,
        include_recent_activity=True,
        activity_hours=12,
    )
    groups_loader.assert_awaited_once_with(include_members=True, member_limit=7)
    tags_loader.assert_awaited_once_with()
    by_tag_loader.assert_awaited_once_with(tag_name="whale", limit=33)

    assert pooled == pooled_rows
    assert tracked == tracked_rows
    assert groups == group_rows
    assert tags == tag_rows
    assert by_tag == wallets_by_tag


@pytest.mark.asyncio
async def test_traders_sdk_directory_wrappers_passthrough(monkeypatch):
    pooled_rows = [{"address": "0xpool"}]
    tracked_rows = [{"address": "0xtracked"}]
    group_rows = [{"id": "grp-1"}]
    tag_rows = [{"name": "whale", "wallet_count": 2}]
    wallets_by_tag = [{"address": "0xtag"}]

    pooled_loader = AsyncMock(return_value=pooled_rows)
    tracked_loader = AsyncMock(return_value=tracked_rows)
    groups_loader = AsyncMock(return_value=group_rows)
    tags_loader = AsyncMock(return_value=tag_rows)
    by_tag_loader = AsyncMock(return_value=wallets_by_tag)

    monkeypatch.setattr(trader_data_access, "get_pooled_traders", pooled_loader)
    monkeypatch.setattr(trader_data_access, "get_tracked_traders", tracked_loader)
    monkeypatch.setattr(trader_data_access, "get_trader_groups", groups_loader)
    monkeypatch.setattr(trader_data_access, "get_trader_tags", tags_loader)
    monkeypatch.setattr(trader_data_access, "get_traders_by_tag", by_tag_loader)

    pooled = await TradersSDK.get_pooled_traders(
        limit=19,
        tier="high",
        include_blacklisted=False,
        tracked_only=True,
    )
    tracked = await TradersSDK.get_tracked_traders(
        limit=29,
        include_recent_activity=True,
        activity_hours=12,
    )
    groups = await TradersSDK.get_groups(
        include_members=True,
        member_limit=7,
    )
    tags = await TradersSDK.get_tags()
    by_tag = await TradersSDK.get_traders_by_tag("whale", limit=33)

    pooled_loader.assert_awaited_once_with(
        limit=19,
        tier="high",
        include_blacklisted=False,
        tracked_only=True,
    )
    tracked_loader.assert_awaited_once_with(
        limit=29,
        include_recent_activity=True,
        activity_hours=12,
    )
    groups_loader.assert_awaited_once_with(include_members=True, member_limit=7)
    tags_loader.assert_awaited_once_with()
    by_tag_loader.assert_awaited_once_with(tag_name="whale", limit=33)

    assert pooled == pooled_rows
    assert tracked == tracked_rows
    assert groups == group_rows
    assert tags == tag_rows
    assert by_tag == wallets_by_tag



def test_strategy_sdk_extract_trader_signal_wallets_reads_nested_context():
    payload = {
        "wallets": ["0x1111111111111111111111111111111111111111"],
        "strategy_context": {
            "wallet_addresses": ["0x2222222222222222222222222222222222222222"],
            "firehose": {
                "top_wallets": [
                    {"address": "0x3333333333333333333333333333333333333333"},
                ]
            },
        },
    }

    wallets = StrategySDK.extract_trader_signal_wallets(payload)
    assert wallets == {
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    }


def test_strategy_sdk_match_trader_signal_scope_uses_runtime_context():
    signal = {
        "strategy_context": {
            "firehose": {
                "wallets": [
                    "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ]
            }
        }
    }
    scope_context = StrategySDK.build_trader_scope_runtime_context(
        {"modes": ["pool"], "individual_wallets": [], "group_ids": []},
        pool_wallets={"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )

    matched, payload = StrategySDK.match_trader_signal_scope(signal, scope_context)
    assert matched is True
    assert payload["matched_modes"] == ["pool"]


def test_strategy_sdk_normalize_trader_wallet_weights_clamps_and_filters():
    weights = StrategySDK.normalize_trader_wallet_weights(
        {
            "0xAAA": "1.25",
            "0xbbb": "-10",
            "bad": "not-a-number",
        },
        min_weight=0.0,
        max_weight=2.0,
    )
    assert weights == {
        "0xaaa": 1.25,
        "0xbbb": 0.0,
    }


def test_strategy_sdk_extract_primary_trader_signal_wallet_prefers_copy_context():
    signal = {
        "wallets": ["0x1111111111111111111111111111111111111111"],
        "source_trade": {"wallet_address": "0x2222222222222222222222222222222222222222"},
        "strategy_context": {
            "copy_event": {"wallet_address": "0x3333333333333333333333333333333333333333"},
        },
    }
    assert (
        StrategySDK.extract_primary_trader_signal_wallet(signal)
        == "0x3333333333333333333333333333333333333333"
    )
