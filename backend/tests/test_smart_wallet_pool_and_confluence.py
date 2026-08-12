"""Unit tests for smart wallet pool scoring/churn and confluence thresholds."""

import sys
import uuid
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from typing import Optional
from unittest.mock import AsyncMock
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from utils.utcnow import utcnow
import services.smart_wallet_pool as smart_wallet_pool_module  # noqa: E402
import services.wallet_intelligence as wallet_intelligence_module  # noqa: E402
from models.database import (  # noqa: E402
    Base,
    DiscoveredWallet,
    MarketConfluenceSignal,
    TraderGroup,
    TraderGroupMember,
    TrackedWallet,
    WalletActivityRollup,
)
from services.smart_wallet_pool import (  # noqa: E402
    SmartWalletPoolService,
    TARGET_POOL_SIZE,
    MIN_POOL_SIZE,
    MAX_HOURLY_REPLACEMENT_RATE,
    MAX_POOL_SIZE,
    SELECTION_SCORE_QUALITY_TARGET_FLOOR,
    POOL_FLAG_BLACKLISTED,
    POOL_FLAG_MANUAL_EXCLUDE,
    POOL_FLAG_MANUAL_INCLUDE,
)
from services.wallet_intelligence import ConfluenceDetector  # noqa: E402
from tests.postgres_test_db import build_postgres_session_factory  # noqa: E402


@pytest.mark.asyncio
async def test_deactivate_market_signals_persists_closed_market_state(tmp_path, monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "confluence_closed_markets")
    monkeypatch.setattr(wallet_intelligence_module, "AsyncSessionLocal", session_factory)

    async with session_factory() as session:
        session.add_all(
            [
                MarketConfluenceSignal(
                    id="closed-active",
                    market_id="0xclosed",
                    signal_type="multi_wallet_buy",
                    outcome="YES",
                    is_active=True,
                ),
                MarketConfluenceSignal(
                    id="closed-inactive",
                    market_id="0xclosed",
                    signal_type="multi_wallet_sell",
                    outcome="NO",
                    is_active=False,
                ),
                MarketConfluenceSignal(
                    id="open-active",
                    market_id="0xopen",
                    signal_type="multi_wallet_buy",
                    outcome="YES",
                    is_active=True,
                ),
            ]
        )
        await session.commit()

    detector = ConfluenceDetector()
    deactivated = await detector.deactivate_market_signals([" 0xCLOSED ", "", "0xclosed"])

    async with session_factory() as session:
        closed_active = await session.get(MarketConfluenceSignal, "closed-active")
        closed_inactive = await session.get(MarketConfluenceSignal, "closed-inactive")
        open_active = await session.get(MarketConfluenceSignal, "open-active")

    assert deactivated == 1
    assert closed_active.is_active is False
    assert closed_active.expired_at is not None
    assert closed_inactive.is_active is False
    assert open_active.is_active is True
    assert open_active.expired_at is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_market_context_uses_fresh_metadata_for_tradability(monkeypatch):
    detector = ConfluenceDetector()
    market_id = "0xclosed"
    lookup = AsyncMock(
        return_value={
            "question": "Closed market",
            "slug": "closed-market",
            "closed": True,
            "active": False,
            "accepting_orders": False,
        }
    )
    monkeypatch.setattr(
        wallet_intelligence_module.polymarket_client,
        "get_market_by_condition_id",
        lookup,
    )

    context = await detector._resolve_market_context(market_id)

    lookup.assert_awaited_once_with(market_id, force_refresh=True)
    assert context["tradability_confirmed"] is True
    assert context["is_tradeable"] is False


