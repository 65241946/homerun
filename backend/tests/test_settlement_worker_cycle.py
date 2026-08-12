from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from config import Settings, settings
from models.database import (
    Base,
    OnlineMarketResolution,
    OnlineMarketResolutionObservation,
    SimulationAccount,
    SimulationCashLedgerEntry,
    TraderOrder,
    TraderOrderSettlement,
)
from services.online_resolution import ResolutionLookup, normalize_gamma_resolution
from services.online_settlement_cycle import (
    MAX_HTTP_CONCURRENCY,
    MAX_MARKETS_PER_CYCLE,
    PER_REQUEST_TIMEOUT_SECONDS,
    _fetch_resolution_observations,
    _load_candidate_batch,
    run_online_settlement_cycle,
)
from services.simulation import SimulationService
from services.simulation_ledger import initialize_new_v2_account
from tests.postgres_test_db import build_postgres_session_factory
from workers import trader_reconciliation_worker


def _condition_id(digit: str) -> str:
    return f"0x{digit * 64}"


def _final_payload(*, condition_id: str, provider_market_id: str) -> dict:
    return {
        "id": provider_market_id,
        "conditionId": condition_id,
        "clobTokenIds": ["10000000000000000001", "10000000000000000002"],
        "outcomes": ["YES", "NO"],
        "outcomePrices": ["1", "0"],
        "closed": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "resolved",
        "updatedAt": "2026-08-11T00:00:00Z",
    }


