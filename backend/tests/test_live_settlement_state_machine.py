from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from models.database import (
    Base,
    TraderOrder,
    TraderOrderSettlement,
    TraderOrderVerificationEvent,
)
from tests.postgres_test_db import build_postgres_session_factory
from tests.settlement_test_support import seed_shadow_order


async def _seed_live_order(
    session_factory,
    *,
    suffix: str,
    order_status: str = "resolved_win",
):
    seeded = await seed_shadow_order(
        session_factory,
        account_id=f"live-account-{suffix}",
        order_id=f"live-order-{suffix}",
        condition_id=f"0x{suffix.encode().hex():0<64}"[:66],
        provider_market_id=f"live-market-{suffix}",
        mode="live",
        order_status=order_status,
        include_simulation_refs=False,
    )
    async with session_factory() as session:
        order = await session.get(TraderOrder, seeded.order_id)
        assert order is not None
        payload = dict(order.payload_json or {})
        payload["token_id"] = seeded.held_token_id
        payload["live_fill"] = {
            "token_id": seeded.held_token_id,
            "filled_size": 250.0,
            "filled_notional": 100.0,
        }
        order.payload_json = payload
        await session.commit()
    return seeded


@pytest.mark.asyncio
async def test_gamma_final_creates_projected_live_settlement_without_realized_pnl():
    from services.live_settlement import project_live_market_final

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_market_final",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="marketfinal")

        async with session_factory() as session, session.begin():
            result = await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        assert result.changed is True
        assert result.live_state == "market_final"
        assert result.status == "projected"
        assert result.realized_pnl_usdc == Decimal("150.000000")

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )

        assert order is not None and order.actual_profit is None
        assert settlement is not None
        assert settlement.live_state == "market_final"
        assert settlement.status == "projected"
        assert settlement.cash_verified_at is None
        assert settlement.verified_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_redemption_states_are_monotonic_and_cash_requires_strong_evidence():
    from services.live_settlement import (
        LiveSettlementTransitionError,
        advance_live_settlement,
        project_live_market_final,
    )

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_state_graph",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="stategraph")
        async with session_factory() as session, session.begin():
            await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        async with session_factory() as session:
            with pytest.raises(LiveSettlementTransitionError, match="gamma_final"):
                async with session.begin():
                    await advance_live_settlement(
                        session,
                        trader_order_id=seeded.order_id,
                        target_state="cash_verified",
                        authority="gamma_final",
                        evidence={"provider": "gamma"},
                        net_payout_usdc=Decimal(250),
                        realized_pnl_usdc=Decimal(150),
                    )

        redeem_tx_hash = "0x" + "ab" * 32
        transitions = (
            (
                "claimable",
                "ctf_claimable",
                {
                    "condition_id": seeded.condition_id,
                    "block_number": 101,
                    "wallet_balance_shares": "250.000000",
                    "expected_payout_usdc": "250.000000",
                },
                None,
            ),
            (
                "redeem_submitted",
                "ctf_redeem_submission",
                {
                    "condition_id": seeded.condition_id,
                    "tx_hash": redeem_tx_hash,
                },
                redeem_tx_hash,
            ),
            (
                "redeem_confirmed",
                "ctf_redeem_receipt",
                {
                    "condition_id": seeded.condition_id,
                    "tx_hash": redeem_tx_hash,
                    "receipt_status": 1,
                    "block_number": 202,
                },
                redeem_tx_hash,
            ),
        )
        for state, authority, evidence, tx_hash in transitions:
            async with session_factory() as session, session.begin():
                result = await advance_live_settlement(
                    session,
                    trader_order_id=seeded.order_id,
                    target_state=state,
                    authority=authority,
                    evidence=evidence,
                    tx_hash=tx_hash,
                )
            assert result.changed is True
            assert result.live_state == state

        async with session_factory() as session, session.begin():
            verified = await advance_live_settlement(
                session,
                trader_order_id=seeded.order_id,
                target_state="cash_verified",
                authority="ctf_cash_balance",
                evidence={
                    "condition_id": seeded.condition_id,
                    "tx_hash": redeem_tx_hash,
                    "cash_received_usdc": "250.000000",
                    "balance_before_usdc": "100.000000",
                    "balance_after_usdc": "350.000000",
                },
                tx_hash=redeem_tx_hash,
                net_payout_usdc=Decimal("250.000000"),
                realized_pnl_usdc=Decimal("150.000000"),
            )

        assert verified.changed is True
        assert verified.live_state == "cash_verified"
        assert verified.status == "verified"

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )
        assert order is not None and order.actual_profit == pytest.approx(150.0)
        assert settlement is not None
        assert settlement.redeem_tx_hash == redeem_tx_hash
        assert settlement.redeem_confirmed_at is not None
        assert settlement.cash_verified_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bot_owned_sell_fill_cash_verification_is_idempotent_across_sessions():
    from services.live_settlement import project_live_market_final
    from services.polymarket_trade_verifier import verify_orders_from_bot_lineage

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_bot_lineage",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="botlineage")
        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            assert order is not None
            payload = dict(order.payload_json or {})
            payload["pending_live_exit"] = {
                "snapshot": {
                    "filled_size": 250.0,
                    "filled_notional_usd": 125.0,
                    "average_fill_price": 0.5,
                    "normalized_status": "filled",
                }
            }
            order.payload_json = payload
            await session.commit()

        async with session_factory() as session, session.begin():
            await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        async with session_factory() as session:
            rejected = await verify_orders_from_bot_lineage(
                session,
                order_ids=[seeded.order_id],
                commit=True,
            )
            assert rejected["verified_sell_fill"] == 0

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            assert order is not None
            payload = dict(order.payload_json or {})
            pending_exit = dict(payload["pending_live_exit"])
            snapshot = dict(pending_exit["snapshot"])
            snapshot["provider_clob_order_id"] = "clob-sell-owned-1"
            pending_exit["snapshot"] = snapshot
            payload["pending_live_exit"] = pending_exit
            order.payload_json = payload
            await session.commit()

        for _ in range(2):
            async with session_factory() as session:
                result = await verify_orders_from_bot_lineage(
                    session,
                    order_ids=[seeded.order_id],
                    commit=True,
                )
                assert result["verified_sell_fill"] == 1

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )
            cash_event_count = await session.scalar(
                select(func.count())
                .select_from(TraderOrderVerificationEvent)
                .where(
                    TraderOrderVerificationEvent.trader_order_id == seeded.order_id,
                    TraderOrderVerificationEvent.event_type
                    == "live_settlement_cash_verified",
                )
            )

        assert order is not None and order.actual_profit == pytest.approx(25.0)
        assert settlement is not None
        assert settlement.live_state == "cash_verified"
        assert settlement.status == "verified"
        assert settlement.realized_pnl_usdc == Decimal("25.000000")
        assert cash_event_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_market_metadata_resolution_never_upgrades_managed_live_cash_pnl(monkeypatch):
    from services import polymarket_trade_verifier as verifier
    from services.live_settlement import project_live_market_final

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_gamma_not_cash",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="gammanotcash")
        async with session_factory() as session, session.begin():
            await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        async def _resolved(rows):
            return {
                str(row.id): {
                    "resolved": True,
                    "winning_outcome": "yes",
                }
                for row in rows
            }

        monkeypatch.setattr(verifier, "_prefetch_market_info", _resolved)
        from services.live_execution_service import live_execution_service

        monkeypatch.setattr(
            live_execution_service,
            "get_execution_wallet_address",
            lambda: "",
        )

        async with session_factory() as session:
            result = await verifier.verify_orders_against_market_resolutions(
                session,
                order_ids=[seeded.order_id],
                commit=True,
            )
            assert result["verified"] == 0

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )
        assert order is not None and order.actual_profit is None
        assert settlement is not None and settlement.live_state == "market_final"
        assert settlement.status == "projected"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attributable_closed_position_upgrades_managed_live_cash(monkeypatch):
    from services import polymarket_trade_verifier as verifier
    from services.live_settlement import project_live_market_final

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_closed_position_cash",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="closedposition")
        async with session_factory() as session, session.begin():
            await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        monkeypatch.setattr(
            verifier.polymarket_client,
            "get_closed_positions_paginated",
            AsyncMock(
                return_value=[
                    {
                        "asset": seeded.held_token_id,
                        "conditionId": "0x" + "ff" * 32,
                        "outcomeIndex": 0,
                        "proxyWallet": "0x1111111111111111111111111111111111111111",
                        "curPrice": 1.0,
                        "timestamp": int(datetime.now(timezone.utc).timestamp()) + 5,
                        "realizedPnl": 150.0,
                        "totalBought": 250.0,
                    }
                ]
            ),
        )

        async with session_factory() as session:
            rejected = await verifier.verify_orders_against_closed_positions(
                session,
                wallet_address="0x1111111111111111111111111111111111111111",
                order_ids=[seeded.order_id],
                commit=True,
            )
            assert rejected["verified"] == 0

        verifier.polymarket_client.get_closed_positions_paginated.return_value = [
            {
                "asset": seeded.held_token_id,
                "conditionId": seeded.condition_id,
                "outcomeIndex": 0,
                "proxyWallet": "0x1111111111111111111111111111111111111111",
                "curPrice": 1.0,
                "timestamp": int(datetime.now(timezone.utc).timestamp()) + 5,
                "realizedPnl": 150.0,
                "totalBought": 250.0,
            }
        ]

        async with session_factory() as session:
            accepted = await verifier.verify_orders_against_closed_positions(
                session,
                wallet_address="0x1111111111111111111111111111111111111111",
                order_ids=[seeded.order_id],
                commit=True,
            )
            assert accepted["verified"] == 1
            assert accepted["managed_evidence_blocked"] == 0

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )
        assert order is not None and order.actual_profit == pytest.approx(150.0)
        assert settlement is not None
        assert settlement.live_state == "cash_verified"
        assert settlement.authority == "polymarket_closed_positions"
        assert settlement.net_payout_usdc == Decimal("250.000000")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_redeemer_summary_stops_at_confirmed_and_replays_once():
    from services.live_settlement import (
        apply_redeemer_state_updates,
        project_live_market_final,
    )

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_redeemer_writeback",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="redeemerwriteback")
        async with session_factory() as session, session.begin():
            await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        summary = {
            "dry_run": False,
            "condition_results": [
                {
                    "condition_id": seeded.condition_id,
                    "wallet_balance_shares": "250.000000",
                    "expected_payout_usdc": "250.000000",
                    "status": "executed",
                    "tx_hash": "0x" + "cd" * 32,
                    "receipt_status": 1,
                    "block_number": 987,
                }
            ],
        }
        first = await apply_redeemer_state_updates(
            summary,
            session_factory=session_factory,
        )
        second = await apply_redeemer_state_updates(
            summary,
            session_factory=session_factory,
        )

        assert first == {"matched": 1, "updated": 3, "errors": []}
        assert second == {"matched": 1, "updated": 0, "errors": []}

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )
            event_types = (
                await session.execute(
                    select(TraderOrderVerificationEvent.event_type)
                    .where(
                        TraderOrderVerificationEvent.trader_order_id
                        == seeded.order_id
                    )
                    .order_by(TraderOrderVerificationEvent.created_at.asc())
                )
            ).scalars().all()

        assert order is not None and order.actual_profit is None
        assert settlement is not None
        assert settlement.live_state == "redeem_confirmed"
        assert settlement.status == "projected"
        assert settlement.cash_verified_at is None
        assert event_types == [
            "live_settlement_market_final",
            "live_settlement_claimable",
            "live_settlement_redeem_submitted",
            "live_settlement_redeem_confirmed",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redeemer_dry_run_does_not_write_live_settlement_state(monkeypatch):
    from workers import redeemer_worker

    monkeypatch.setattr(
        redeemer_worker.live_execution_service,
        "ensure_initialized",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        redeemer_worker.ctf_execution_service,
        "redeem_resolved_wallet_positions",
        AsyncMock(
            return_value={
                "dry_run": True,
                "redeemed": 1,
                "condition_results": [
                    {
                        "condition_id": "0x" + "ab" * 32,
                        "claimable": True,
                        "status": "would_redeem",
                    }
                ],
            }
        ),
    )
    writeback = AsyncMock(return_value={"updated": 0})
    monkeypatch.setattr(
        redeemer_worker,
        "apply_redeemer_state_updates",
        writeback,
        raising=False,
    )

    result = await redeemer_worker._run_redeem_cycle(dry_run=True)

    assert result["dry_run"] is True
    writeback.assert_not_awaited()


@pytest.mark.asyncio
async def test_redeemer_real_cycle_reconciles_completed_condition_results(monkeypatch):
    from workers import redeemer_worker

    monkeypatch.setattr(
        redeemer_worker.live_execution_service,
        "ensure_initialized",
        AsyncMock(return_value=True),
    )
    summary = {
        "dry_run": False,
        "redeemed": 1,
        "failed": 0,
        "condition_results": [
            {
                "condition_id": "0x" + "ab" * 32,
                "wallet_balance_shares": 10.0,
                "expected_payout_usdc": 10.0,
                "status": "executed",
                "tx_hash": "0x" + "cd" * 32,
                "receipt_status": 1,
                "block_number": 123,
            }
        ],
    }
    redeem = AsyncMock(return_value=dict(summary))
    monkeypatch.setattr(
        redeemer_worker.ctf_execution_service,
        "redeem_resolved_wallet_positions",
        redeem,
    )
    writeback = AsyncMock(return_value={"matched": 1, "updated": 3, "errors": []})
    monkeypatch.setattr(redeemer_worker, "apply_redeemer_state_updates", writeback)

    result = await redeemer_worker._run_redeem_cycle(dry_run=False)

    writeback.assert_awaited_once()
    callback = redeem.await_args.kwargs["status_callback"]
    assert callback is redeemer_worker.record_redeemer_lifecycle_event
    assert result["status"] == "ok"
    assert result["settlement_writeback"]["updated"] == 3


@pytest.mark.asyncio
async def test_redeemer_missing_credentials_is_explicitly_not_configured(monkeypatch):
    from workers import redeemer_worker

    monkeypatch.setattr(
        redeemer_worker.live_execution_service,
        "ensure_initialized",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        redeemer_worker.live_execution_service,
        "get_last_init_error",
        lambda: "missing_polymarket_credentials",
    )

    result = await redeemer_worker._run_redeem_cycle(dry_run=False)

    assert result["status"] == "not_configured"
    assert result["errors"] == ["missing_polymarket_credentials"]


def test_trader_order_verification_mirror_failure_aborts_parent_write():
    from models.database import _mirror_trader_order_to_verification

    target = MagicMock()
    target.id = "order-mirror-failure"
    target.verification_status = "wallet_activity"
    target.actual_profit = 12.34
    target.execution_wallet_address = "0xabc"
    connection = MagicMock()
    connection.execute.side_effect = RuntimeError("mirror write failed")

    with pytest.raises(RuntimeError, match="mirror write failed"):
        _mirror_trader_order_to_verification(None, connection, target)


@pytest.mark.asyncio
async def test_cash_verification_rolls_back_when_authoritative_mirror_write_fails():
    from services.live_settlement import (
        advance_live_settlement,
        project_live_market_final,
    )

    engine, session_factory = await build_postgres_session_factory(
        Base,
        "live_mirror_atomicity",
    )
    try:
        seeded = await _seed_live_order(session_factory, suffix="mirroratomicity")
        async with session_factory() as session, session.begin():
            await project_live_market_final(
                session,
                trader_order_id=seeded.order_id,
                online_market_resolution_id=seeded.resolution_id,
            )

        async with session_factory() as session:
            await session.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION fail_live_verification_mirror()
                    RETURNS trigger AS $$
                    BEGIN
                        RAISE EXCEPTION 'injected mirror failure';
                    END;
                    $$ LANGUAGE plpgsql
                    """
                )
            )
            await session.execute(
                text(
                    """
                    CREATE TRIGGER fail_live_verification_mirror_trigger
                    BEFORE INSERT OR UPDATE ON trader_order_verification
                    FOR EACH ROW EXECUTE FUNCTION fail_live_verification_mirror()
                    """
                )
            )
            await session.commit()

        with pytest.raises(DBAPIError, match="injected mirror failure"):
            async with session_factory() as session:
                async with session.begin():
                    await advance_live_settlement(
                        session,
                        trader_order_id=seeded.order_id,
                        target_state="cash_verified",
                        authority="polymarket_closed_positions",
                        evidence={
                            "condition_id": seeded.condition_id,
                            "attributed_size": "250.000000",
                            "settled_price": "1.0",
                            "fee_usdc": "0",
                        },
                        net_payout_usdc=Decimal("250.000000"),
                        realized_pnl_usdc=Decimal("150.000000"),
                    )

        async with session_factory() as session:
            order = await session.get(TraderOrder, seeded.order_id)
            settlement = await session.scalar(
                select(TraderOrderSettlement).where(
                    TraderOrderSettlement.trader_order_id == seeded.order_id
                )
            )
            cash_event_count = await session.scalar(
                select(func.count())
                .select_from(TraderOrderVerificationEvent)
                .where(
                    TraderOrderVerificationEvent.trader_order_id == seeded.order_id,
                    TraderOrderVerificationEvent.event_type
                    == "live_settlement_cash_verified",
                )
            )

        assert order is not None and order.actual_profit is None
        assert settlement is not None and settlement.live_state == "market_final"
        assert settlement.status == "projected"
        assert cash_event_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ctf_sender_emits_submitted_before_confirmed_receipt(monkeypatch):
    from services.ctf_execution import CTFExecutionService

    class _Signed:
        raw_transaction = b"signed"

    class _TxHash:
        @staticmethod
        def hex():
            return "0x" + "ef" * 32

    class _Receipt:
        status = 1
        blockNumber = 456

    class _Account:
        @staticmethod
        def sign_transaction(_tx, _private_key):
            return _Signed()

    class _Eth:
        gas_price = 30_000_000_000
        account = _Account()

        @staticmethod
        def send_raw_transaction(_raw):
            return _TxHash()

        @staticmethod
        def wait_for_transaction_receipt(_tx_hash, _timeout):
            return _Receipt()

    class _W3:
        eth = _Eth()

    service = CTFExecutionService()
    monkeypatch.setattr(service, "_next_sender_nonce", AsyncMock(return_value=7))
    events: list[dict] = []

    async def _callback(event):
        events.append(dict(event))
        return {"errors": []}

    receipt_metadata: dict[str, int] = {}
    tx_hash = await service._send_eoa_call(
        w3=_W3(),
        from_address="0x1111111111111111111111111111111111111111",
        private_key="0x" + "11" * 32,
        to_address="0x2222222222222222222222222222222222222222",
        data=b"redeem",
        gas_limit=260_000,
        status_callback=_callback,
        audit_errors=[],
        receipt_metadata=receipt_metadata,
    )

    assert tx_hash == "0x" + "ef" * 32
    assert receipt_metadata == {"receipt_status": 1, "block_number": 456}
    assert [event["state"] for event in events] == [
        "redeem_submitted",
        "redeem_confirmed",
    ]