@pytest.mark.asyncio
async def test_signal_upsert_does_not_reactivate_untradeable_market(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "confluence_upsert_tradability")
    monkeypatch.setattr(wallet_intelligence_module, "AsyncSessionLocal", session_factory)
    detector = ConfluenceDetector()

    payload = {
        "market_id": "0xclosed",
        "market_question": "Closed market",
        "market_slug": "closed-market",
        "signal_type": "multi_wallet_buy",
        "strength": 0.8,
        "conviction_score": 80.0,
        "tier": "HIGH",
        "window_minutes": 15,
        "wallet_count": 3,
        "cluster_adjusted_wallet_count": 3,
        "unique_core_wallets": 1,
        "weighted_wallet_score": 0.8,
        "wallets": ["0xa", "0xb", "0xc"],
        "outcome": "YES",
        "avg_entry_price": 0.5,
        "total_size": 30.0,
        "avg_wallet_rank": 0.7,
        "net_notional": 15.0,
        "conflicting_notional": 0.0,
        "market_liquidity": 1000.0,
        "market_volume_24h": 2000.0,
        "is_tradeable": False,
    }

    await detector._batch_upsert_signals([payload])
    async with session_factory() as session:
        created = (
            await session.execute(
                wallet_intelligence_module.select(MarketConfluenceSignal).where(
                    MarketConfluenceSignal.market_id == "0xclosed"
                )
            )
        ).scalar_one()
        assert created.is_active is False
        assert created.expired_at is not None

    active_payload = dict(payload, is_tradeable=True)
    await detector._batch_upsert_signals([active_payload])
    await detector._batch_upsert_signals([payload])
    async with session_factory() as session:
        refreshed = (
            await session.execute(
                wallet_intelligence_module.select(MarketConfluenceSignal).where(
                    MarketConfluenceSignal.market_id == "0xclosed"
                )
            )
        ).scalar_one()
        assert refreshed.is_active is False
        assert refreshed.expired_at is not None

    await engine.dispose()


def _wallet(
    *,
    rank_score: float,
    win_rate: float,
    sharpe_ratio: Optional[float],
    profit_factor: Optional[float],
    total_pnl: float,
    max_drawdown: Optional[float] = None,
    roi_std: float = 0.0,
    anomaly_score: float = 0.0,
    cluster_id: Optional[str] = None,
    is_profitable: bool = False,
    metrics_source_version: Optional[str] = "accuracy_v2_closed_positions",
    last_analyzed_at=None,
):
    from utils.utcnow import utcnow as _utcnow

    return SimpleNamespace(
        rank_score=rank_score,
        win_rate=win_rate,
        sharpe_ratio=sharpe_ratio,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        max_drawdown=max_drawdown,
        roi_std=roi_std,
        anomaly_score=anomaly_score,
        cluster_id=cluster_id,
        is_profitable=is_profitable,
        metrics_source_version=metrics_source_version,
        last_analyzed_at=last_analyzed_at if last_analyzed_at is not None else _utcnow(),
    )


class TestWalletActivityRollupIdentity:
    def test_rollup_identity_dedupes_same_tx_across_sources(self):
        svc = SmartWalletPoolService()
        traded_at = utcnow()

        first = {
            "wallet_address": "0xabc",
            "market_id": "market-1",
            "side": "BUY",
            "price": 0.44,
            "size": 10.0,
            "notional": 4.4,
            "tx_hash": "0xdeadbeef",
            "source": "wallet_trades_api",
            "traded_at": traded_at,
        }
        second = {
            **first,
            "price": 0.441,
            "size": 10.5,
            "source": "activity_api",
            "tx_hash": "0xDEADBEEF",
        }

        assert svc._build_wallet_activity_rollup_id(first) == svc._build_wallet_activity_rollup_id(second)

    def test_rollup_identity_without_tx_hash_uses_time_and_size_signature(self):
        svc = SmartWalletPoolService()
        traded_at = utcnow().replace(microsecond=123000)

        first = {
            "wallet_address": "0xabc",
            "market_id": "market-1",
            "side": "BUY",
            "price": 0.44,
            "size": 10.0,
            "notional": 4.4,
            "tx_hash": None,
            "source": "wallet_trades_api",
            "traded_at": traded_at,
        }
        second = {**first, "size": 11.0}

        assert svc._build_wallet_activity_rollup_id(first) != svc._build_wallet_activity_rollup_id(second)

    def test_event_record_preserves_current_data_api_instrument_identity(self):
        svc = SmartWalletPoolService()
        traded_at = utcnow()

        record = svc._event_record(
            wallet="0xabc",
            market_id="0x" + "1" * 64,
            side="BUY",
            size=15.0,
            price=0.82,
            traded_at=traded_at,
            source="wallet_trades_api",
            tx_hash="0xtrade",
            token_id="24674095336808547940535196874152354815560124295320806027794449038192128385578",
            outcome="Down",
            outcome_index=1,
        )

        assert record["market_id"] == "0x" + "1" * 64
        assert record["token_id"].startswith("246740")
        assert record["outcome"] == "Down"
        assert record["outcome_index"] == 1