async def _seed_unresolved_shadow_order(
    session_factory,
    *,
    suffix: str,
    persist_identity: bool = True,
) -> dict[str, str]:
    account_id = f"account-{suffix}"
    order_id = f"order-{suffix}"
    condition_id = _condition_id("a" if suffix == "observe" else "b")
    provider_market_id = f"market-{suffix}"
    held_token_id = "10000000000000000001"
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        account = SimulationAccount(
            id=account_id,
            name=f"Settlement cycle {suffix}",
            initial_capital=1_000.0,
            current_capital=1_000.0,
            ledger_version=2,
            ledger_integrity_status="complete",
        )
        session.add(account)
        initialize_new_v2_account(session, account)
        await session.commit()

    async with session_factory() as session:
        opened = await SimulationService().record_orchestrator_shadow_fill(
            account_id=account_id,
            trader_id=f"trader-{suffix}",
            signal_id=f"signal-{suffix}",
            market_id=provider_market_id,
            market_question=f"Settlement cycle proof {suffix}",
            direction="buy_yes",
            notional_usd=100.0,
            entry_price=0.4,
            strategy_type="settlement_cycle_proof",
            token_id=held_token_id,
            payload={
                "market": {
                    "condition_id": condition_id,
                    "token_ids": [held_token_id, "10000000000000000002"],
                    "outcomes": ["YES", "NO"],
                }
            },
            session=session,
            commit=False,
        )
        session.add(
            TraderOrder(
                id=order_id,
                trader_id=f"trader-{suffix}",
                signal_id=f"signal-{suffix}",
                source="scanner",
                strategy_key="settlement_cycle_proof",
                market_id=provider_market_id,
                venue="polymarket" if persist_identity else None,
                provider_market_id=provider_market_id if persist_identity else None,
                condition_id=condition_id if persist_identity else None,
                token_id=held_token_id if persist_identity else None,
                outcome_index=0 if persist_identity else None,
                identity_status="complete" if persist_identity else None,
                market_question=f"Settlement cycle proof {suffix}",
                direction="buy_yes",
                mode="shadow",
                status="open",
                notional_usd=100.0,
                entry_price=0.4,
                effective_price=0.4,
                payload_json={
                    "market": {
                        "provider_market_id": provider_market_id,
                        "condition_id": condition_id,
                        "token_ids": [held_token_id, "10000000000000000002"],
                        "outcomes": ["YES", "NO"],
                    },
                    "simulation_ledger": {
                        "account_id": account_id,
                        "trade_id": opened["trade_id"],
                        "position_id": opened["position_id"],
                    },
                },
                created_at=now,
                executed_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    return {
        "account_id": account_id,
        "order_id": order_id,
        "condition_id": condition_id,
        "provider_market_id": provider_market_id,
    }


class _TrackingSessionFactory:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.active_sessions = 0

    def __call__(self):
        outer = self
        context = self.delegate()

        class _Context:
            async def __aenter__(self):
                session = await context.__aenter__()
                outer.active_sessions += 1
                return session

            async def __aexit__(self, exc_type, exc, traceback):
                try:
                    return await context.__aexit__(exc_type, exc, traceback)
                finally:
                    outer.active_sessions -= 1

        return _Context()


class _StaticFinalProvider:
    def __init__(self, tracking_factory: _TrackingSessionFactory) -> None:
        self.tracking_factory = tracking_factory
        self.calls = 0

    async def fetch_one(self, lookup: ResolutionLookup):
        assert self.tracking_factory.active_sessions == 0
        self.calls += 1
        return normalize_gamma_resolution(
            _final_payload(
                condition_id=lookup.condition_id,
                provider_market_id=str(lookup.provider_market_id),
            )
        )


def test_settlement_runtime_mode_defaults_to_observe_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("HOMERUN_SETTLEMENT_RUNTIME_MODE", raising=False)

    assert Settings(_env_file=None).HOMERUN_SETTLEMENT_RUNTIME_MODE == "observe"
    assert Settings(HOMERUN_SETTLEMENT_RUNTIME_MODE="SHADOW").HOMERUN_SETTLEMENT_RUNTIME_MODE == "shadow"
    with pytest.raises(ValidationError):
        Settings(HOMERUN_SETTLEMENT_RUNTIME_MODE="enabled")


@pytest.mark.asyncio
async def test_off_mode_does_not_open_database_or_call_provider():
    class _ForbiddenFactory:
        def __call__(self):
            raise AssertionError("off mode must not open a database session")

    class _ForbiddenProvider:
        async def fetch_one(self, lookup):
            raise AssertionError(f"off mode must not fetch {lookup}")

    result = await run_online_settlement_cycle(
        runtime_mode="off",
        session_factory=_ForbiddenFactory(),
        provider=_ForbiddenProvider(),
    )

    assert result["mode"] == "off"
    assert result["health"] == "disabled"
    assert result["observed"] == 0
    assert result["applied"] == 0


@pytest.mark.asyncio
async def test_observe_persists_final_fact_without_closing_order_or_crediting_cash():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_worker_observe")
    try:
        seeded = await _seed_unresolved_shadow_order(session_factory, suffix="observe")
        tracking_factory = _TrackingSessionFactory(session_factory)
        provider = _StaticFinalProvider(tracking_factory)

        result = await run_online_settlement_cycle(
            runtime_mode="observe",
            session_factory=tracking_factory,
            provider=provider,
        )

        async with session_factory() as session:
            account = await session.get(SimulationAccount, seeded["account_id"])
            order = await session.get(TraderOrder, seeded["order_id"])
            fact_count = await session.scalar(select(func.count()).select_from(OnlineMarketResolution))
            observation_count = await session.scalar(
                select(func.count()).select_from(OnlineMarketResolutionObservation)
            )
            settlement_count = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))

        assert provider.calls == 1
        assert tracking_factory.active_sessions == 0
        assert result["observed"] == 1
        assert result["final"] == 1
        assert result["would_settle"] == 1
        assert result["applied"] == 0
        assert fact_count == 1
        assert observation_count == 1
        assert settlement_count == 0
        assert account is not None and account.current_capital == pytest.approx(900.0)
        assert order is not None and order.status == "open"
        assert order.actual_profit is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_observe_discovers_legacy_identity_without_persisting_or_applying():
    engine, session_factory = await build_postgres_session_factory(
        Base,
        "settlement_worker_legacy_observe",
    )
    try:
        seeded = await _seed_unresolved_shadow_order(
            session_factory,
            suffix="legacy-observe",
            persist_identity=False,
        )
        tracking_factory = _TrackingSessionFactory(session_factory)
        provider = _StaticFinalProvider(tracking_factory)

        async with session_factory() as session:
            cash_before = await session.scalar(select(func.count()).select_from(SimulationCashLedgerEntry))

        result = await run_online_settlement_cycle(
            runtime_mode="observe",
            session_factory=tracking_factory,
            provider=provider,
        )

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded["order_id"])
            fact_count = await session.scalar(select(func.count()).select_from(OnlineMarketResolution))
            settlement_count = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))
            cash_after = await session.scalar(select(func.count()).select_from(SimulationCashLedgerEntry))

        assert provider.calls == 1
        assert tracking_factory.active_sessions == 0
        assert result["candidates"] == 1
        assert result["observed"] == 1
        assert result["final"] == 1
        assert result["blocked"] == 1
        assert result["health"] == "attention"
        assert result["applied"] == 0
        assert fact_count == 1
        assert settlement_count == 0
        assert cash_after == cash_before
        assert order is not None and order.status == "open"
        assert order.identity_status is None
        assert order.venue is None
        assert order.condition_id is None
        assert order.token_id is None
        assert order.outcome_index is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_does_not_auto_apply_transient_legacy_identity():
    engine, session_factory = await build_postgres_session_factory(
        Base,
        "settlement_worker_legacy_shadow_gate",
    )
    try:
        seeded = await _seed_unresolved_shadow_order(
            session_factory,
            suffix="legacy-shadow",
            persist_identity=False,
        )
        tracking_factory = _TrackingSessionFactory(session_factory)
        provider = _StaticFinalProvider(tracking_factory)

        result = await run_online_settlement_cycle(
            runtime_mode="shadow",
            session_factory=tracking_factory,
            provider=provider,
        )

        async with session_factory() as session:
            account = await session.get(SimulationAccount, seeded["account_id"])
            order = await session.get(TraderOrder, seeded["order_id"])
            fact_count = await session.scalar(select(func.count()).select_from(OnlineMarketResolution))
            settlement_count = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))

        assert provider.calls == 1
        assert result["observed"] == 1
        assert result["final"] == 1
        assert result["blocked"] == 1
        assert result["health"] == "attention"
        assert result["applied"] == 0
        assert fact_count == 1
        assert settlement_count == 0
        assert account is not None and account.current_capital == pytest.approx(900.0)
        assert order is not None and order.status == "open"
        assert order.identity_status is None
        assert order.condition_id is None
        assert order.token_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_mode_applies_persisted_final_fact_once():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_worker_shadow")
    try:
        seeded = await _seed_unresolved_shadow_order(session_factory, suffix="shadow")
        tracking_factory = _TrackingSessionFactory(session_factory)
        provider = _StaticFinalProvider(tracking_factory)

        result = await run_online_settlement_cycle(
            runtime_mode="shadow",
            session_factory=tracking_factory,
            provider=provider,
        )

        async with session_factory() as session:
            account = await session.get(SimulationAccount, seeded["account_id"])
            order = await session.get(TraderOrder, seeded["order_id"])
            settlement_count = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))

        assert result["final"] == 1
        assert result["applied"] == 1
        assert result["conflicted"] == 0
        assert settlement_count == 1
        assert account is not None and account.current_capital == pytest.approx(1_147.0)
        assert order is not None and order.status == "resolved_win"
        assert order.actual_profit == pytest.approx(147.0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_restart_uses_saved_final_fact_without_refetching_provider():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_worker_restart")
    try:
        seeded = await _seed_unresolved_shadow_order(session_factory, suffix="observe")
        tracking_factory = _TrackingSessionFactory(session_factory)
        provider = _StaticFinalProvider(tracking_factory)
        observe_result = await run_online_settlement_cycle(
            runtime_mode="observe",
            session_factory=tracking_factory,
            provider=provider,
        )
        assert observe_result["final"] == 1
        assert provider.calls == 1

        class _ForbiddenProvider:
            async def fetch_one(self, lookup):
                raise AssertionError(f"saved final fact must avoid provider refetch: {lookup}")

        shadow_result = await run_online_settlement_cycle(
            runtime_mode="shadow",
            session_factory=tracking_factory,
            provider=_ForbiddenProvider(),
        )

        async with session_factory() as session:
            account = await session.get(SimulationAccount, seeded["account_id"])
            order = await session.get(TraderOrder, seeded["order_id"])
            settlement_count = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))

        assert shadow_result["observed"] == 0
        assert shadow_result["final"] == 1
        assert shadow_result["applied"] == 1
        assert settlement_count == 1
        assert account is not None and account.current_capital == pytest.approx(1_147.0)
        assert order is not None and order.status == "resolved_win"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_batch_bounds_concurrency_and_isolates_one_market_failure():
    active = 0
    peak = 0

    class _Provider:
        async def fetch_one(self, lookup: ResolutionLookup):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                if lookup.provider_market_id == "market-5":
                    raise RuntimeError("provider failure")
                return normalize_gamma_resolution(
                    _final_payload(
                        condition_id=lookup.condition_id,
                        provider_market_id=str(lookup.provider_market_id),
                    )
                )
            finally:
                active -= 1

    lookups = [
        ResolutionLookup(
            condition_id=f"0x{index:064x}",
            provider_market_id=f"market-{index}",
        )
        for index in range(12)
    ]

    batch = await _fetch_resolution_observations(
        lookups,
        provider=_Provider(),
        max_concurrency=4,
        request_timeout_seconds=0.2,
    )

    assert peak == 4
    assert len(batch.observations) == 11
    assert batch.network_errors == 1
    assert batch.errors[0]["provider_market_id"] == "market-5"


