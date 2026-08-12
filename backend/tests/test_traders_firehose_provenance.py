from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.trader_data_access as trader_data_access
import services.traders_firehose_pipeline as traders_firehose_pipeline
from services.strategies.traders_confluence import TradersConfluenceStrategy
from services.strategy_sdk import StrategySDK


def _qualified_signal(**overrides) -> dict:
    signal = {
        "market_id": "market-1",
        "market_question": "Will the test event happen?",
        "wallets": ["0xwallet1", "0xwallet2"],
        "outcome": "YES",
        "entry_price": 0.55,
        "source_flags": {
            "from_pool": True,
            "from_tracked_traders": False,
            "qualified": True,
        },
        "is_active": True,
        "is_tradeable": True,
        "firehose_market_tradable": True,
        "firehose_is_crypto": False,
        "firehose_age_minutes": 1.0,
        "firehose_confidence": 0.8,
        "tier": "low",
        "wallet_count": 2,
        "strength": 0.8,
    }
    signal.update(overrides)
    return signal


def test_real_pool_provenance_passes_source_gates() -> None:
    passed, reasons, checks = TradersConfluenceStrategy().evaluate_firehose_signal(_qualified_signal())

    assert passed is True
    assert reasons == []
    assert checks["has_source_provenance"] is True
    assert checks["has_qualified_source"] is True
    assert checks["matches_source_scope"] is True


def test_missing_provenance_has_distinct_rejection_reason() -> None:
    strategy = TradersConfluenceStrategy()
    signal = _qualified_signal()
    signal.pop("source_flags")

    filtered = strategy.apply_firehose_filters([signal], include_filtered=True)
    passed = filtered[0]["is_tradeable"]
    reasons = filtered[0]["validation_reasons"]

    assert passed is False
    assert "missing_source_provenance" in reasons
    assert "unqualified_wallet_source" not in reasons
    assert "source_scope_mismatch" not in reasons


def test_explicit_false_provenance_remains_an_actual_scope_rejection() -> None:
    signal = _qualified_signal(
        source_flags={
            "from_pool": False,
            "from_tracked_traders": False,
            "qualified": False,
        }
    )

    passed, reasons, _ = TradersConfluenceStrategy().evaluate_firehose_signal(signal)

    assert passed is False
    assert "missing_source_provenance" not in reasons
    assert "unqualified_wallet_source" in reasons
    assert "source_scope_mismatch" in reasons


@pytest.mark.asyncio
async def test_data_access_enriches_source_context_by_default(monkeypatch) -> None:
    raw_signal = _qualified_signal()
    raw_signal.pop("source_flags")
    loader = AsyncMock(return_value=[raw_signal])
    annotator = AsyncMock()
    monkeypatch.setattr(
        trader_data_access.smart_wallet_pool,
        "get_tracked_trader_firehose_signals",
        loader,
    )
    monkeypatch.setattr(trader_data_access, "annotate_trader_signal_source_context", annotator)

    rows = await trader_data_access.get_trader_firehose_signals(limit=10)

    annotator.assert_awaited_once_with(rows)


@pytest.mark.asyncio
async def test_strategy_filtered_pipeline_returns_qualified_rows(monkeypatch) -> None:
    signal = _qualified_signal()
    loader = AsyncMock(return_value=[signal])
    monkeypatch.setattr(StrategySDK, "get_trader_firehose_signals", loader)

    async def _apply(rows, *, include_filtered, limit):
        strategy = TradersConfluenceStrategy()
        prepared = await strategy.prepare_firehose_signals(rows)
        return strategy.apply_firehose_filters(
            prepared,
            include_filtered=include_filtered,
            limit=limit,
        )

    monkeypatch.setattr(traders_firehose_pipeline, "apply_traders_firehose_strategy", _apply)

    rows = await traders_firehose_pipeline.get_strategy_filtered_trader_opportunities(limit=10)

    assert len(rows) > 0
    assert rows[0]["source_flags"]["from_pool"] is True
    loader.assert_awaited_once_with(limit=250, include_filtered=False)


@pytest.mark.asyncio
async def test_detect_async_uses_default_source_context(monkeypatch) -> None:
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr(StrategySDK, "get_trader_firehose_signals", loader)

    result = await TradersConfluenceStrategy().detect_async([], [], {})

    assert result == []
    loader.assert_awaited_once_with(limit=250, include_filtered=True)


def test_dead_defaults_follow_declared_config_and_overrides() -> None:
    strategy = TradersConfluenceStrategy()

    cfg_without_tier = strategy._effective_config()
    cfg_without_tier.pop("min_tier")
    passed, reasons, _ = strategy.evaluate_firehose_signal(_qualified_signal(tier="low"), cfg=cfg_without_tier)
    assert passed is True
    assert "tier_below_threshold" not in reasons

    cfg_without_crypto_default = strategy._effective_config()
    cfg_without_crypto_default.pop("firehose_exclude_crypto_markets")
    passed, reasons, _ = strategy.evaluate_firehose_signal(
        _qualified_signal(firehose_is_crypto=True),
        cfg=cfg_without_crypto_default,
    )
    assert passed is True
    assert "crypto_market_excluded" not in reasons

    configured = strategy._effective_config()
    configured.update({"min_tier": "high", "firehose_exclude_crypto_markets": True})
    passed, reasons, _ = strategy.evaluate_firehose_signal(
        _qualified_signal(tier="low", firehose_is_crypto=True),
        cfg=configured,
    )
    assert passed is False
    assert "tier_below_threshold" in reasons
    assert "crypto_market_excluded" in reasons

    signal = SimpleNamespace(source="traders", signal_type="confluence", strategy_context_json={})
    payload = {
        "strategy_context": {"strategy_slug": strategy.strategy_type},
        "strength": 0.52,
    }
    default_checks = strategy.custom_checks(signal, {}, {}, payload)
    assert next(check for check in default_checks if check.key == "channel_threshold").passed is True

    configured_checks = strategy.custom_checks(
        signal,
        {},
        {"min_confluence_strength": 0.60},
        payload,
    )
    assert next(check for check in configured_checks if check.key == "channel_threshold").passed is False
