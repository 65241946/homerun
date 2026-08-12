"""Atomic settlement coordinator for proof-grade HOMERUN Shadow v2 orders.

The coordinator consumes only persisted ``OnlineMarketResolution`` facts.  It
does not perform HTTP calls, strategy exits, reverse entries, or commits.  The
caller owns the surrounding short database transaction.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    OnlineMarketResolution,
    SimulationAccount,
    SimulationPosition,
    SimulationTrade,
    TraderOrder,
    TraderOrderSettlement,
    TradeStatus,
)
from services.simulation import SimulationService
from services.simulation_ledger import (
    ShadowCloseEconomics,
    calculate_shadow_close_economics,
    quantize_usdc,
)

SETTLEMENT_KIND = "market_resolution"
SAFE_IDENTITY_STATUSES = frozenset({"complete", "legacy_inferred"})
ACTIVE_SHADOW_ORDER_STATUSES = frozenset({"submitted", "executed", "completed", "open"})
TERMINAL_SETTLEMENT_STATUSES = frozenset({"applied", "projected", "verified"})


class SettlementCoordinatorError(RuntimeError):
    """Base error for coordinator integrity failures."""


class SettlementProjectionMismatch(SettlementCoordinatorError):
    """Raised when the simulation adapter disagrees with canonical economics."""


class SettlementIdempotencyConflict(SettlementCoordinatorError):
    """Raised when a terminal settlement is replayed with different evidence."""


FaultInjector = Callable[[str], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class SettlementApplyResult:
    settlement_id: str
    trader_order_id: str
    status: str
    applied: bool
    idempotent_replay: bool
    reason_code: str | None
    reason_detail: str | None
    net_payout_usdc: Decimal | None
    realized_pnl_usdc: Decimal | None


@dataclass(frozen=True, slots=True)
class ShadowSettlementInspection:
    """Read-only coordinator verdict used by repair previews and apply."""

    eligible: bool
    reason_code: str | None
    reason_detail: str | None
    account_id: str | None
    trade_id: str | None
    position_id: str | None
    economics: ShadowCloseEconomics | None


@dataclass(frozen=True, slots=True)
class _LedgerReferences:
    account_id: str
    trade_id: str
    position_id: str


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}\x1f{value}".encode()).hexdigest()


def _idempotency_key(order_id: str) -> str:
    return f"shadow-market-resolution:{order_id}"


def _settlement_id(order_id: str) -> str:
    return _stable_id("trader-order-settlement-v1", _idempotency_key(order_id))


def _reason_payload(code: str, detail: str) -> str:
    return json.dumps(
        {"code": str(code), "detail": str(detail)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_ledger_references(order: TraderOrder) -> _LedgerReferences | None:
    payload = order.payload_json if isinstance(order.payload_json, Mapping) else {}
    raw = payload.get("simulation_ledger")
    if not isinstance(raw, Mapping):
        return None
    account_id = str(raw.get("account_id") or "").strip()
    trade_id = str(raw.get("trade_id") or "").strip()
    position_id = str(raw.get("position_id") or "").strip()
    if not account_id or not trade_id or not position_id:
        return None
    return _LedgerReferences(account_id, trade_id, position_id)


def _resolution_block_reason(
    order: TraderOrder,
    resolution: OnlineMarketResolution,
) -> tuple[str, str] | None:
    state = str(resolution.state or "").strip().lower()
    if state == "conflicted":
        return "resolution_conflicted", "The persisted resolution fact is conflicted."
    if state != "final":
        return "resolution_not_final", f"Resolution state is {state or 'missing'}, not final."

    token_ids = [str(value or "").strip() for value in (resolution.token_ids_json or [])]
    winning_token = str(resolution.winning_token_id or "").strip()
    if not winning_token or winning_token not in token_ids:
        return "resolution_winner_incomplete", "Final resolution has no aligned winning token."

    mode = str(order.mode or "").strip().lower()
    if mode != "shadow":
        return "unsupported_order_mode", f"Order mode {mode or 'missing'} is not Shadow."

    identity_status = str(order.identity_status or "").strip().lower()
    if identity_status not in SAFE_IDENTITY_STATUSES:
        return (
            "identity_not_settlement_safe",
            f"Order identity status {identity_status or 'missing'} is not settlement-safe.",
        )

    if str(order.venue or "").strip().lower() != str(resolution.venue or "").strip().lower():
        return "identity_venue_mismatch", "Order venue does not match the resolution fact."
    if str(order.condition_id or "").strip() != str(resolution.condition_id or "").strip():
        return "identity_condition_mismatch", "Order condition ID does not match the resolution fact."

    order_provider_market = str(order.provider_market_id or "").strip()
    resolution_provider_market = str(resolution.provider_market_id or "").strip()
    if (
        order_provider_market
        and resolution_provider_market
        and order_provider_market != resolution_provider_market
    ):
        return (
            "identity_provider_market_mismatch",
            "Order provider market ID does not match the resolution fact.",
        )

    held_token = str(order.token_id or "").strip()
    if not held_token or held_token not in token_ids:
        return "identity_token_mismatch", "Order held token is not aligned with the resolution fact."
    try:
        expected_index = token_ids.index(held_token)
    except ValueError:
        return "identity_token_mismatch", "Order held token is not aligned with the resolution fact."
    if order.outcome_index is None or int(order.outcome_index) != expected_index:
        return "identity_outcome_index_mismatch", "Order outcome index does not match its held token."
    return None


def shadow_resolution_block_reason(
    order: TraderOrder,
    resolution: OnlineMarketResolution,
) -> tuple[str, str] | None:
    """Public read-only identity/finality gate shared by repair previews."""

    return _resolution_block_reason(order, resolution)


async def _inject(fault_injector: FaultInjector | None, stage: str) -> None:
    if fault_injector is None:
        return
    result = fault_injector(stage)
    if inspect.isawaitable(result):
        await result


async def _lock_order(session: AsyncSession, order_id: str) -> TraderOrder | None:
    return (
        await session.execute(
            select(TraderOrder)
            .where(TraderOrder.id == str(order_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _load_existing_settlement(
    session: AsyncSession,
    order_id: str,
) -> TraderOrderSettlement | None:
    return (
        await session.execute(
            select(TraderOrderSettlement)
            .where(
                TraderOrderSettlement.trader_order_id == str(order_id),
                TraderOrderSettlement.settlement_kind == SETTLEMENT_KIND,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


def _result_from_settlement(
    settlement: TraderOrderSettlement,
    *,
    applied: bool,
    idempotent_replay: bool,
    reason_code: str | None = None,
    reason_detail: str | None = None,
) -> SettlementApplyResult:
    return SettlementApplyResult(
        settlement_id=str(settlement.id),
        trader_order_id=str(settlement.trader_order_id),
        status=str(settlement.status),
        applied=applied,
        idempotent_replay=idempotent_replay,
        reason_code=reason_code,
        reason_detail=reason_detail,
        net_payout_usdc=(
            quantize_usdc(settlement.net_payout_usdc)
            if settlement.net_payout_usdc is not None
            else None
        ),
        realized_pnl_usdc=(
            quantize_usdc(settlement.realized_pnl_usdc)
            if settlement.realized_pnl_usdc is not None
            else None
        ),
    )


def _assert_terminal_replay_matches(
    settlement: TraderOrderSettlement,
    *,
    order: TraderOrder,
    resolution: OnlineMarketResolution,
) -> None:
    if str(resolution.state or "").strip().lower() != "final":
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} cannot be replayed from a non-final resolution fact"
        )
    if str(settlement.online_market_resolution_id or "") != str(resolution.id):
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} was replayed with a different resolution fact"
        )
    if str(settlement.mode or "").strip().lower() != str(order.mode or "").strip().lower():
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} mode differs from the current order"
        )
    if str(settlement.held_token_id or "").strip() != str(order.token_id or "").strip():
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} held token differs from the current order"
        )
    if str(settlement.winning_token_id or "").strip() != str(
        resolution.winning_token_id or ""
    ).strip():
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} winner differs from the current resolution fact"
        )
    if str(settlement.evidence_hash or "") != str(resolution.evidence_hash or ""):
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} evidence hash differs from the current resolution fact"
        )
    if int(settlement.resolution_fact_version or 0) != int(resolution.fact_version or 0):
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} fact version differs from the current resolution fact"
        )
    if settlement.net_payout_usdc is None or settlement.realized_pnl_usdc is None:
        raise SettlementIdempotencyConflict(
            f"Terminal settlement {settlement.id} has incomplete economic fields"
        )


async def _record_manual_review(
    session: AsyncSession,
    *,
    order: TraderOrder,
    resolution: OnlineMarketResolution,
    reason_code: str,
    reason_detail: str,
    existing: TraderOrderSettlement | None = None,
) -> SettlementApplyResult:
    settlement = existing or await _load_existing_settlement(session, str(order.id))
    if settlement is not None and str(settlement.status) in TERMINAL_SETTLEMENT_STATUSES:
        _assert_terminal_replay_matches(
            settlement,
            order=order,
            resolution=resolution,
        )
        return _result_from_settlement(
            settlement,
            applied=False,
            idempotent_replay=True,
        )

    now = _now_utc()
    if settlement is None:
        settlement = TraderOrderSettlement(
            id=_settlement_id(str(order.id)),
            trader_order_id=str(order.id),
            settlement_kind=SETTLEMENT_KIND,
            mode=str(order.mode or ""),
            online_market_resolution_id=str(resolution.id),
            resolution_fact_version=int(resolution.fact_version or 1),
            held_token_id=str(order.token_id or "").strip() or None,
            winning_token_id=str(resolution.winning_token_id or "").strip() or None,
            status="manual_review",
            authority="gamma_final",
            evidence_hash=str(resolution.evidence_hash or ""),
            idempotency_key=_idempotency_key(str(order.id)),
            attempt_count=1,
            last_error=_reason_payload(reason_code, reason_detail),
            detected_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(settlement)
    else:
        settlement.status = "manual_review"
        settlement.attempt_count = int(settlement.attempt_count or 0) + 1
        settlement.last_error = _reason_payload(reason_code, reason_detail)
        settlement.online_market_resolution_id = str(resolution.id)
        settlement.resolution_fact_version = int(resolution.fact_version or 1)
        settlement.evidence_hash = str(resolution.evidence_hash or "")
        settlement.updated_at = now
    await session.flush()
    return _result_from_settlement(
        settlement,
        applied=False,
        idempotent_replay=False,
        reason_code=reason_code,
        reason_detail=reason_detail,
    )


def _economics_for_resolution(
    *,
    position: SimulationPosition,
    resolution: OnlineMarketResolution,
) -> ShadowCloseEconomics:
    held_token = str(position.token_id or "").strip()
    winning_token = str(resolution.winning_token_id or "").strip()
    close_price = Decimal(1) if held_token == winning_token else Decimal(0)
    return calculate_shadow_close_economics(
        quantity=position.quantity or 0,
        entry_cost_usdc=position.entry_cost or 0,
        close_price=close_price,
        explicit_close_fee_usdc=0,
        winner_fee_rate=SimulationService.POLYMARKET_FEE,
    )


def _validate_locked_economic_rows(
    *,
    account: SimulationAccount,
    trade: SimulationTrade,
    position: SimulationPosition,
    order: TraderOrder,
    references: _LedgerReferences,
    allow_legacy_account: bool = False,
) -> tuple[str, str] | None:
    version = int(account.ledger_version or 1)
    integrity = str(account.ledger_integrity_status or "").strip().lower()
    if version < 2:
        if not allow_legacy_account or integrity != "legacy":
            return "ledger_not_v2", "Simulation account is not a repairable legacy or v2 cash-journal account."
    elif integrity not in {"complete", "checkpointed"}:
        return "ledger_integrity_blocked", f"Simulation ledger integrity is {integrity or 'missing'}."
    if str(account.id) != references.account_id:
        return "simulation_account_reference_mismatch", "Order references a different simulation account."
    if str(trade.account_id) != references.account_id:
        return "simulation_trade_account_mismatch", "Simulation trade belongs to another account."
    if str(position.account_id) != references.account_id:
        return "simulation_position_account_mismatch", "Simulation position belongs to another account."
    if str(order.status or "").strip().lower() not in ACTIVE_SHADOW_ORDER_STATUSES:
        return "order_not_active", f"Order status {order.status!s} is not active."
    if trade.status != TradeStatus.OPEN:
        return "simulation_trade_not_open", f"Simulation trade status is {trade.status.value}."
    if position.status != TradeStatus.OPEN:
        return "simulation_position_not_open", f"Simulation position status is {position.status.value}."
    if str(position.token_id or "").strip() != str(order.token_id or "").strip():
        return "simulation_position_token_mismatch", "Simulation position token differs from the order token."
    if Decimal(str(position.quantity or 0)) <= 0:
        return "simulation_quantity_invalid", "Simulation position quantity must be positive."
    if quantize_usdc(position.entry_cost or 0) <= 0:
        return "simulation_cost_basis_invalid", "Simulation position cost basis must be positive."
    return None


def inspect_shadow_market_resolution(
    *,
    account: SimulationAccount,
    trade: SimulationTrade,
    position: SimulationPosition,
    order: TraderOrder,
    resolution: OnlineMarketResolution,
    allow_legacy_account: bool = False,
) -> ShadowSettlementInspection:
    """Evaluate one persisted fact without creating settlement or journal rows."""

    references = _parse_ledger_references(order)
    if references is None:
        return ShadowSettlementInspection(
            False,
            "simulation_ledger_reference_missing",
            "Order payload has no complete simulation account/trade/position references.",
            None,
            None,
            None,
            None,
        )
    block = _resolution_block_reason(order, resolution)
    if block is None:
        block = _validate_locked_economic_rows(
            account=account,
            trade=trade,
            position=position,
            order=order,
            references=references,
            allow_legacy_account=allow_legacy_account,
        )
    if block is not None:
        return ShadowSettlementInspection(
            False,
            block[0],
            block[1],
            references.account_id,
            references.trade_id,
            references.position_id,
            None,
        )
    return ShadowSettlementInspection(
        True,
        None,
        None,
        references.account_id,
        references.trade_id,
        references.position_id,
        _economics_for_resolution(position=position, resolution=resolution),
    )


async def apply_shadow_market_resolution(
    session: AsyncSession,
    *,
    trader_order_id: str,
    online_market_resolution_id: str,
    fault_injector: FaultInjector | None = None,
) -> SettlementApplyResult:
    """Apply one persisted market-final fact to one Shadow v2 order.

    No commit or rollback is performed here.  Any raised exception must escape
    the caller's transaction so intent, journal, projections, and terminal
    states roll back together.
    """

    resolution = (
        await session.execute(
            select(OnlineMarketResolution)
            .where(OnlineMarketResolution.id == str(online_market_resolution_id))
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if resolution is None:
        raise ValueError(f"Online market resolution not found: {online_market_resolution_id}")

    pre_order = await session.get(TraderOrder, str(trader_order_id))
    if pre_order is None:
        raise ValueError(f"Trader order not found: {trader_order_id}")

    early_block = _resolution_block_reason(pre_order, resolution)
    references = _parse_ledger_references(pre_order)
    if early_block is not None or references is None:
        locked_order = await _lock_order(session, str(trader_order_id))
        if locked_order is None:
            raise ValueError(f"Trader order not found: {trader_order_id}")
        refreshed_block = _resolution_block_reason(locked_order, resolution)
        refreshed_references = _parse_ledger_references(locked_order)
        if refreshed_block is None and refreshed_references is not None:
            references = refreshed_references
        else:
            code, detail = refreshed_block or (
                "simulation_ledger_reference_missing",
                "Order payload has no complete simulation account/trade/position references.",
            )
            return await _record_manual_review(
                session,
                order=locked_order,
                resolution=resolution,
                reason_code=code,
                reason_detail=detail,
            )

    assert references is not None
    account = (
        await session.execute(
            select(SimulationAccount)
            .where(SimulationAccount.id == references.account_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    trade = (
        await session.execute(
            select(SimulationTrade)
            .where(SimulationTrade.id == references.trade_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    position = (
        await session.execute(
            select(SimulationPosition)
            .where(SimulationPosition.id == references.position_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    order = await _lock_order(session, str(trader_order_id))
    if order is None:
        raise ValueError(f"Trader order not found: {trader_order_id}")

    refreshed_references = _parse_ledger_references(order)
    if refreshed_references != references:
        return await _record_manual_review(
            session,
            order=order,
            resolution=resolution,
            reason_code="simulation_ledger_reference_changed",
            reason_detail="Simulation ledger references changed while the settlement was being claimed.",
        )

    existing = await _load_existing_settlement(session, str(order.id))
    if existing is not None and str(existing.status) in TERMINAL_SETTLEMENT_STATUSES:
        _assert_terminal_replay_matches(
            existing,
            order=order,
            resolution=resolution,
        )
        return _result_from_settlement(
            existing,
            applied=False,
            idempotent_replay=True,
        )

    block = _resolution_block_reason(order, resolution)
    if block is not None:
        return await _record_manual_review(
            session,
            order=order,
            resolution=resolution,
            reason_code=block[0],
            reason_detail=block[1],
            existing=existing,
        )
    if account is None or trade is None or position is None:
        missing = [
            name
            for name, value in (("account", account), ("trade", trade), ("position", position))
            if value is None
        ]
        return await _record_manual_review(
            session,
            order=order,
            resolution=resolution,
            reason_code="simulation_ledger_row_missing",
            reason_detail=f"Referenced simulation rows are missing: {', '.join(missing)}.",
            existing=existing,
        )

    row_block = _validate_locked_economic_rows(
        account=account,
        trade=trade,
        position=position,
        order=order,
        references=references,
    )
    if row_block is not None:
        return await _record_manual_review(
            session,
            order=order,
            resolution=resolution,
            reason_code=row_block[0],
            reason_detail=row_block[1],
            existing=existing,
        )

    economics = _economics_for_resolution(position=position, resolution=resolution)
    now = _now_utc()
    if existing is None:
        settlement = TraderOrderSettlement(
            id=_settlement_id(str(order.id)),
            trader_order_id=str(order.id),
            settlement_kind=SETTLEMENT_KIND,
            mode="shadow",
            simulation_account_id=references.account_id,
            simulation_trade_id=references.trade_id,
            simulation_position_id=references.position_id,
            online_market_resolution_id=str(resolution.id),
            resolution_fact_version=int(resolution.fact_version or 1),
            held_token_id=str(order.token_id),
            winning_token_id=str(resolution.winning_token_id),
            quantity=economics.quantity,
            cost_basis_usdc=economics.cost_basis_usdc,
            gross_payout_usdc=economics.gross_payout_usdc,
            fee_usdc=economics.total_fee_usdc,
            net_payout_usdc=economics.net_payout_usdc,
            realized_pnl_usdc=economics.realized_pnl_usdc,
            status="detected",
            authority="gamma_final",
            evidence_hash=str(resolution.evidence_hash),
            idempotency_key=_idempotency_key(str(order.id)),
            attempt_count=1,
            last_error=None,
            detected_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(settlement)
    else:
        settlement = existing
        settlement.mode = "shadow"
        settlement.simulation_account_id = references.account_id
        settlement.simulation_trade_id = references.trade_id
        settlement.simulation_position_id = references.position_id
        settlement.online_market_resolution_id = str(resolution.id)
        settlement.resolution_fact_version = int(resolution.fact_version or 1)
        settlement.held_token_id = str(order.token_id)
        settlement.winning_token_id = str(resolution.winning_token_id)
        settlement.quantity = economics.quantity
        settlement.cost_basis_usdc = economics.cost_basis_usdc
        settlement.gross_payout_usdc = economics.gross_payout_usdc
        settlement.fee_usdc = economics.total_fee_usdc
        settlement.net_payout_usdc = economics.net_payout_usdc
        settlement.realized_pnl_usdc = economics.realized_pnl_usdc
        settlement.status = "detected"
        settlement.authority = "gamma_final"
        settlement.evidence_hash = str(resolution.evidence_hash)
        settlement.attempt_count = int(settlement.attempt_count or 0) + 1
        settlement.last_error = None
        settlement.updated_at = now

    await session.flush()
    await _inject(fault_injector, "after_settlement_intent")

    close_result = await SimulationService().close_orchestrator_shadow_fill(
        account_id=references.account_id,
        trade_id=references.trade_id,
        position_id=references.position_id,
        close_price=float(economics.close_price),
        close_trigger="resolution",
        price_source="gamma_final",
        reason=f"online_market_resolution:{resolution.id}",
        close_fee_usd=0.0,
        trader_order_id=str(order.id),
        trader_order_settlement_id=str(settlement.id),
        session=session,
        commit=False,
    )
    if not close_result.get("closed"):
        raise SettlementProjectionMismatch(
            f"Simulation close did not apply for open trade {trade.id}: {close_result!r}"
        )
    if (
        quantize_usdc(close_result.get("actual_payout", 0)) != economics.net_payout_usdc
        or quantize_usdc(close_result.get("actual_pnl", 0)) != economics.realized_pnl_usdc
    ):
        raise SettlementProjectionMismatch(
            "Simulation close economics differ from the settlement certificate"
        )
    await _inject(fault_injector, "after_cash_journal")

    terminal_status = "resolved_win" if economics.won else "resolved_loss"
    order.status = terminal_status
    order.actual_profit = float(economics.realized_pnl_usdc)
    order_payload = dict(order.payload_json) if isinstance(order.payload_json, Mapping) else {}
    prior_close = order_payload.get("position_close")
    close_payload = dict(prior_close) if isinstance(prior_close, Mapping) else {}
    close_payload.update(
        {
            "close_price": float(economics.close_price),
            "close_trigger": "resolution",
            "price_source": "gamma_final",
            "gross_payout_usd": f"{economics.gross_payout_usdc:.6f}",
            "close_fee_usd": f"{economics.total_fee_usdc:.6f}",
            "settlement_proceeds_usd": f"{economics.net_payout_usdc:.6f}",
            "realized_pnl": f"{economics.realized_pnl_usdc:.6f}",
            "settlement_id": str(settlement.id),
            "online_market_resolution_id": str(resolution.id),
            "resolution_fact_version": int(resolution.fact_version or 1),
            "resolution_evidence_hash": str(resolution.evidence_hash),
            "closed_at": now.isoformat().replace("+00:00", "Z"),
        }
    )
    order_payload["position_close"] = close_payload
    order.payload_json = order_payload
    order.updated_at = now

    settlement.status = "projected"
    settlement.applied_at = now
    settlement.projected_at = now
    settlement.last_error = None
    settlement.updated_at = now
    await _inject(fault_injector, "before_terminal_flush")
    await session.flush()
    return _result_from_settlement(
        settlement,
        applied=True,
        idempotent_replay=False,
    )


__all__ = [
    "ACTIVE_SHADOW_ORDER_STATUSES",
    "SAFE_IDENTITY_STATUSES",
    "SETTLEMENT_KIND",
    "SettlementApplyResult",
    "SettlementCoordinatorError",
    "SettlementIdempotencyConflict",
    "SettlementProjectionMismatch",
    "ShadowSettlementInspection",
    "apply_shadow_market_resolution",
    "inspect_shadow_market_resolution",
    "shadow_resolution_block_reason",
]