@pytest.mark.asyncio
async def test_wallet_trade_collector_preserves_current_data_api_fields():
    svc = SmartWalletPoolService()

    async def _get_wallet_trades(_address, *, limit):
        assert limit == 25
        return [
            {
                "proxyWallet": "0xabc",
                "side": "BUY",
                "asset": "123456789",
                "conditionId": "0x" + "2" * 64,
                "size": 12.5,
                "price": 0.31,
                "timestamp": 1_786_327_509,
                "outcome": "Away Team",
                "outcomeIndex": 1,
                "transactionHash": "0xwallettrade",
            }
        ]

    svc.client = SimpleNamespace(get_wallet_trades=_get_wallet_trades)
    candidates = defaultdict(lambda: defaultdict(bool))
    events: list[dict] = []

    await svc._collect_wallet_trade_candidates(
        candidates,
        events,
        wallet_addresses=["0xABC"],
        per_wallet_limit=25,
    )

    assert len(events) == 1
    assert events[0]["market_id"] == "0x" + "2" * 64
    assert events[0]["token_id"] == "123456789"
    assert events[0]["outcome"] == "Away Team"
    assert events[0]["outcome_index"] == 1


@pytest.mark.asyncio
async def test_market_trade_collector_accepts_proxy_wallet_current_contract():
    svc = SmartWalletPoolService()

    async def _get_markets(**_kwargs):
        return [SimpleNamespace(condition_id="0x" + "3" * 64, liquidity=100.0, volume=200.0)]

    async def _get_market_trades(_market_id, *, limit):
        assert limit == 20
        return [
            {
                "proxyWallet": "0xfeed",
                "side": "SELL",
                "asset": "987654321",
                "conditionId": "0x" + "3" * 64,
                "size": 4.0,
                "price": 0.7,
                "timestamp": 1_786_327_509,
                "outcome": "Home Team",
                "outcomeIndex": 0,
                "transactionHash": "0xmarkettrade",
            }
        ]

    svc.client = SimpleNamespace(get_markets=_get_markets, get_market_trades=_get_market_trades)
    candidates = defaultdict(lambda: defaultdict(bool))
    events: list[dict] = []

    await svc._collect_market_trade_candidates(
        candidates,
        events,
        max_markets=1,
        max_trades_per_market=20,
    )

    assert len(events) == 1
    assert events[0]["wallet_address"] == "0xfeed"
    assert events[0]["side"] == "SELL"
    assert events[0]["token_id"] == "987654321"
    assert events[0]["outcome_index"] == 0


