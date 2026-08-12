"""Auditable Live settlement finality and cash-verification state machine.

This module is deliberately database-only.  It does not fetch market data,
sign transactions, submit orders, redeem positions, commit, or roll back.  A
caller supplies persisted market facts or attributed wallet/chain evidence and
owns the surrounding short transaction.

The central invariant is that ``market_final`` is only a projected economic
result.  ``TraderOrder.actual_profit`` is not written until independent cash
evidence advances the settlement to ``cash_verified``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    AsyncSessionLocal,
    OnlineMarketResolution,
    TraderOrder,
    TraderOrderSettlement,
    TraderOrderVerificationEvent,
)
from services.trader_order_verification import (
    TRADER_ORDER_VERIFICATION_WALLET_ACTIVITY,
    apply_trader_order_verification,
    normalize_trader_order_verification_status,
)

LIVE_SETTLEMENT_KIND = "market_resolution"
LIVE_STATES = (
    "market_final",
    "claimable",
    "redeem_submitted",
    "redeem_confirmed",
    "cash_verified",
)
_STATE_RANK = {state: index for index, state in enumerate(LIVE_STATES, start=1)}
_SAFE_IDENTITY_STATUSES = frozenset({"complete", "legacy_inferred"})
_DIRECT_CASH_AUTHORITIES = frozenset(
    {
        "bot_lineage_sell_fill",
        "polymarket_closed_positions",
    }
)
_REDEMPTION_CASH_AUTHORITIES = frozenset(
    {
        "ctf_cash_balance",
        "ctf_redeem_transfer",
    }
)
_AUTHORITY_BY_STATE = {
    "claimable": frozenset({"ctf_claimable"}),
    "redeem_submitted": frozenset({"ctf_redeem_submission"}),
    "redeem_confirmed": frozenset({"ctf_redeem_receipt"}),
    "cash_verified": _DIRECT_CASH_AUTHORITIES | _REDEMPTION_CASH_AUTHORITIES,
}
_ZERO = Decimal("0.000000")
_USDC_QUANTUM = Decimal("0.000001")


class LiveSettlementError(RuntimeError):
    """Base error for fail-closed Live settlement processing."""


class LiveSettlementIntegrityError(LiveSettlementError):
    """Raised when persisted identity/economics are not settlement-safe."""


class LiveSettlementTransitionError(LiveSettlementError):
    """Raised for an invalid state, authority, or evidence transition."""


class LiveSettlementEvidenceConflict(LiveSettlementError):
    """Raised when a terminal state is replayed with different economics."""


@dataclass(frozen=True, slots=True)
class LiveSettlementResult:
    settlement_id: str
    trader_order_id: str
    live_state: str
    status: str
    changed: bool
    idempotent_replay: bool
    net_payout_usdc: Decimal | None
    realized_pnl_usdc: Decimal | None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}\x1f{value}".encode()).hexdigest()


def _settlement_id(order_id: str) -> str:
    return _stable_id(
        "trader-order-live-settlement-v1",
        f"live-market-resolution:{order_id}",
    )


def _idempotency_key(order_id: str) -> str:
    return f"live-market-resolution:{order_id}"


def _decimal(value: Any, *, field: str, allow_zero: bool = True) -> Decimal:
    try:
        normalized = Decimal(str(value)).quantize(_USDC_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveSettlementIntegrityError(f"{field} is not a finite decimal") from exc
    if not normalized.is_finite():
        raise LiveSettlementIntegrityError(f"{field} is not a finite decimal")
    if normalized < _ZERO or (not allow_zero and normalized == _ZERO):
        qualifier = "positive" if not allow_zero else "non-negative"
        raise LiveSettlementIntegrityError(f"{field} must be {qualifier}")
    return normalized


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_evidence(
    *,
    state: str,
    authority: str,
    evidence: Mapping[str, Any],
    tx_hash: str | None = None,
    net_payout_usdc: Decimal | None = None,
    realized_pnl_usdc: Decimal | None = None,
) -> tuple[dict[str, Any], str]:
    payload = {
        "state": state,
        "authority": authority,
        "evidence": _json_safe(dict(evidence)),
        "tx_hash": str(tx_hash or "").strip() or None,
        "net_payout_usdc": (
            format(net_payout_usdc, "f") if net_payout_usdc is not None else None
        ),
        "realized_pnl_usdc": (
            format(realized_pnl_usdc, "f")
            if realized_pnl_usdc is not None
            else None
        ),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_tx_hash(value: Any, *, required: bool) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        if required:
            raise LiveSettlementTransitionError("a transaction hash is required")
        return None
    if not text.startswith("0x") or len(text) != 66:
        raise LiveSettlementTransitionError("transaction hash must be a 32-byte 0x hex value")
    try:
        int(text, 0)
    except ValueError as exc:
        raise LiveSettlementTransitionError("transaction hash contains non-hex characters") from exc
    return text


def _candidate_dicts(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    reconciliation = payload.get("provider_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, Mapping) else {}
    snapshot = reconciliation.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    provider_snapshot = payload.get("provider_snapshot")
    provider_snapshot = provider_snapshot if isinstance(provider_snapshot, Mapping) else {}
    live_fill = payload.get("live_fill")
    live_fill = live_fill if isinstance(live_fill, Mapping) else {}
    return [live_fill, reconciliation, snapshot, provider_snapshot, payload]


def _first_decimal(
    candidates: list[Mapping[str, Any]],
    keys: tuple[str, ...],
) -> Decimal | None:
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if parsed.is_finite():
                return parsed
    return None


def _entry_economics(order: TraderOrder) -> tuple[Decimal, Decimal]:
    payload = order.payload_json if isinstance(order.payload_json, Mapping) else {}
    candidates = _candidate_dicts(payload)
    quantity = _first_decimal(
        candidates,
        (
            "filled_size",
            "size_matched",
            "sizeMatched",
            "matched_size",
            "filled_shares",
            "executed_size",
        ),
    )
    cost = _first_decimal(
        candidates,
        (
            "filled_notional_usd",
            "filled_notional",
            "matched_notional",
            "matched_amount",
            "executed_notional",
        ),
    )
    average_price = _first_decimal(
        candidates,
        (
            "average_fill_price",
            "avg_fill_price",
            "avg_price",
            "avgFillPrice",
            "matched_price",
            "price",
        ),
    )
    if cost is None and quantity is not None and average_price is not None:
        cost = quantity * average_price
    if quantity is None and cost is not None and average_price is not None and average_price > 0:
        quantity = cost / average_price
    if quantity is None or quantity <= 0 or cost is None or cost <= 0:
        raise LiveSettlementIntegrityError(
            "live order has no attributable provider fill size and cost basis"
        )
    return (
        quantity.quantize(Decimal("0.000000000000000001")),
        cost.quantize(_USDC_QUANTUM),
    )


def _validate_live_identity(
    order: TraderOrder,
    resolution: OnlineMarketResolution,
) -> None:
    if str(order.mode or "").strip().lower() != "live":
        raise LiveSettlementIntegrityError("order mode is not live")
    if str(order.identity_status or "").strip().lower() not in _SAFE_IDENTITY_STATUSES:
        raise LiveSettlementIntegrityError("order identity is not settlement-safe")
    if str(resolution.state or "").strip().lower() != "final":
        raise LiveSettlementIntegrityError("resolution fact is not final")
    if str(order.venue or "").strip().lower() != str(resolution.venue or "").strip().lower():
        raise LiveSettlementIntegrityError("order venue does not match resolution")
    if str(order.condition_id or "").strip().lower() != str(
        resolution.condition_id or ""
    ).strip().lower():
        raise LiveSettlementIntegrityError("order condition does not match resolution")
    order_market = str(order.provider_market_id or "").strip()
    resolution_market = str(resolution.provider_market_id or "").strip()
    if order_market and resolution_market and order_market != resolution_market:
        raise LiveSettlementIntegrityError("provider market ID does not match resolution")
    tokens = [str(item or "").strip() for item in (resolution.token_ids_json or [])]
    held_token = str(order.token_id or "").strip()
    winning_token = str(resolution.winning_token_id or "").strip()
    if not held_token or held_token not in tokens:
        raise LiveSettlementIntegrityError("held token is not aligned to resolution outcomes")
    if not winning_token or winning_token not in tokens:
        raise LiveSettlementIntegrityError("resolution winner is incomplete")
    held_index = tokens.index(held_token)
    if order.outcome_index is None or int(order.outcome_index) != held_index:
        raise LiveSettlementIntegrityError("held token and outcome index disagree")


def _result(settlement: TraderOrderSettlement, *, changed: bool) -> LiveSettlementResult:
    return LiveSettlementResult(
        settlement_id=str(settlement.id),
        trader_order_id=str(settlement.trader_order_id),
        live_state=str(settlement.live_state or ""),
        status=str(settlement.status or ""),
        changed=changed,
        idempotent_replay=not changed,
        net_payout_usdc=(
            Decimal(settlement.net_payout_usdc)
            if settlement.net_payout_usdc is not None
            else None
        ),
        realized_pnl_usdc=(
            Decimal(settlement.realized_pnl_usdc)
            if settlement.realized_pnl_usdc is not None
            else None
        ),
    )


async def _append_state_event(
    session: AsyncSession,
    *,
    order: TraderOrder,
    settlement: TraderOrderSettlement,
    state: str,
    authority: str,
    evidence: Mapping[str, Any],
    evidence_hash: str,
    now: datetime,
    tx_hash: str | None,
) -> None:
    event_id = _stable_id(
        "live-settlement-verification-event-v1",
        f"{settlement.id}:{state}",
    )
    verification_status = (
        TRADER_ORDER_VERIFICATION_WALLET_ACTIVITY
        if state == "cash_verified"
        else normalize_trader_order_verification_status(order.verification_status)
    )
    statement = pg_insert(TraderOrderVerificationEvent.__table__).values(
        id=event_id,
        trader_order_id=str(order.id),
        verification_status=verification_status,
        source=authority,
        event_type=f"live_settlement_{state}",
        reason=f"Live settlement advanced to {state}",
        provider_order_id=order.provider_order_id,
        provider_clob_order_id=order.provider_clob_order_id,
        execution_wallet_address=order.execution_wallet_address,
        tx_hash=tx_hash,
        token_id=order.token_id,
        payload_json={
            "settlement_id": str(settlement.id),
            "live_state": state,
            "live_state_version": int(settlement.live_state_version or 0),
            "evidence_hash": evidence_hash,
            "evidence": _json_safe(dict(evidence)),
            "projected_pnl_usdc": (
                format(Decimal(settlement.realized_pnl_usdc), "f")
                if settlement.realized_pnl_usdc is not None
                else None
            ),
        },
        created_at=now,
    ).on_conflict_do_nothing(index_elements=["id"])
    await session.execute(statement)


async def project_live_market_final(
    session: AsyncSession,
    *,
    trader_order_id: str,
    online_market_resolution_id: str,
) -> LiveSettlementResult:
    """Persist a Gamma-final projection without claiming realized cash P&L."""

    order = await session.scalar(
        select(TraderOrder)
        .where(TraderOrder.id == str(trader_order_id))
        .with_for_update()
    )
    if order is None:
        raise LiveSettlementIntegrityError("trader order was not found")
    resolution = await session.scalar(
        select(OnlineMarketResolution)
        .where(OnlineMarketResolution.id == str(online_market_resolution_id))
        .with_for_update()
    )
    if resolution is None:
        raise LiveSettlementIntegrityError("online market resolution was not found")
    _validate_live_identity(order, resolution)

    quantity, cost_basis = _entry_economics(order)
    won = str(order.token_id or "").strip() == str(
        resolution.winning_token_id or ""
    ).strip()
    gross_payout = (quantity if won else Decimal(0)).quantize(_USDC_QUANTUM)
    net_payout = gross_payout
    realized_pnl = (net_payout - cost_basis).quantize(_USDC_QUANTUM)
    now = _now_utc()
    evidence = {
        "resolution_id": str(resolution.id),
        "resolution_fact_version": int(resolution.fact_version or 0),
        "resolution_evidence_hash": str(resolution.evidence_hash or ""),
        "condition_id": str(resolution.condition_id or ""),
        "held_token_id": str(order.token_id or ""),
        "winning_token_id": str(resolution.winning_token_id or ""),
        "quantity": format(quantity, "f"),
        "cost_basis_usdc": format(cost_basis, "f"),
        "projected_net_payout_usdc": format(net_payout, "f"),
        "projected_realized_pnl_usdc": format(realized_pnl, "f"),
    }
    canonical, evidence_hash = _canonical_evidence(
        state="market_final",
        authority="gamma_final",
        evidence=evidence,
        net_payout_usdc=net_payout,
        realized_pnl_usdc=realized_pnl,
    )

    settlement = await session.scalar(
        select(TraderOrderSettlement)
        .where(
            TraderOrderSettlement.trader_order_id == str(order.id),
            TraderOrderSettlement.settlement_kind == LIVE_SETTLEMENT_KIND,
        )
        .with_for_update()
    )
    if settlement is not None:
        if str(settlement.mode or "").strip().lower() != "live":
            raise LiveSettlementEvidenceConflict(
                "existing market-resolution settlement belongs to a different mode"
            )
        if str(settlement.online_market_resolution_id or "") != str(resolution.id):
            raise LiveSettlementEvidenceConflict(
                "existing Live settlement references a different resolution fact"
            )
        if settlement.cost_basis_usdc != cost_basis or settlement.quantity != quantity:
            raise LiveSettlementEvidenceConflict(
                "existing Live settlement has different entry economics"
            )
        return _result(settlement, changed=False)

    settlement = TraderOrderSettlement(
        id=_settlement_id(str(order.id)),
        trader_order_id=str(order.id),
        settlement_kind=LIVE_SETTLEMENT_KIND,
        mode="live",
        online_market_resolution_id=str(resolution.id),
        resolution_fact_version=int(resolution.fact_version or 0),
        held_token_id=str(order.token_id or ""),
        winning_token_id=str(resolution.winning_token_id or ""),
        quantity=quantity,
        cost_basis_usdc=cost_basis,
        gross_payout_usdc=gross_payout,
        fee_usdc=_ZERO,
        net_payout_usdc=net_payout,
        realized_pnl_usdc=realized_pnl,
        status="projected",
        live_state="market_final",
        live_state_version=1,
        live_evidence_hash=evidence_hash,
        live_evidence_json=canonical,
        authority="gamma_final",
        evidence_hash=evidence_hash,
        idempotency_key=_idempotency_key(str(order.id)),
        attempt_count=1,
        detected_at=now,
        projected_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(settlement)
    await session.flush()
    await _append_state_event(
        session,
        order=order,
        settlement=settlement,
        state="market_final",
        authority="gamma_final",
        evidence=evidence,
        evidence_hash=evidence_hash,
        now=now,
        tx_hash=None,
    )
    await session.flush()
    return _result(settlement, changed=True)


def _validate_condition_evidence(
    *,
    order: TraderOrder,
    evidence: Mapping[str, Any],
) -> None:
    condition_id = str(evidence.get("condition_id") or "").strip().lower()
    if not condition_id or condition_id != str(order.condition_id or "").strip().lower():
        raise LiveSettlementTransitionError("evidence condition_id does not match the order")


def _validate_claimable_evidence(
    *,
    order: TraderOrder,
    settlement: TraderOrderSettlement,
    evidence: Mapping[str, Any],
) -> None:
    _validate_condition_evidence(order=order, evidence=evidence)
    try:
        block_number = int(evidence.get("block_number") or 0)
    except (TypeError, ValueError) as exc:
        raise LiveSettlementTransitionError("claimable block_number is invalid") from exc
    if block_number <= 0:
        raise LiveSettlementTransitionError("claimable evidence requires a positive block_number")
    wallet_shares = _decimal(
        evidence.get("wallet_balance_shares"),
        field="wallet_balance_shares",
        allow_zero=False,
    )
    required_shares = Decimal(settlement.quantity or 0).quantize(_USDC_QUANTUM)
    if wallet_shares < required_shares:
        raise LiveSettlementTransitionError(
            "claimable wallet balance does not cover the attributed order quantity"
        )
    _decimal(
        evidence.get("expected_payout_usdc"),
        field="expected_payout_usdc",
    )


def _validate_cash_evidence(
    *,
    settlement: TraderOrderSettlement,
    authority: str,
    evidence: Mapping[str, Any],
    tx_hash: str | None,
    net_payout: Decimal,
    realized_pnl: Decimal,
) -> None:
    if authority not in (_DIRECT_CASH_AUTHORITIES | _REDEMPTION_CASH_AUTHORITIES):
        raise LiveSettlementTransitionError(
            f"authority {authority or 'missing'} cannot establish cash_verified"
        )
    cost_basis = Decimal(settlement.cost_basis_usdc or 0).quantize(_USDC_QUANTUM)
    if (net_payout - cost_basis).quantize(_USDC_QUANTUM) != realized_pnl:
        raise LiveSettlementTransitionError(
            "verified realized PnL does not reconcile to net payout minus cost basis"
        )
    if authority in _REDEMPTION_CASH_AUTHORITIES:
        if str(settlement.live_state or "") != "redeem_confirmed":
            raise LiveSettlementTransitionError(
                "redemption cash cannot be verified before redeem_confirmed"
            )
        if not tx_hash or tx_hash != str(settlement.redeem_tx_hash or "").strip().lower():
            raise LiveSettlementTransitionError(
                "cash evidence transaction does not match the confirmed redemption"
            )
        received = _decimal(
            evidence.get("cash_received_usdc"),
            field="cash_received_usdc",
        )
        if received != net_payout:
            raise LiveSettlementTransitionError(
                "cash evidence amount does not equal verified net payout"
            )
        before = _decimal(
            evidence.get("balance_before_usdc"),
            field="balance_before_usdc",
        )
        after = _decimal(
            evidence.get("balance_after_usdc"),
            field="balance_after_usdc",
        )
        if (after - before).quantize(_USDC_QUANTUM) != received:
            raise LiveSettlementTransitionError(
                "cash balance delta does not equal attributed receipt"
            )
    else:
        attributed_size = _decimal(
            evidence.get("attributed_size"),
            field="attributed_size",
            allow_zero=False,
        )
        full_size = Decimal(settlement.quantity or 0).quantize(_USDC_QUANTUM)
        if attributed_size != full_size:
            raise LiveSettlementTransitionError(
                "cash evidence does not cover the full attributed order quantity"
            )


async def advance_live_settlement(
    session: AsyncSession,
    *,
    trader_order_id: str,
    target_state: str,
    authority: str,
    evidence: Mapping[str, Any],
    tx_hash: str | None = None,
    net_payout_usdc: Decimal | None = None,
    realized_pnl_usdc: Decimal | None = None,
) -> LiveSettlementResult:
    """Advance one Live settlement under a strict, monotonic evidence graph."""

    state = str(target_state or "").strip().lower()
    source = str(authority or "").strip().lower()
    if state not in _STATE_RANK or state == "market_final":
        raise LiveSettlementTransitionError(
            "advance target must be claimable, redeem_submitted, "
            "redeem_confirmed, or cash_verified"
        )
    if source not in _AUTHORITY_BY_STATE[state]:
        raise LiveSettlementTransitionError(
            f"authority {source or 'missing'} cannot establish {state}"
        )
    if not isinstance(evidence, Mapping) or not evidence:
        raise LiveSettlementTransitionError("transition evidence is required")

    order = await session.scalar(
        select(TraderOrder)
        .where(TraderOrder.id == str(trader_order_id))
        .with_for_update()
    )
    if order is None or str(order.mode or "").strip().lower() != "live":
        raise LiveSettlementIntegrityError("managed Live order was not found")
    settlement = await session.scalar(
        select(TraderOrderSettlement)
        .where(
            TraderOrderSettlement.trader_order_id == str(order.id),
            TraderOrderSettlement.settlement_kind == LIVE_SETTLEMENT_KIND,
            TraderOrderSettlement.mode == "live",
        )
        .with_for_update()
    )
    if settlement is None:
        raise LiveSettlementIntegrityError(
            "market_final projection must exist before Live state advancement"
        )

    current_state = str(settlement.live_state or "").strip().lower()
    if current_state not in _STATE_RANK:
        raise LiveSettlementIntegrityError("existing Live settlement state is invalid")
    _validate_condition_evidence(order=order, evidence=evidence)

    normalized_tx = _normalize_tx_hash(
        tx_hash or evidence.get("tx_hash"),
        required=state in {"redeem_submitted", "redeem_confirmed"}
        or source in _REDEMPTION_CASH_AUTHORITIES,
    )
    verified_net: Decimal | None = None
    verified_pnl: Decimal | None = None
    if state == "cash_verified":
        if net_payout_usdc is None or realized_pnl_usdc is None:
            raise LiveSettlementTransitionError(
                "cash_verified requires exact net payout and realized PnL"
            )
        verified_net = _decimal(net_payout_usdc, field="net_payout_usdc")
        try:
            verified_pnl = Decimal(str(realized_pnl_usdc)).quantize(_USDC_QUANTUM)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise LiveSettlementTransitionError("realized_pnl_usdc is invalid") from exc
        if not verified_pnl.is_finite():
            raise LiveSettlementTransitionError("realized_pnl_usdc is invalid")
        _validate_cash_evidence(
            settlement=settlement,
            authority=source,
            evidence=evidence,
            tx_hash=normalized_tx,
            net_payout=verified_net,
            realized_pnl=verified_pnl,
        )

    if current_state == "cash_verified":
        if state == "cash_verified" and (
            Decimal(settlement.net_payout_usdc or 0).quantize(_USDC_QUANTUM)
            != verified_net
            or Decimal(settlement.realized_pnl_usdc or 0).quantize(_USDC_QUANTUM)
            != verified_pnl
        ):
            raise LiveSettlementEvidenceConflict(
                "cash_verified replay has different economics"
            )
        return _result(settlement, changed=False)

    current_rank = _STATE_RANK[current_state]
    target_rank = _STATE_RANK[state]
    direct_cash = state == "cash_verified" and source in _DIRECT_CASH_AUTHORITIES
    if target_rank < current_rank:
        return _result(settlement, changed=False)
    if target_rank == current_rank:
        if normalized_tx and str(settlement.redeem_tx_hash or "").strip().lower() not in {
            "",
            normalized_tx,
        }:
            raise LiveSettlementEvidenceConflict(
                f"{state} replay references a different transaction"
            )
        return _result(settlement, changed=False)
    if not direct_cash and target_rank != current_rank + 1:
        raise LiveSettlementTransitionError(
            f"invalid Live settlement transition {current_state} -> {state}"
        )

    if state == "claimable":
        _validate_claimable_evidence(
            order=order,
            settlement=settlement,
            evidence=evidence,
        )
    elif state == "redeem_submitted":
        if current_state != "claimable":
            raise LiveSettlementTransitionError(
                "redeem_submitted requires claimable state"
            )
    elif state == "redeem_confirmed":
        if current_state != "redeem_submitted":
            raise LiveSettlementTransitionError(
                "redeem_confirmed requires redeem_submitted state"
            )
        if normalized_tx != str(settlement.redeem_tx_hash or "").strip().lower():
            raise LiveSettlementTransitionError(
                "confirmed redemption transaction does not match submission"
            )
        try:
            receipt_status = int(evidence.get("receipt_status"))
            block_number = int(evidence.get("block_number") or 0)
        except (TypeError, ValueError) as exc:
            raise LiveSettlementTransitionError("redemption receipt evidence is invalid") from exc
        if receipt_status != 1 or block_number <= 0:
            raise LiveSettlementTransitionError(
                "redeem_confirmed requires a successful mined receipt"
            )

    canonical, evidence_hash = _canonical_evidence(
        state=state,
        authority=source,
        evidence=evidence,
        tx_hash=normalized_tx,
        net_payout_usdc=verified_net,
        realized_pnl_usdc=verified_pnl,
    )
    now = _now_utc()
    settlement.live_state = state
    settlement.live_state_version = int(settlement.live_state_version or 0) + 1
    settlement.live_evidence_hash = evidence_hash
    settlement.live_evidence_json = canonical
    settlement.authority = source
    settlement.evidence_hash = evidence_hash
    settlement.attempt_count = int(settlement.attempt_count or 0) + 1
    settlement.last_error = None
    settlement.updated_at = now
    if state == "claimable":
        settlement.claimable_at = settlement.claimable_at or now
    elif state == "redeem_submitted":
        settlement.redeem_tx_hash = normalized_tx
        settlement.redeem_submitted_at = settlement.redeem_submitted_at or now
    elif state == "redeem_confirmed":
        settlement.redeem_tx_hash = normalized_tx
        settlement.redeem_confirmed_at = settlement.redeem_confirmed_at or now
    elif state == "cash_verified":
        assert verified_net is not None and verified_pnl is not None
        fee = _decimal(evidence.get("fee_usdc", 0), field="fee_usdc")
        settlement.gross_payout_usdc = (verified_net + fee).quantize(_USDC_QUANTUM)
        settlement.fee_usdc = fee
        settlement.net_payout_usdc = verified_net
        settlement.realized_pnl_usdc = verified_pnl
        settlement.status = "verified"
        settlement.verified_at = settlement.verified_at or now
        settlement.cash_verified_at = settlement.cash_verified_at or now
        if normalized_tx:
            settlement.redeem_tx_hash = normalized_tx

        order.actual_profit = float(verified_pnl)
        current_order_status = str(order.status or "").strip().lower()
        if current_order_status in {
            "closed_win",
            "closed_loss",
            "resolved_win",
            "resolved_loss",
        }:
            resolution_label = current_order_status.startswith("resolved")
            if verified_pnl > 0:
                order.status = "resolved_win" if resolution_label else "closed_win"
            elif verified_pnl < 0:
                order.status = "resolved_loss" if resolution_label else "closed_loss"
        apply_trader_order_verification(
            order,
            verification_status=TRADER_ORDER_VERIFICATION_WALLET_ACTIVITY,
            verification_source=source,
            verification_reason="cash_verified by Live settlement evidence",
            execution_wallet_address=order.execution_wallet_address,
            verification_tx_hash=normalized_tx,
            verified_at=now,
            force=True,
        )
        payload = dict(order.payload_json or {})
        payload["verified_close"] = {
            "verified_at": now.isoformat(),
            "source": source,
            "settlement_id": str(settlement.id),
            "matched_size": format(Decimal(settlement.quantity or 0), "f"),
            "matched_proceeds": format(verified_net, "f"),
            "allocated_cost": format(Decimal(settlement.cost_basis_usdc or 0), "f"),
            "realized_pnl": format(verified_pnl, "f"),
            "evidence_hash": evidence_hash,
        }
        order.payload_json = payload
        order.updated_at = now

    await _append_state_event(
        session,
        order=order,
        settlement=settlement,
        state=state,
        authority=source,
        evidence=evidence,
        evidence_hash=evidence_hash,
        now=now,
        tx_hash=normalized_tx,
    )
    # Flush inside the caller-owned transaction so the verification mirror and
    # every constraint are proven before this function reports success.
    await session.flush()
    return _result(settlement, changed=True)


async def has_managed_live_settlement(
    session: AsyncSession,
    trader_order_id: str,
) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(TraderOrderSettlement)
        .where(
            TraderOrderSettlement.trader_order_id == str(trader_order_id),
            TraderOrderSettlement.settlement_kind == LIVE_SETTLEMENT_KIND,
            TraderOrderSettlement.mode == "live",
        )
    )
    return bool(count)


async def _matching_live_order_ids(
    session: AsyncSession,
    condition_id: str,
) -> list[str]:
    rows = (
        await session.execute(
            select(TraderOrderSettlement.trader_order_id)
            .join(
                OnlineMarketResolution,
                OnlineMarketResolution.id
                == TraderOrderSettlement.online_market_resolution_id,
            )
            .where(
                TraderOrderSettlement.mode == "live",
                TraderOrderSettlement.settlement_kind == LIVE_SETTLEMENT_KIND,
                func.lower(OnlineMarketResolution.condition_id)
                == str(condition_id or "").strip().lower(),
                TraderOrderSettlement.status != "verified",
            )
            .order_by(TraderOrderSettlement.trader_order_id.asc())
        )
    ).scalars().all()
    return [str(value) for value in rows]


async def _apply_redeemer_event_to_order(
    session: AsyncSession,
    *,
    order_id: str,
    event: Mapping[str, Any],
) -> int:
    state = str(event.get("state") or "").strip().lower()
    condition_id = str(event.get("condition_id") or "").strip().lower()
    tx_hash = str(event.get("tx_hash") or "").strip().lower() or None
    base_evidence = {
        "condition_id": condition_id,
        "block_number": int(event.get("block_number") or 0),
        "wallet_balance_shares": str(event.get("wallet_balance_shares") or "0"),
        "expected_payout_usdc": str(event.get("expected_payout_usdc") or "0"),
    }
    changed = 0
    if state in {"claimable", "redeem_submitted", "redeem_confirmed"}:
        claim = await advance_live_settlement(
            session,
            trader_order_id=order_id,
            target_state="claimable",
            authority="ctf_claimable",
            evidence=base_evidence,
        )
        changed += int(claim.changed)
    if state in {"redeem_submitted", "redeem_confirmed"}:
        submitted = await advance_live_settlement(
            session,
            trader_order_id=order_id,
            target_state="redeem_submitted",
            authority="ctf_redeem_submission",
            evidence={"condition_id": condition_id, "tx_hash": tx_hash},
            tx_hash=tx_hash,
        )
        changed += int(submitted.changed)
    if state == "redeem_confirmed":
        confirmed = await advance_live_settlement(
            session,
            trader_order_id=order_id,
            target_state="redeem_confirmed",
            authority="ctf_redeem_receipt",
            evidence={
                "condition_id": condition_id,
                "tx_hash": tx_hash,
                "receipt_status": int(event.get("receipt_status") or 0),
                "block_number": int(event.get("block_number") or 0),
            },
            tx_hash=tx_hash,
        )
        changed += int(confirmed.changed)
    return changed


async def record_redeemer_lifecycle_event(
    event: Mapping[str, Any],
    *,
    session_factory=AsyncSessionLocal,
) -> dict[str, Any]:
    """Persist one pre-submit/submitted/confirmed redeemer callback event."""

    if not isinstance(event, Mapping):
        raise LiveSettlementTransitionError("redeemer lifecycle event must be a mapping")
    condition_id = str(event.get("condition_id") or "").strip().lower()
    if not condition_id:
        raise LiveSettlementTransitionError("redeemer lifecycle event has no condition_id")
    async with session_factory() as session:
        order_ids = await _matching_live_order_ids(session, condition_id)
        attributed_quantity = (
            await session.scalar(
                select(func.coalesce(func.sum(TraderOrderSettlement.quantity), 0)).where(
                    TraderOrderSettlement.trader_order_id.in_(order_ids),
                    TraderOrderSettlement.mode == "live",
                    TraderOrderSettlement.settlement_kind == LIVE_SETTLEMENT_KIND,
                    TraderOrderSettlement.status != "verified",
                )
            )
            if order_ids
            else Decimal(0)
        )

    if order_ids:
        wallet_shares = _decimal(
            event.get("wallet_balance_shares"),
            field="wallet_balance_shares",
            allow_zero=False,
        )
        required_shares = Decimal(attributed_quantity or 0).quantize(_USDC_QUANTUM)
        if wallet_shares < required_shares:
            return {
                "matched": len(order_ids),
                "updated": 0,
                "errors": [
                    {
                        "order_id": "*",
                        "error_type": "LiveSettlementTransitionError",
                        "error": (
                            "claimable wallet balance does not cover the total "
                            "managed quantity for this condition"
                        ),
                    }
                ],
            }

    updated = 0
    errors: list[dict[str, str]] = []
    for order_id in order_ids:
        try:
            async with session_factory() as session, session.begin():
                updated += await _apply_redeemer_event_to_order(
                    session,
                    order_id=order_id,
                    event=event,
                )
        except Exception as exc:  # noqa: BLE001 - isolate and report each order failure
            errors.append(
                {
                    "order_id": order_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return {
        "matched": len(order_ids),
        "updated": updated,
        "errors": errors,
    }


async def apply_redeemer_state_updates(
    summary: Mapping[str, Any],
    *,
    session_factory=AsyncSessionLocal,
) -> dict[str, Any]:
    """Idempotently reconcile a completed real redeemer cycle into Live state."""

    if bool(summary.get("dry_run")):
        return {"matched": 0, "updated": 0, "errors": [], "skipped": "dry_run"}
    matched = 0
    updated = 0
    errors: list[dict[str, str]] = []
    results = summary.get("condition_results")
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"claimable", "submitted", "executed"}:
            continue
        event = dict(item)
        event["state"] = (
            "redeem_confirmed"
            if status == "executed" and int(item.get("receipt_status") or 0) == 1
            else "redeem_submitted"
            if status == "submitted"
            else "claimable"
        )
        result = await record_redeemer_lifecycle_event(
            event,
            session_factory=session_factory,
        )
        matched += int(result.get("matched") or 0)
        updated += int(result.get("updated") or 0)
        errors.extend(result.get("errors") or [])
    return {"matched": matched, "updated": updated, "errors": errors}


__all__ = [
    "LIVE_STATES",
    "LiveSettlementError",
    "LiveSettlementEvidenceConflict",
    "LiveSettlementIntegrityError",
    "LiveSettlementResult",
    "LiveSettlementTransitionError",
    "advance_live_settlement",
    "apply_redeemer_state_updates",
    "has_managed_live_settlement",
    "project_live_market_final",
    "record_redeemer_lifecycle_event",
]