def test_settlement_cycle_limits_match_reviewed_worker_budget():
    assert MAX_MARKETS_PER_CYCLE == 100
    assert MAX_HTTP_CONCURRENCY == 4
    assert PER_REQUEST_TIMEOUT_SECONDS == 8.0


@pytest.mark.asyncio
async def test_candidate_scan_caps_unique_valid_markets_without_malformed_rows_starving_batch():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_worker_batch_cap")
    try:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            for index in range(101):
                session.add(
                    TraderOrder(
                        id=f"batch-order-{index}",
                        trader_id="batch-trader",
                        source="scanner",
                        market_id=f"batch-market-{index}",
                        venue="polymarket",
                        provider_market_id=f"batch-market-{index}",
                        condition_id=f"0x{index:064x}",
                        token_id=str(20000000000000000000 + index),
                        outcome_index=0,
                        identity_status="complete",
                        mode="shadow",
                        status="open",
                        direction="buy_yes",
                        notional_usd=1.0,
                        entry_price=0.5,
                        effective_price=0.5,
                        payload_json={},
                        created_at=now,
                        executed_at=now,
                        updated_at=now,
                    )
                )
            for index in range(5):
                session.add(
                    TraderOrder(
                        id=f"malformed-order-{index}",
                        trader_id="batch-trader",
                        source="scanner",
                        market_id=f"malformed-market-{index}",
                        venue="polymarket",
                        provider_market_id=f"malformed-market-{index}",
                        condition_id=f"not-a-condition-{index}",
                        token_id=str(30000000000000000000 + index),
                        outcome_index=0,
                        identity_status="complete",
                        mode="shadow",
                        status="open",
                        direction="buy_yes",
                        notional_usd=1.0,
                        entry_price=0.5,
                        effective_price=0.5,
                        payload_json={},
                        created_at=now,
                        executed_at=now,
                        updated_at=now,
                    )
                )
            await session.commit()

        candidates, blocked = await _load_candidate_batch(
            session_factory,
            max_markets=MAX_MARKETS_PER_CYCLE,
        )

        assert len(candidates) == 100
        assert len({candidate.condition_id for candidate in candidates}) == 100
        assert blocked >= 5
        assert all(candidate.condition_id.startswith("0x") for candidate in candidates)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_mode_without_credentials_is_observation_only_and_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "POLYMARKET_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "POLYMARKET_API_KEY", None)
    monkeypatch.setattr(settings, "POLYMARKET_API_SECRET", None)
    monkeypatch.setattr(settings, "POLYMARKET_API_PASSPHRASE", None)

    result = await run_online_settlement_cycle(
        runtime_mode="live",
        session_factory=SimpleNamespace(),
        provider=SimpleNamespace(),
    )

    assert result["health"] == "not_configured"
    assert result["live_status"] == "not_configured"
    assert result["applied"] == 0