@pytest.mark.asyncio
async def test_rollup_upsert_replaces_placeholder_outcome_index_with_confirmed_binary_index(
    tmp_path,
    monkeypatch,
):
    engine, session_factory = await build_postgres_session_factory(Base, "rollup_outcome_index_enrichment")
    monkeypatch.setattr(smart_wallet_pool_module, "AsyncSessionLocal", session_factory)

    traded_at = utcnow()
    common = {
        "wallet": "0xabc",
        "market_id": "0x" + "5" * 64,
        "side": "BUY",
        "size": 5.0,
        "price": 0.4,
        "traded_at": traded_at,
        "source": "wallet_trades_api",
        "tx_hash": "0xenrich",
        "token_id": "555",
        "outcome": "Away",
    }
    first_service = SmartWalletPoolService()
    first_event = first_service._event_record(**common, outcome_index=999)
    await first_service._persist_activity_events([first_event])

    # A later authoritative observation of the same event can correct the
    # API's placeholder index without inventing a value from BUY/SELL.
    second_service = SmartWalletPoolService()
    second_event = second_service._event_record(**common, outcome_index=1)
    await second_service._persist_activity_events([second_event])

    rollup_id = second_service._build_wallet_activity_rollup_id(second_event)
    async with session_factory() as session:
        row = await session.get(WalletActivityRollup, rollup_id)

    assert row is not None
    assert row.token_id == "555"
    assert row.outcome == "Away"
    assert row.outcome_index == 1

    await engine.dispose()


class TestSmartWalletPoolScoring:
    def test_quality_score_monotonic_for_better_wallet(self):
        svc = SmartWalletPoolService()
        weak = _wallet(
            rank_score=0.20,
            win_rate=0.45,
            sharpe_ratio=0.4,
            profit_factor=1.1,
            total_pnl=500.0,
        )
        strong = _wallet(
            rank_score=0.90,
            win_rate=0.72,
            sharpe_ratio=2.4,
            profit_factor=4.2,
            total_pnl=25000.0,
        )

        assert svc._score_quality(strong) > svc._score_quality(weak)

    def test_activity_score_decays_with_recency(self):
        svc = SmartWalletPoolService()
        now = utcnow()

        fresh_score = svc._score_activity(
            trades_1h=6,
            trades_24h=40,
            last_trade_at=now - timedelta(minutes=2),
            now=now,
        )
        stale_score = svc._score_activity(
            trades_1h=0,
            trades_24h=1,
            last_trade_at=now - timedelta(hours=96),
            now=now,
        )

        assert 0.0 <= fresh_score <= 1.0
        assert 0.0 <= stale_score <= 1.0
        assert fresh_score > stale_score

    def test_source_confidence_rewards_broader_signal_coverage(self):
        svc = SmartWalletPoolService()
        light = SimpleNamespace(source_flags={"leaderboard": True})
        broad = SimpleNamespace(
            source_flags={
                "leaderboard": True,
                "leaderboard_pnl": True,
                "wallet_trades": True,
                "market_trades": True,
                "activity": True,
            }
        )
        assert svc._score_source_confidence(broad) > svc._score_source_confidence(light)

    def test_quality_gate_blockers_cover_unanalyzed_low_quality_wallets(self):
        svc = SmartWalletPoolService()
        wallet = SimpleNamespace(
            last_analyzed_at=None,
            recommendation="unanalyzed",
            total_trades=12,
            anomaly_score=0.74,
            total_pnl=-150.0,
        )
        blockers = svc._eligibility_blockers(wallet)
        codes = {b.get("code") for b in blockers}
        assert "not_analyzed" in codes
        assert "recommendation_blocked" in codes
        assert "insufficient_trades" in codes
        assert "anomaly_too_high" in codes
        assert "non_positive_pnl" in codes

    def test_activity_score_downweights_unverified_metrics_profiles(self):
        svc = SmartWalletPoolService()
        now = utcnow()
        verified_wallet = SimpleNamespace(
            metrics_source_version="accuracy_v2_closed_positions",
            last_analyzed_at=now - timedelta(hours=2),
        )
        legacy_wallet = SimpleNamespace(
            metrics_source_version=None,
            last_analyzed_at=now - timedelta(hours=2),
        )
        verified = svc._score_activity(
            trades_1h=4,
            trades_24h=18,
            last_trade_at=now - timedelta(minutes=10),
            now=now,
            wallet=verified_wallet,
        )
        legacy = svc._score_activity(
            trades_1h=4,
            trades_24h=18,
            last_trade_at=now - timedelta(minutes=10),
            now=now,
            wallet=legacy_wallet,
        )
        assert verified > legacy

    def test_selection_reasons_include_manual_churn_and_insider_alignment(self):
        svc = SmartWalletPoolService()
        now = utcnow()
        wallet = SimpleNamespace(
            source_flags={POOL_FLAG_MANUAL_INCLUDE: True},
            quality_score=0.8,
            trades_1h=0,
            trades_24h=4,
            last_trade_at=now - timedelta(hours=2),
            cluster_id=None,
        )
        reasons = svc._derive_selection_reasons(
            wallet=wallet,
            address="wallet_a",
            selection_score=0.86,
            insider_score=0.8,
            cutoff_72h=now - timedelta(hours=72),
            desired_addresses=set(),
            current_addresses={"wallet_a"},
            cluster_count=0,
            cluster_cap=10,
        )
        codes = {r.get("code") for r in reasons}
        assert "manual_include" in codes
        assert "churn_guard_retained" in codes
        assert "insider_alignment" in codes

    def test_selection_score_clamps_components(self):
        svc = SmartWalletPoolService()
        score = svc._score_selection(
            composite=1.4,
            rank_score=-0.4,
            insider_score=0.2,
            source_confidence=2.0,
            diversity_score=-1.0,
            momentum_score=0.5,
        )
        assert score == pytest.approx(0.711, abs=1e-12)


