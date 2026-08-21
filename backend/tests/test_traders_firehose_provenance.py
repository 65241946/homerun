from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.trader_data_access as trader_data_access
import services.traders_firehose_pipeline as traders_firehose_pipeline
from services.opportunity_strategy_catalog import build_system_opportunity_strategy_rows
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


def test_unknown_direction_is_not_converted_to_no_opportunity() -> None:
    signal = _qualified_signal(outcome="", side="", signal_type="")
    signal["validation"] = {"is_valid": True, "checks": {}, "reasons": []}

    assert TradersConfluenceStrategy()._resolve_trade_outcome(signal) is None
    assert TradersConfluenceStrategy().build_opportunities_from_firehose([signal]) == []


def test_weighted_wallet_count_prefers_cluster_adjusted_count() -> None:
    strategy = TradersConfluenceStrategy()
    cfg = strategy._effective_config()
    signal = _qualified_signal(
        tier="high",
        wallets=["0x1", "0x2", "0x3", "0x4"],
        wallet_count=4,
        cluster_adjusted_wallet_count=1,
    )

    assert strategy._effective_wallet_count(signal, cfg) == pytest.approx(2.0)
    passed, reasons, checks = strategy.evaluate_firehose_signal(signal, cfg=cfg)
    assert passed is True
    assert reasons == []
    assert checks["meets_min_wallet_count"] is True

    signal["tier"] = "medium"
    passed, reasons, checks = strategy.evaluate_firehose_signal(signal, cfg=cfg)
    assert passed is False
    assert "insufficient_wallet_count" in reasons
    assert checks["meets_min_wallet_count"] is False


def test_confluence_default_age_and_weight_schema_are_reconciled() -> None:
    assert TradersConfluenceStrategy.DEFAULT_CONFIG["firehose_max_age_minutes"] == 60
    assert TradersConfluenceStrategy.DEFAULT_CONFIG["tier_weights"] == {
        "low": 1.0,
        "high": 2.0,
        "extreme": 3.0,
    }
    assert TradersConfluenceStrategy._normalize_tier_weights({"medium": 9.0}) == {
        "low": 1.0,
        "high": 2.0,
        "extreme": 3.0,
    }

    row = next(
        item
        for item in build_system_opportunity_strategy_rows()
        if item["slug"] == "traders_confluence"
    )
    fields = {field["key"]: field for field in row["config_schema"]["param_fields"]}
    assert "weighted" in fields["min_wallet_count"]["description"].lower()
    assert fields["tier_weights"]["type"] in {"object", "json"}
    assert "unknown tiers use low" in fields["tier_weights"]["description"].lower()

    at_limit = TradersConfluenceStrategy().evaluate_firehose_signal(
        _qualified_signal(firehose_age_minutes=60.0)
    )
    over_limit = TradersConfluenceStrategy().evaluate_firehose_signal(
        _qualified_signal(firehose_age_minutes=60.01)
    )
    assert at_limit[0] is True
    assert over_limit[0] is False
    assert "signal_too_old" in over_limit[1]


@pytest.mark.asyncio
async def test_firehose_runtime_keeps_strategy_age_default_when_db_config_omits_it(monkeypatch) -> None:
    strategy = TradersConfluenceStrategy()
    monkeypatch.setattr(
        traders_firehose_pipeline,
        "refresh_strategy_runtime_if_needed",
        AsyncMock(),
    )
    monkeypatch.setattr(
        traders_firehose_pipeline,
        "_load_strategy_row",
        AsyncMock(return_value=SimpleNamespace(enabled=True, config={})),
    )
    monkeypatch.setitem(
        traders_firehose_pipeline.strategy_loader._loaded,
        "traders_confluence",
        SimpleNamespace(instance=strategy),
    )

    resolved = await traders_firehose_pipeline._resolve_traders_strategy(None)

    assert resolved is strategy
    assert strategy._effective_config()["firehose_max_age_minutes"] == 60