@pytest.mark.asyncio
async def test_worker_only_invokes_settlement_cycle_on_cold_plane(monkeypatch):
    calls = 0

    async def fake_cycle():
        nonlocal calls
        calls += 1
        return {"health": "healthy", "observed": 0, "applied": 0}

    monkeypatch.setattr(trader_reconciliation_worker, "run_online_settlement_cycle", fake_cycle)
    monkeypatch.setattr(trader_reconciliation_worker, "_IS_COLD_RECONCILE_PLANE", False)
    skipped = await trader_reconciliation_worker._run_online_settlement_post_cycle()
    assert calls == 0
    assert skipped["health"] == "plane_disabled"

    monkeypatch.setattr(trader_reconciliation_worker, "_IS_COLD_RECONCILE_PLANE", True)
    executed = await trader_reconciliation_worker._run_online_settlement_post_cycle()
    assert calls == 1
    assert executed["health"] == "healthy"


@pytest.mark.asyncio
async def test_worker_contains_settlement_timeout_and_returns_structured_stats(monkeypatch):
    async def slow_cycle():
        await asyncio.sleep(1.0)

    monkeypatch.setattr(trader_reconciliation_worker, "_IS_COLD_RECONCILE_PLANE", True)
    monkeypatch.setattr(trader_reconciliation_worker, "run_online_settlement_cycle", slow_cycle)
    monkeypatch.setattr(trader_reconciliation_worker, "_ONLINE_SETTLEMENT_CYCLE_TIMEOUT_SECONDS", 0.01)

    result = await trader_reconciliation_worker._run_online_settlement_post_cycle()

    assert result["health"] == "degraded"
    assert result["error_code"] == "cycle_timeout"
    assert result["applied"] == 0