class TestSmartWalletPoolChurnGuard:
    def test_effective_target_shrinks_in_quality_only_mode(self):
        svc = SmartWalletPoolService()
        strong = {f"strong_{i}": SELECTION_SCORE_QUALITY_TARGET_FLOOR + 0.05 for i in range(125)}
        weak = {f"weak_{i}": SELECTION_SCORE_QUALITY_TARGET_FLOOR - 0.05 for i in range(240)}
        scores = {**strong, **weak}
        eligible = set(scores.keys())

        target = svc._effective_target_pool_size(
            selection_scores=scores,
            eligible_addresses=eligible,
            manual_includes=[],
            quality_only_mode=True,
        )

        assert target == len(strong)

    def test_effective_target_keeps_balanced_minimum(self):
        svc = SmartWalletPoolService()
        strong = {f"strong_{i}": SELECTION_SCORE_QUALITY_TARGET_FLOOR + 0.05 for i in range(125)}
        weak = {f"weak_{i}": SELECTION_SCORE_QUALITY_TARGET_FLOOR - 0.05 for i in range(240)}
        scores = {**strong, **weak}
        eligible = set(scores.keys())

        target = svc._effective_target_pool_size(
            selection_scores=scores,
            eligible_addresses=eligible,
            manual_includes=[],
            quality_only_mode=False,
        )

        assert target == MIN_POOL_SIZE

    def test_quality_only_mode_allows_pool_shrink_below_minimum(self):
        svc = SmartWalletPoolService()
        current = [f"cur_{i}" for i in range(200)]
        desired = [f"elite_{i}" for i in range(30)]
        scores = {address: 0.5 for address in current + desired}

        final_pool, _ = svc._apply_churn_guard(
            desired=desired,
            current=current,
            scores=scores,
            quality_only_mode=True,
        )

        assert len(final_pool) == len(desired)
        assert set(final_pool) == set(desired)

    def test_quality_only_churn_counts_slot_turnover_without_double_count(self):
        svc = SmartWalletPoolService()
        current = [f"cur_{i}" for i in range(100)]
        desired = [f"cur_{i}" for i in range(99)] + ["new_0"]
        scores = {address: 0.5 for address in current + desired}

        _, churn_rate = svc._apply_churn_guard(
            desired=desired,
            current=current,
            scores=scores,
            quality_only_mode=True,
        )

        # One replacement over a 100-wallet baseline should be 1%.
        assert abs(churn_rate - 0.01) < 1e-9

    def test_replacements_capped_when_score_delta_is_small(self):
        svc = SmartWalletPoolService()
        current = [f"cur_{i}" for i in range(TARGET_POOL_SIZE)]
        desired = [f"new_{i}" for i in range(TARGET_POOL_SIZE)]

        scores = {address: 0.50 for address in current}
        scores.update({address: 0.53 for address in desired})

        final_pool, churn_rate = svc._apply_churn_guard(
            desired=desired,
            current=current,
            scores=scores,
        )

        replacements = len(set(final_pool) - set(current))
        cap = int(TARGET_POOL_SIZE * MAX_HOURLY_REPLACEMENT_RATE)

        assert replacements <= cap
        assert churn_rate <= cap / TARGET_POOL_SIZE

    def test_replacements_can_exceed_cap_when_score_delta_is_large(self):
        svc = SmartWalletPoolService()
        current = [f"cur_{i}" for i in range(TARGET_POOL_SIZE)]
        desired = [f"new_{i}" for i in range(TARGET_POOL_SIZE)]

        scores = {address: 0.20 for address in current}
        scores.update({address: 0.95 for address in desired})

        final_pool, _ = svc._apply_churn_guard(
            desired=desired,
            current=current,
            scores=scores,
        )

        replacements = len(set(final_pool) - set(current))
        cap = int(TARGET_POOL_SIZE * MAX_HOURLY_REPLACEMENT_RATE)
        assert replacements > cap

    def test_manual_include_enforcement_adds_missing_addresses(self):
        svc = SmartWalletPoolService()
        base_pool = [f"cur_{i}" for i in range(10)]
        manual = ["wallet_manual_1", "wallet_manual_2"]
        scores = {address: 0.5 for address in base_pool + manual}
        eligible = set(base_pool + manual)

        final = svc._enforce_manual_includes(
            final_pool=base_pool,
            manual_includes=manual,
            scores=scores,
            eligible_addresses=eligible,
        )

        assert "wallet_manual_1" in final
        assert "wallet_manual_2" in final
        assert len(final) <= MAX_POOL_SIZE

    def test_pool_block_flags_take_precedence(self):
        svc = SmartWalletPoolService()
        wallet = SimpleNamespace(
            source_flags={
                POOL_FLAG_MANUAL_INCLUDE: True,
                POOL_FLAG_MANUAL_EXCLUDE: True,
                POOL_FLAG_BLACKLISTED: False,
            }
        )
        assert svc._is_pool_manually_included(wallet) is True
        assert svc._is_pool_manually_excluded(wallet) is True
        assert svc._is_pool_blocked(wallet) is True

    def test_cluster_diversity_limits_single_cluster_domination(self):
        svc = SmartWalletPoolService()
        crowded = [SimpleNamespace(address=f"cluster_{i}", cluster_id="entity_a") for i in range(80)]
        independent = [SimpleNamespace(address=f"ind_{i}", cluster_id=None) for i in range(120)]
        ranked = crowded + independent
        target_size = 100
        diversified = svc._rank_with_cluster_diversity(ranked, target_size=target_size)
        cap = max(3, int(target_size * 0.08))
        crowded_count = sum(1 for w in diversified if getattr(w, "cluster_id", None) == "entity_a")
        assert crowded_count <= cap