def test_effective_config_is_validated_once_per_config_version(monkeypatch) -> None:
    strategy = TradersConfluenceStrategy()
    original_validate = StrategySDK.validate_trader_filter_config
    call_count = 0

    def _counting_validate(config):
        nonlocal call_count
        call_count += 1
        return original_validate(config)

    monkeypatch.setattr(StrategySDK, "validate_trader_filter_config", _counting_validate)
    first = strategy._effective_config()
    first["min_wallet_count"] = 999
    second = strategy._effective_config()
    assert second["min_wallet_count"] == 2
    assert call_count == 1

    strategy.configure({"min_wallet_count": 3})
    assert strategy._effective_config()["min_wallet_count"] == 3
    assert call_count == 2


def test_filtered_row_is_normalized_and_evaluated_only_once(monkeypatch) -> None:
    strategy = TradersConfluenceStrategy()
    normalize_count = 0
    evaluate_count = 0
    original_normalize = StrategySDK.normalize_trader_signal
    original_evaluate = strategy.evaluate_firehose_signal

    def _counting_normalize(signal):
        nonlocal normalize_count
        normalize_count += 1
        return original_normalize(signal)

    def _counting_evaluate(signal, *, cfg=None, normalized=False):
        nonlocal evaluate_count
        evaluate_count += 1
        return original_evaluate(signal, cfg=cfg, normalized=normalized)

    monkeypatch.setattr(StrategySDK, "normalize_trader_signal", _counting_normalize)
    monkeypatch.setattr(strategy, "evaluate_firehose_signal", _counting_evaluate)

    filtered = strategy.apply_firehose_filters([_qualified_signal()])
    opportunities = strategy.build_opportunities_from_firehose(filtered)

    assert len(opportunities) == 1
    assert normalize_count == 1
    assert evaluate_count == 1


@pytest.mark.asyncio
async def test_pipeline_does_not_normalize_before_strategy_filter(monkeypatch) -> None:
    strategy = TradersConfluenceStrategy()
    signal = _qualified_signal()
    signal.pop("source_flags")
    original_normalize = StrategySDK.normalize_trader_signal
    normalize_count = 0

    def _counting_normalize(row):
        nonlocal normalize_count
        normalize_count += 1
        return original_normalize(row)

    async def _resolve(_session):
        return strategy

    monkeypatch.setattr(StrategySDK, "normalize_trader_signal", _counting_normalize)
    monkeypatch.setattr(traders_firehose_pipeline, "_resolve_traders_strategy", _resolve)

    rows = await traders_firehose_pipeline.apply_traders_firehose_strategy(
        [signal],
        include_filtered=True,
    )

    assert normalize_count == 1
    assert "missing_source_provenance" in rows[0]["validation_reasons"]
    assert "unqualified_wallet_source" not in rows[0]["validation_reasons"]


def test_confluence_strength_and_tier_are_payload_local() -> None:
    strategy = TradersConfluenceStrategy()
    weak_low = {
        "strategy_context": {
            "confluence_strength": 0.2,
            "tier": "low",
        }
    }
    strong_high = {
        "strategy_context": {
            "confluence_strength": 0.9,
            "tier": "high",
        }
    }

    weak_score = strategy.compute_score(5.0, 0.8, 0.4, 1, weak_low)
    strong_score = strategy.compute_score(5.0, 0.8, 0.4, 1, strong_high)
    weak_size = strategy.compute_size(10.0, 100.0, 5.0, 0.8, 0.4, 1, payload=weak_low)
    strong_size = strategy.compute_size(10.0, 100.0, 5.0, 0.8, 0.4, 1, payload=strong_high)

    assert strong_score > weak_score
    assert strong_size > weak_size
    assert strategy.compute_score(5.0, 0.8, 0.4, 1, weak_low) == pytest.approx(weak_score)
    assert not hasattr(strategy, "_confluence_strength")