@pytest.mark.asyncio
async def test_worker_scheduler_keeps_single_cycle_in_flight_without_blocking(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def controlled_cycle():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"health": "healthy", "observed": 1, "applied": 0}

    monkeypatch.setattr(trader_reconciliation_worker, "_IS_COLD_RECONCILE_PLANE", True)
    monkeypatch.setattr(trader_reconciliation_worker, "run_online_settlement_cycle", controlled_cycle)
    monkeypatch.setattr(trader_reconciliation_worker, "_online_settlement_cycle_task", None)
    monkeypatch.setattr(trader_reconciliation_worker, "_online_settlement_last_started_at", 0.0)
    monkeypatch.setattr(trader_reconciliation_worker, "_online_settlement_last_stats", None)

    first = trader_reconciliation_worker._schedule_online_settlement_post_cycle()
    await asyncio.wait_for(started.wait(), timeout=0.2)
    second = trader_reconciliation_worker._schedule_online_settlement_post_cycle()

    assert first["in_progress"] is True
    assert second["in_progress"] is True
    assert calls == 1

    release.set()
    task = trader_reconciliation_worker._online_settlement_cycle_task
    assert task is not None
    await asyncio.wait_for(task, timeout=0.2)
    harvested = trader_reconciliation_worker._schedule_online_settlement_post_cycle()

    assert calls == 1
    assert harvested["health"] == "healthy"
    assert harvested["in_progress"] is False