class TestConfluenceDetectorThresholds:
    @pytest.mark.parametrize(
        ("side", "outcome", "outcome_index", "expected"),
        [
            ("BUY", "Up", 0, "YES"),
            ("BUY", "Down", 1, "NO"),
            ("SELL", "Up", 0, "NO"),
            ("SELL", "Down", 1, "YES"),
        ],
    )
    def test_effective_outcome_uses_trade_side_and_token_outcome_index(
        self,
        side,
        outcome,
        outcome_index,
        expected,
    ):
        detector = ConfluenceDetector()

        assert detector._resolve_effective_outcome(side, outcome, outcome_index) == expected

    def test_effective_outcome_rejects_legacy_rows_without_outcome_identity(self):
        detector = ConfluenceDetector()

        assert detector._resolve_effective_outcome("BUY", None, None) is None
        assert detector._resolve_effective_outcome("SELL", None, None) is None

    def test_sell_entry_price_is_converted_to_opposite_binary_outcome(self):
        detector = ConfluenceDetector()

        assert detector._canonical_entry_price("BUY", 0.72) == pytest.approx(0.72)
        assert detector._canonical_entry_price("SELL", 0.72) == pytest.approx(0.28)

    def test_tier_thresholds_follow_watch_high_extreme(self):
        detector = ConfluenceDetector()
        assert detector._tier_for_count(3) == "WATCH"
        assert detector._tier_for_count(4) == "HIGH"
        assert detector._tier_for_count(5) == "HIGH"
        assert detector._tier_for_count(6) == "EXTREME"
        assert detector._tier_for_count(12) == "EXTREME"

    def test_conviction_score_clamped_and_directional(self):
        detector = ConfluenceDetector()

        best_case = detector._conviction_score(
            adjusted_wallet_count=30,
            weighted_wallet_score=1.0,
            timing_tightness=1.0,
            net_notional=1_000_000_000.0,
            conflicting_notional=0.0,
            market_liquidity=1_000_000_000.0,
            market_volume_24h=1_000_000_000.0,
            anomaly_avg=0.0,
            unique_wallet_count=30,
        )
        worst_case = detector._conviction_score(
            adjusted_wallet_count=0,
            weighted_wallet_score=0.0,
            timing_tightness=0.0,
            net_notional=0.0,
            conflicting_notional=10_000.0,
            market_liquidity=0.0,
            market_volume_24h=0.0,
            anomaly_avg=1.0,
            unique_wallet_count=10,
        )

        assert 0.0 <= best_case <= 100.0
        assert 0.0 <= worst_case <= 100.0
        assert best_case > worst_case


