from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import services.settlement_repair as settlement_repair_module
from api import routes_simulation
from models.database import (
    Base,
    OnlineMarketResolution,
    PositionSide,
    SimulationAccount,
    SimulationCashLedgerEntry,
    SimulationPosition,
    SimulationTrade,
    TraderOrder,
    TraderOrderSettlement,
    TradeStatus,
)
from services.settlement_coordinator import SettlementCoordinatorError
from services.settlement_repair import (
    SettlementRepairConflict,
    SettlementRepairRejected,
    apply_settlement_repair,
    preview_settlement_repair,
)
from services.simulation_ledger import get_ledger_integrity
from tests.postgres_test_db import build_postgres_session_factory
from tests.settlement_test_support import load_seeded_rows, seed_shadow_order


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


async def _seed_legacy_order(
    session_factory,
    *,
    account_id: str = "legacy-account",
    order_id: str = "legacy-order",
    persist_identity: bool = True,
) -> tuple[str, str, str, str]:
    now = datetime.now(timezone.utc)
    condition_id = f"0x{hashlib.sha256(f'condition-{order_id}'.encode()).hexdigest()}"
    provider_market_id = f"market-{order_id}"
    token_seed = int(hashlib.sha256(f"token-{order_id}".encode()).hexdigest(), 16)
    held_token = str(token_seed)
    losing_token = str(token_seed + 1)
    trade_id = f"trade-{order_id}"
    position_id = f"position-{order_id}"
    resolution_id = f"resolution-{order_id}"

    async with session_factory() as session:
        session.add(
            SimulationAccount(
                id=account_id,
                name="Historical aggregate account",
                initial_capital=1_000.0,
                current_capital=900.0,
                total_pnl=0.0,
                total_trades=1,
                ledger_version=1,
                ledger_integrity_status="legacy",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SimulationTrade(
                id=trade_id,
                account_id=account_id,
                opportunity_id=f"opportunity-{order_id}",
                strategy_type="historical_repair_test",
                positions_data=[{"outcome": "YES", "token_id": held_token}],
                total_cost=100.0,
                expected_profit=147.0,
                status=TradeStatus.OPEN,
                executed_at=now,
            )
        )
        session.add(
            SimulationPosition(
                id=position_id,
                account_id=account_id,
                opportunity_id=f"opportunity-{order_id}",
                market_id=provider_market_id,
                market_question="Will the historical repair test pass?",
                token_id=held_token,
                side=PositionSide.YES,
                quantity=250.0,
                entry_price=0.4,
                entry_cost=100.0,
                current_price=0.4,
                status=TradeStatus.OPEN,
                opened_at=now,
            )
        )
        session.add(
            OnlineMarketResolution(
                id=resolution_id,
                venue="polymarket",
                provider="gamma",
                provider_market_id=provider_market_id,
                condition_id=condition_id,
                token_ids_json=[held_token, losing_token],
                outcomes_json=["YES", "NO"],
                outcome_prices_json=["1", "0"],
                state="final",
                winning_token_id=held_token,
                winning_outcome_index=0,
                winning_outcome="YES",
                provider_resolved_at=now,
                first_observed_at=now,
                last_observed_at=now,
                finalized_at=now,
                fact_version=1,
                evidence_hash=_hash(condition_id, "final", held_token),
                evidence_json={
                    "provider": "gamma",
                    "condition_id": condition_id,
                    "state": "final",
                    "winning_token_id": held_token,
                },
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TraderOrder(
                id=order_id,
                trader_id=f"trader-{account_id}",
                signal_id=f"signal-{order_id}",
                source="scanner",
                strategy_key="historical_repair_test",
                market_id=provider_market_id,
                venue="polymarket" if persist_identity else None,
                provider_market_id=provider_market_id if persist_identity else None,
                condition_id=condition_id if persist_identity else None,
                token_id=held_token if persist_identity else None,
                outcome_index=0 if persist_identity else None,
                identity_status="complete" if persist_identity else None,
                market_question="Will the historical repair test pass?",
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
                        "token_ids": [held_token, losing_token],
                        "outcomes": ["YES", "NO"],
                    },
                    "simulation_ledger": {
                        "account_id": account_id,
                        "trade_id": trade_id,
                        "position_id": position_id,
                    },
                },
                created_at=now,
                executed_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return order_id, trade_id, position_id, resolution_id


@pytest.mark.asyncio
async def test_preview_is_read_only_and_returns_stable_economic_certificate():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_preview")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="preview-account",
            order_id="preview-order",
            condition_id="preview-condition",
            provider_market_id="preview-market",
        )

        async with session_factory() as session:
            before_settlements = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))
            before_cash = await session.scalar(select(func.count()).select_from(SimulationCashLedgerEntry))
            preview = await preview_settlement_repair(
                session,
                account_id=seeded.account_id,
                order_ids=[seeded.order_id],
            )
            discovered_preview = await preview_settlement_repair(
                session,
                account_id=seeded.account_id,
                order_ids=None,
            )

        assert preview["counts"] == {"safe": 1, "ambiguous": 0, "blocked": 0}
        assert preview["items"][0]["classification"] == "safe"
        assert preview["items"][0]["order_updated_at"]
        assert preview["items"][0]["resolution_evidence_hash"]
        assert preview["items"][0]["candidate_net_payout_usdc"] == "247.000000"
        assert preview["items"][0]["candidate_realized_pnl_usdc"] == "147.000000"
        assert preview["totals"] == {
            "candidate_net_payout_usdc": "247.000000",
            "candidate_realized_pnl_usdc": "147.000000",
        }
        assert len(preview["preview_digest"]) == 64
        assert discovered_preview["order_ids"] == [seeded.order_id]
        assert discovered_preview["preview_digest"] == preview["preview_digest"]

        async with session_factory() as session:
            account, order, trade, position, _ = await load_seeded_rows(session, seeded)
            assert await session.scalar(select(func.count()).select_from(TraderOrderSettlement)) == before_settlements
            assert await session.scalar(select(func.count()).select_from(SimulationCashLedgerEntry)) == before_cash
            assert account is not None and account.current_capital == pytest.approx(900.0)
            assert order is not None and order.status == "open" and order.actual_profit is None
            assert trade is not None and trade.status == TradeStatus.OPEN
            assert position is not None and position.status == TradeStatus.OPEN
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preview_uses_legacy_inferred_identity_without_persisting_it():
    engine, session_factory = await build_postgres_session_factory(
        Base,
        "settlement_repair_legacy_identity_preview",
    )
    try:
        order_id, _trade_id, _position_id, _resolution_id = await _seed_legacy_order(
            session_factory,
            account_id="legacy-preview-account",
            order_id="legacy-preview-order",
            persist_identity=False,
        )

        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id="legacy-preview-account",
                order_ids=[order_id],
            )

        item = preview["items"][0]
        assert preview["counts"] == {"safe": 1, "ambiguous": 0, "blocked": 0}
        assert item["classification"] == "safe"
        assert item["identity_status"] == "legacy_inferred"
        assert item["persisted_identity_status"] is None
        assert item["identity_would_update"] is True
        assert item["condition_id"].startswith("0x")
        assert item["token_id"].isdigit()
        assert item["outcome_index"] == 0
        assert item["candidate_net_payout_usdc"] == "247.000000"
        assert item["candidate_realized_pnl_usdc"] == "147.000000"

        async with session_factory() as session:
            order = await session.get(TraderOrder, order_id)
        assert order is not None
        assert order.identity_status is None
        assert order.venue is None
        assert order.condition_id is None
        assert order.token_id is None
        assert order.outcome_index is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preview_classifies_non_final_ambiguous_live_and_cross_account_without_network():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_classify")
    try:
        pending = await seed_shadow_order(
            session_factory,
            account_id="classify-account",
            order_id="pending-order",
            condition_id="pending-condition",
            provider_market_id="pending-market",
            resolution_state="closed_pending",
            winning_outcome_index=None,
        )
        ambiguous = await seed_shadow_order(
            session_factory,
            account_id="classify-account",
            order_id="ambiguous-order",
            condition_id="ambiguous-condition",
            provider_market_id="ambiguous-market",
            identity_status="ambiguous",
        )
        live = await seed_shadow_order(
            session_factory,
            account_id="classify-account",
            order_id="live-order",
            condition_id="live-condition",
            provider_market_id="live-market",
            mode="live",
        )
        other = await seed_shadow_order(
            session_factory,
            account_id="other-account",
            order_id="cross-account-order",
            condition_id="cross-condition",
            provider_market_id="cross-market",
        )
        missing = await seed_shadow_order(
            session_factory,
            account_id="classify-account",
            order_id="missing-fact-order",
            condition_id="missing-fact-condition",
            provider_market_id="missing-fact-market",
        )
        conflicted = await seed_shadow_order(
            session_factory,
            account_id="classify-account",
            order_id="conflicted-order",
            condition_id="conflicted-condition",
            provider_market_id="conflicted-market",
            resolution_state="conflicted",
        )
        async with session_factory() as session, session.begin():
            resolution = await session.get(OnlineMarketResolution, missing.resolution_id)
            assert resolution is not None
            await session.delete(resolution)

        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id="classify-account",
                order_ids=[
                    pending.order_id,
                    ambiguous.order_id,
                    live.order_id,
                    other.order_id,
                    missing.order_id,
                    conflicted.order_id,
                ],
            )

        by_id = {item["order_id"]: item for item in preview["items"]}
        assert by_id[pending.order_id]["classification"] == "blocked"
        assert by_id[pending.order_id]["reason_code"] == "resolution_not_final"
        assert by_id[ambiguous.order_id]["classification"] == "ambiguous"
        assert by_id[ambiguous.order_id]["reason_code"] == "identity_not_settlement_safe"
        assert by_id[live.order_id]["classification"] == "blocked"
        assert by_id[live.order_id]["reason_code"] == "unsupported_order_mode"
        assert by_id[other.order_id]["classification"] == "blocked"
        assert by_id[other.order_id]["reason_code"] == "order_account_mismatch"
        assert by_id[missing.order_id]["classification"] == "blocked"
        assert by_id[missing.order_id]["reason_code"] == "resolution_fact_missing"
        assert by_id[conflicted.order_id]["classification"] == "blocked"
        assert by_id[conflicted.order_id]["reason_code"] == "resolution_conflicted"
        assert preview["counts"] == {"safe": 0, "ambiguous": 1, "blocked": 5}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_apply_rejects_stale_digest_atomically_when_order_version_changes():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_stale")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="stale-account",
            order_id="stale-order",
            condition_id="stale-condition",
            provider_market_id="stale-market",
        )
        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id=seeded.account_id,
                order_ids=[seeded.order_id],
            )
        async with session_factory() as session, session.begin():
            order = await session.get(TraderOrder, seeded.order_id)
            assert order is not None
            order.reason = "state changed after preview"
            order.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)

        async with session_factory() as session:
            with pytest.raises(SettlementRepairConflict, match="preview_digest"):
                async with session.begin():
                    await apply_settlement_repair(
                        session,
                        account_id=seeded.account_id,
                        order_ids=[seeded.order_id],
                        preview_digest=preview["preview_digest"],
                        confirm=True,
                    )

        async with session_factory() as session:
            account, order, trade, position, _ = await load_seeded_rows(session, seeded)
            credits = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.entry_type == "settlement_credit")
            )
            assert credits == 0
            assert await session.scalar(select(func.count()).select_from(TraderOrderSettlement)) == 0
            assert account is not None and account.current_capital == pytest.approx(900.0)
            assert order is not None and order.status == "open"
            assert trade is not None and trade.status == TradeStatus.OPEN
            assert position is not None and position.status == TradeStatus.OPEN
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_apply_rejects_stale_digest_atomically_when_resolution_fact_changes():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_fact_stale")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="fact-stale-account",
            order_id="fact-stale-order",
            condition_id="fact-stale-condition",
            provider_market_id="fact-stale-market",
        )
        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id=seeded.account_id,
                order_ids=[seeded.order_id],
            )
        async with session_factory() as session, session.begin():
            resolution = await session.get(OnlineMarketResolution, seeded.resolution_id)
            assert resolution is not None
            resolution.fact_version = 2
            resolution.evidence_hash = _hash(seeded.condition_id, "final", "new-evidence")
            resolution.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)

        async with session_factory() as session:
            with pytest.raises(SettlementRepairConflict, match="preview_digest"):
                async with session.begin():
                    await apply_settlement_repair(
                        session,
                        account_id=seeded.account_id,
                        order_ids=[seeded.order_id],
                        preview_digest=preview["preview_digest"],
                        confirm=True,
                    )

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(TraderOrderSettlement)) == 0
            credits = await session.scalar(
                select(func.count())
                .select_from(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.entry_type == "settlement_credit")
            )
            assert credits == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_apply_requires_explicit_confirmation_and_exact_safe_order_ids():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_confirmation")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="confirm-account",
            order_id="confirm-order",
            condition_id="confirm-condition",
            provider_market_id="confirm-market",
        )
        other = await seed_shadow_order(
            session_factory,
            account_id="other-confirm-account",
            order_id="other-confirm-order",
            condition_id="other-confirm-condition",
            provider_market_id="other-confirm-market",
        )
        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id=seeded.account_id,
                order_ids=[seeded.order_id],
            )

        async with session_factory() as session:
            with pytest.raises(SettlementRepairRejected, match="confirm=true"):
                async with session.begin():
                    await apply_settlement_repair(
                        session,
                        account_id=seeded.account_id,
                        order_ids=[seeded.order_id],
                        preview_digest=preview["preview_digest"],
                        confirm=False,
                    )
        async with session_factory() as session:
            with pytest.raises(SettlementRepairConflict):
                async with session.begin():
                    await apply_settlement_repair(
                        session,
                        account_id=seeded.account_id,
                        order_ids=[seeded.order_id, other.order_id],
                        preview_digest=preview["preview_digest"],
                        confirm=True,
                    )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_apply_checkpoints_then_uses_coordinator_and_replays_idempotently():
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_legacy")
    try:
        order_id, _trade_id, _position_id, _resolution_id = await _seed_legacy_order(
            session_factory,
            persist_identity=False,
        )
        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id="legacy-account",
                order_ids=[order_id],
            )
        assert preview["account"]["ledger_integrity_status"] == "legacy"
        assert preview["items"][0]["classification"] == "safe"

        async with session_factory() as session, session.begin():
            first = await apply_settlement_repair(
                session,
                account_id="legacy-account",
                order_ids=[order_id],
                preview_digest=preview["preview_digest"],
                confirm=True,
            )
        assert first["applied_count"] == 1
        assert first["idempotent_replay"] is False

        async with session_factory() as session, session.begin():
            replay = await apply_settlement_repair(
                session,
                account_id="legacy-account",
                order_ids=[order_id],
                preview_digest=preview["preview_digest"],
                confirm=True,
            )
        assert replay["applied_count"] == 0
        assert replay["idempotent_replay"] is True
        assert replay["results"][0]["settlement_id"] == first["results"][0]["settlement_id"]

        async with session_factory() as session:
            account = await session.get(SimulationAccount, "legacy-account")
            order = await session.get(TraderOrder, order_id)
            entries = (
                (
                    await session.execute(
                        select(SimulationCashLedgerEntry)
                        .where(SimulationCashLedgerEntry.account_id == "legacy-account")
                        .order_by(SimulationCashLedgerEntry.ledger_sequence)
                    )
                )
                .scalars()
                .all()
            )
            integrity = await get_ledger_integrity(session, "legacy-account")

        assert account is not None
        assert account.ledger_version == 2
        assert account.ledger_integrity_status == "checkpointed"
        assert account.current_capital == pytest.approx(1_147.0)
        assert account.total_pnl == pytest.approx(147.0)
        assert order is not None and order.actual_profit == pytest.approx(147.0)
        assert order.identity_status == "legacy_inferred"
        assert order.venue == "polymarket"
        assert order.condition_id and order.condition_id.startswith("0x")
        assert order.token_id and order.token_id.isdigit()
        assert order.outcome_index == 0
        assert [entry.entry_type for entry in entries] == ["manual_adjustment", "settlement_credit"]
        assert [entry.amount_usdc for entry in entries] == [Decimal("-100.000000"), Decimal("247.000000")]
        assert integrity.status == "checkpointed"
        assert integrity.difference_usdc == Decimal("0.000000")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_identity_checkpoint_and_settlement_roll_back_together(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(
        Base,
        "settlement_repair_legacy_identity_rollback",
    )
    try:
        order_id, _trade_id, _position_id, _resolution_id = await _seed_legacy_order(
            session_factory,
            account_id="legacy-rollback-account",
            order_id="legacy-rollback-order",
            persist_identity=False,
        )
        async with session_factory() as session:
            preview = await preview_settlement_repair(
                session,
                account_id="legacy-rollback-account",
                order_ids=[order_id],
            )

        async def fail_after_identity_and_checkpoint(
            session,
            *,
            trader_order_id,
            online_market_resolution_id,
        ):
            del online_market_resolution_id
            order = await session.get(TraderOrder, trader_order_id)
            account = await session.get(SimulationAccount, "legacy-rollback-account")
            assert order is not None and order.identity_status == "legacy_inferred"
            assert account is not None and account.ledger_integrity_status == "checkpointed"
            raise SettlementCoordinatorError("injected coordinator failure")

        monkeypatch.setattr(
            settlement_repair_module,
            "apply_shadow_market_resolution",
            fail_after_identity_and_checkpoint,
        )
        async with session_factory() as session:
            with pytest.raises(SettlementRepairConflict, match="injected coordinator failure"):
                async with session.begin():
                    await apply_settlement_repair(
                        session,
                        account_id="legacy-rollback-account",
                        order_ids=[order_id],
                        preview_digest=preview["preview_digest"],
                        confirm=True,
                    )

        async with session_factory() as session:
            order = await session.get(TraderOrder, order_id)
            account = await session.get(SimulationAccount, "legacy-rollback-account")
            settlement_count = await session.scalar(select(func.count()).select_from(TraderOrderSettlement))
            cash_count = await session.scalar(select(func.count()).select_from(SimulationCashLedgerEntry))

        assert order is not None
        assert order.identity_status is None
        assert order.venue is None
        assert order.condition_id is None
        assert order.token_id is None
        assert order.outcome_index is None
        assert account is not None
        assert account.ledger_version == 1
        assert account.ledger_integrity_status == "legacy"
        assert account.current_capital == pytest.approx(900.0)
        assert settlement_count == 0
        assert cash_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_routes_enforce_disabled_apply_map_digest_conflict_and_expose_integrity(monkeypatch):
    engine, session_factory = await build_postgres_session_factory(Base, "settlement_repair_routes")
    try:
        seeded = await seed_shadow_order(
            session_factory,
            account_id="route-account",
            order_id="route-order",
            condition_id="route-condition",
            provider_market_id="route-market",
        )
        request = routes_simulation.SettlementRepairPreviewRequest(order_ids=[seeded.order_id])
        async with session_factory() as session:
            preview = await routes_simulation.preview_account_settlement_repair(
                seeded.account_id,
                request,
                session,
            )

        apply_request = routes_simulation.SettlementRepairApplyRequest(
            order_ids=[seeded.order_id],
            preview_digest=preview["preview_digest"],
            confirm=True,
        )
        monkeypatch.setattr(
            routes_simulation.settings,
            "HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED",
            False,
        )
        async with session_factory() as session:
            with pytest.raises(HTTPException) as disabled:
                await routes_simulation.apply_account_settlement_repair(
                    seeded.account_id,
                    apply_request,
                    session,
                )
        assert disabled.value.status_code == 403
        assert disabled.value.detail["code"] == "settlement_repair_apply_disabled"

        monkeypatch.setattr(
            routes_simulation.settings,
            "HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED",
            True,
        )
        async with session_factory() as session, session.begin():
            order = await session.get(TraderOrder, seeded.order_id)
            assert order is not None
            order.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        async with session_factory() as session:
            with pytest.raises(HTTPException) as stale:
                await routes_simulation.apply_account_settlement_repair(
                    seeded.account_id,
                    apply_request,
                    session,
                )
        assert stale.value.status_code == 409
        assert stale.value.detail["code"] == "settlement_repair_preview_stale"

        async with session_factory() as session:
            integrity = await routes_simulation.get_account_ledger_integrity(
                seeded.account_id,
                session,
            )
        assert integrity["ledger_version"] == 2
        assert integrity["coverage"] == "full"
        assert integrity["journal_balance_usdc"] == "900.000000"
        assert integrity["projected_balance_usdc"] == "900.000000"
        assert integrity["difference_usdc"] == "0.000000"
    finally:
        await engine.dispose()