@pytest.mark.asyncio
async def test_confluence_qualifying_wallets_only_include_recent_activity_candidates(tmp_path, monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "confluence_recent_qualifiers")
    monkeypatch.setattr(wallet_intelligence_module, "AsyncSessionLocal", session_factory)

    now = utcnow()
    cutoff_60m = now - timedelta(minutes=60)

    async with session_factory() as session:
        session.add_all(
            [
                DiscoveredWallet(
                    address="recent_ranked",
                    rank_score=0.95,
                    composite_score=0.81,
                    anomaly_score=0.05,
                ),
                DiscoveredWallet(
                    address="stale_ranked",
                    rank_score=0.99,
                    composite_score=0.88,
                    anomaly_score=0.03,
                ),
                TrackedWallet(address="tracked_recent"),
                TrackedWallet(address="tracked_stale"),
                TraderGroup(id="group-1", name="Unit Test Group", is_active=True),
                TraderGroupMember(
                    id="group-member-1",
                    group_id="group-1",
                    wallet_address="group_recent",
                ),
                WalletActivityRollup(
                    id=str(uuid.uuid4()),
                    wallet_address="recent_ranked",
                    market_id="market-1",
                    side="BUY",
                    traded_at=now - timedelta(minutes=5),
                    source="unit_test",
                ),
                WalletActivityRollup(
                    id=str(uuid.uuid4()),
                    wallet_address="tracked_recent",
                    market_id="market-2",
                    side="BUY",
                    traded_at=now - timedelta(minutes=7),
                    source="unit_test",
                ),
                WalletActivityRollup(
                    id=str(uuid.uuid4()),
                    wallet_address="group_recent",
                    market_id="market-3",
                    side="SELL",
                    traded_at=now - timedelta(minutes=9),
                    source="unit_test",
                ),
            ]
        )
        await session.commit()

    detector = ConfluenceDetector()
    wallets = await detector._get_qualifying_wallets(cutoff_60m)
    addresses = {str(row.get("address") or "") for row in wallets}

    assert "recent_ranked" in addresses
    assert "tracked_recent" in addresses
    assert "group_recent" in addresses
    assert "stale_ranked" not in addresses
    assert "tracked_stale" not in addresses

    await engine.dispose()


@pytest.mark.asyncio
async def test_confluence_groups_by_effective_binary_outcome_not_raw_buy_sell(tmp_path, monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "confluence_effective_outcomes")
    monkeypatch.setattr(wallet_intelligence_module, "AsyncSessionLocal", session_factory)

    now = utcnow()
    market_id = "0x" + "4" * 64
    wallets = ["buy_yes", "sell_no", "buy_no", "sell_yes", "legacy_buy_1", "legacy_buy_2"]
    async with session_factory() as session:
        session.add_all(
            [
                DiscoveredWallet(
                    address=address,
                    rank_score=0.9,
                    composite_score=0.8,
                    anomaly_score=0.0,
                )
                for address in wallets
            ]
        )
        session.add_all(
            [
                WalletActivityRollup(
                    id="buy-yes",
                    wallet_address="buy_yes",
                    market_id=market_id,
                    side="BUY",
                    token_id="yes-token",
                    outcome="Home",
                    outcome_index=0,
                    price=0.72,
                    size=10,
                    notional=7.2,
                    traded_at=now - timedelta(minutes=2),
                    source="unit_test",
                ),
                WalletActivityRollup(
                    id="sell-no",
                    wallet_address="sell_no",
                    market_id=market_id,
                    side="SELL",
                    token_id="no-token",
                    outcome="Away",
                    outcome_index=1,
                    price=0.28,
                    size=10,
                    notional=2.8,
                    traded_at=now - timedelta(minutes=3),
                    source="unit_test",
                ),
                WalletActivityRollup(
                    id="buy-no",
                    wallet_address="buy_no",
                    market_id=market_id,
                    side="BUY",
                    token_id="no-token",
                    outcome="Away",
                    outcome_index=1,
                    price=0.31,
                    size=10,
                    notional=3.1,
                    traded_at=now - timedelta(minutes=4),
                    source="unit_test",
                ),
                WalletActivityRollup(
                    id="sell-yes",
                    wallet_address="sell_yes",
                    market_id=market_id,
                    side="SELL",
                    token_id="yes-token",
                    outcome="Home",
                    outcome_index=0,
                    price=0.69,
                    size=10,
                    notional=6.9,
                    traded_at=now - timedelta(minutes=5),
                    source="unit_test",
                ),
                # Legacy rows have no token outcome identity.  They must not
                # be guessed as YES merely because their raw side is BUY.
                WalletActivityRollup(
                    id="legacy-1",
                    wallet_address="legacy_buy_1",
                    market_id=market_id,
                    side="BUY",
                    price=0.5,
                    size=100,
                    notional=50,
                    traded_at=now - timedelta(minutes=6),
                    source="unit_test",
                ),
                WalletActivityRollup(
                    id="legacy-2",
                    wallet_address="legacy_buy_2",
                    market_id=market_id,
                    side="BUY",
                    price=0.5,
                    size=100,
                    notional=50,
                    traded_at=now - timedelta(minutes=7),
                    source="unit_test",
                ),
            ]
        )
        await session.commit()

    detector = ConfluenceDetector()

    async def _market_contexts(market_ids):
        assert market_ids == [market_id]
        return {
            market_id: {
                "question": "Home vs Away",
                "market_slug": "home-vs-away",
                "liquidity": 10_000.0,
                "volume_24h": 20_000.0,
                "tradability_confirmed": True,
                "is_tradeable": True,
            }
        }

    monkeypatch.setattr(detector, "_batch_resolve_market_context", _market_contexts)

    signals = await detector.scan_for_confluence()
    by_outcome = {row["outcome"]: row for row in signals}

    assert set(by_outcome) == {"YES", "NO"}
    assert set(by_outcome["YES"]["wallets"]) == {"buy_yes", "sell_no"}
    assert set(by_outcome["NO"]["wallets"]) == {"buy_no", "sell_yes"}
    assert by_outcome["YES"]["avg_entry_price"] == pytest.approx(0.72)
    assert by_outcome["NO"]["avg_entry_price"] == pytest.approx(0.31)
    assert by_outcome["YES"]["net_notional"] == pytest.approx(14.4)
    assert by_outcome["NO"]["net_notional"] == pytest.approx(6.2)

    await engine.dispose()
