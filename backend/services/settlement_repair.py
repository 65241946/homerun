"""Auditable preview/digest/apply flow for historical Shadow settlements.

The service is database-only: it never fetches provider APIs and never owns a
commit or rollback.  Callers must wrap ``apply_settlement_repair`` in one
transaction so checkpoint, settlement certificate, cash journal, projections,
and the repair receipt either all persist or all roll back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
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
from services.market_identity import MarketIdentity, identity_from_order
from services.settlement_coordinator import (
    SAFE_IDENTITY_STATUSES,
    TERMINAL_SETTLEMENT_STATUSES,
    SettlementCoordinatorError,
    apply_shadow_market_resolution,
    inspect_shadow_market_resolution,
    shadow_resolution_block_reason,
)
from services.simulation_ledger import checkpoint_legacy_account, quantize_usdc

REPAIR_DIGEST_VERSION = 1
MAX_REPAIR_ORDERS = 200
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class SettlementRepairError(RuntimeError):
    """Base class for historical settlement repair failures."""


class SettlementRepairNotFound(SettlementRepairError):
    """Raised when the requested simulation account does not exist."""


class SettlementRepairRejected(SettlementRepairError):
    """Raised when explicit operator confirmation is absent or malformed."""


class SettlementRepairConflict(SettlementRepairError):
    """Raised when preview state changed or any selected order is unsafe."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _money(value: Any) -> str | None:
    if value is None:
        return None
    return f"{quantize_usdc(value):.6f}"


def _status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, TradeStatus):
        return value.value
    raw = getattr(value, "value", value)
    return str(raw)


def _references(order: TraderOrder) -> tuple[str, str, str] | None:
    payload = order.payload_json if isinstance(order.payload_json, Mapping) else {}
    raw = payload.get("simulation_ledger")
    if not isinstance(raw, Mapping):
        return None
    values = tuple(str(raw.get(key) or "").strip() for key in ("account_id", "trade_id", "position_id"))
    if any(not value for value in values):
        return None
    return values  # type: ignore[return-value]


def _normalize_order_ids(
    order_ids: Iterable[str] | None,
    *,
    required: bool,
) -> list[str] | None:
    if order_ids is None:
        if required:
            raise SettlementRepairRejected("order_ids are required for apply")
        return None
    raw = [str(value or "").strip() for value in order_ids]
    if not raw or any(not value for value in raw):
        raise SettlementRepairRejected("order_ids must contain at least one non-empty ID")
    if len(raw) > MAX_REPAIR_ORDERS:
        raise SettlementRepairRejected(f"order_ids cannot exceed {MAX_REPAIR_ORDERS} entries")
    if len(set(raw)) != len(raw):
        raise SettlementRepairRejected("order_ids must be unique")
    return sorted(raw)


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _classification_for_identity(identity_status: str) -> str:
    return "blocked" if identity_status == "invalid" else "ambiguous"


def _base_item(order_id: str) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "classification": "blocked",
        "reason_code": "order_not_found",
        "reason_detail": "Trader order was not found.",
        "order_updated_at": None,
        "order_mode": None,
        "order_status": None,
        "persisted_identity_status": None,
        "identity_status": None,
        "identity_reason": None,
        "identity_would_update": False,
        "venue": None,
        "provider_market_id": None,
        "condition_id": None,
        "token_id": None,
        "outcome_index": None,
        "order_payload_hash": None,
        "simulation_account_id": None,
        "simulation_trade_id": None,
        "simulation_trade_status": None,
        "simulation_position_id": None,
        "simulation_position_status": None,
        "resolution_id": None,
        "resolution_state": None,
        "resolution_fact_version": None,
        "resolution_evidence_hash": None,
        "resolution_updated_at": None,
        "settlement_id": None,
        "settlement_status": None,
        "already_applied": False,
        "candidate_net_payout_usdc": None,
        "candidate_realized_pnl_usdc": None,
    }


def _terminal_settlement_conflict(
    settlement: TraderOrderSettlement,
    order: TraderOrder,
    resolution: OnlineMarketResolution,
) -> str | None:
    expected = (
        (str(settlement.online_market_resolution_id or ""), str(resolution.id)),
        (str(settlement.resolution_fact_version or ""), str(int(resolution.fact_version or 1))),
        (str(settlement.evidence_hash or ""), str(resolution.evidence_hash or "")),
        (str(settlement.held_token_id or ""), str(order.token_id or "")),
        (str(settlement.winning_token_id or ""), str(resolution.winning_token_id or "")),
    )
    return next(("terminal_settlement_evidence_changed" for left, right in expected if left != right), None)


def _identity_would_update(order: TraderOrder, identity: MarketIdentity) -> bool:
    return (
        str(order.identity_status or "").strip().lower() != identity.status
        or str(order.venue or "").strip().lower() != identity.venue
        or (str(order.provider_market_id or "").strip() or None) != identity.provider_market_id
        or (str(order.condition_id or "").strip().lower() or None) != identity.condition_id
        or (str(order.token_id or "").strip() or None) != identity.token_id
        or (int(order.outcome_index) if order.outcome_index is not None else None) != identity.outcome_index
    )


def _populate_order_fields(
    item: dict[str, Any],
    order: TraderOrder,
    identity: MarketIdentity,
) -> tuple[str, str, str] | None:
    refs = _references(order)
    persisted_status = str(order.identity_status or "").strip().lower() or None
    payload = order.payload_json if isinstance(order.payload_json, Mapping) else {}
    item.update(
        {
            "order_updated_at": _iso_utc(order.updated_at),
            "order_mode": str(order.mode or "").strip().lower() or None,
            "order_status": str(order.status or "").strip().lower() or None,
            "persisted_identity_status": persisted_status,
            "identity_status": identity.status,
            "identity_reason": identity.reason,
            "identity_would_update": _identity_would_update(order, identity),
            "venue": identity.venue,
            "provider_market_id": identity.provider_market_id,
            "condition_id": identity.condition_id,
            "token_id": identity.token_id,
            "outcome_index": identity.outcome_index,
            "order_payload_hash": _canonical_digest(payload),
            "simulation_account_id": refs[0] if refs else None,
            "simulation_trade_id": refs[1] if refs else None,
            "simulation_position_id": refs[2] if refs else None,
        }
    )
    return refs


def _resolution_key(identity: MarketIdentity) -> tuple[str, str]:
    return identity.venue, str(identity.condition_id or "")


def _order_with_identity(order: TraderOrder, identity: MarketIdentity) -> SimpleNamespace:
    return SimpleNamespace(
        id=order.id,
        mode=order.mode,
        status=order.status,
        payload_json=order.payload_json,
        venue=identity.venue,
        provider_market_id=identity.provider_market_id,
        condition_id=identity.condition_id,
        token_id=identity.token_id,
        outcome_index=identity.outcome_index,
        identity_status=identity.status,
    )


def _persist_inferred_identity(order: TraderOrder, item: Mapping[str, Any]) -> None:
    if not bool(item.get("identity_would_update")):
        return
    identity = identity_from_order(order)
    expected = (
        str(item.get("venue") or ""),
        str(item.get("provider_market_id") or "") or None,
        str(item.get("condition_id") or "") or None,
        str(item.get("token_id") or "") or None,
        item.get("outcome_index"),
        str(item.get("identity_status") or ""),
    )
    actual = (
        identity.venue,
        identity.provider_market_id,
        identity.condition_id,
        identity.token_id,
        identity.outcome_index,
        identity.status,
    )
    if identity.status != "legacy_inferred" or actual != expected:
        raise SettlementRepairConflict("legacy identity changed after preview")
    order.venue = identity.venue
    order.provider_market_id = identity.provider_market_id
    order.condition_id = identity.condition_id
    order.token_id = identity.token_id
    order.outcome_index = identity.outcome_index
    order.identity_status = identity.status


def _repair_marker_has_digest(order: TraderOrder, digest: str, account_id: str) -> bool:
    payload = order.payload_json if isinstance(order.payload_json, Mapping) else {}
    marker = payload.get("settlement_repair")
    if not isinstance(marker, Mapping) or str(marker.get("account_id") or "") != account_id:
        return False
    digests = marker.get("applied_digests")
    if isinstance(digests, list) and digest in {str(value) for value in digests}:
        return True
    return str(marker.get("preview_digest") or "") == digest


def _record_repair_marker(
    order: TraderOrder,
    *,
    account_id: str,
    preview_digest: str,
    settlement_id: str,
    applied_at: datetime,
) -> None:
    payload = dict(order.payload_json) if isinstance(order.payload_json, Mapping) else {}
    prior = payload.get("settlement_repair")
    marker = dict(prior) if isinstance(prior, Mapping) else {}
    digests = [str(value) for value in marker.get("applied_digests", []) if str(value)]
    if preview_digest not in digests:
        digests.append(preview_digest)
    marker.update(
        {
            "account_id": account_id,
            "preview_digest": preview_digest,
            "applied_digests": digests,
            "settlement_id": settlement_id,
            "applied_at": _iso_utc(applied_at),
            "authority": "settlement_repair_api",
        }
    )
    payload["settlement_repair"] = marker
    order.payload_json = payload
    order.updated_at = applied_at


async def _load_orders(
    session: AsyncSession,
    *,
    account_id: str,
    order_ids: list[str] | None,
) -> tuple[list[str], dict[str, TraderOrder]]:
    if order_ids is not None:
        ids = order_ids
        rows = (await session.execute(select(TraderOrder).where(TraderOrder.id.in_(ids)))).scalars().all()
        return ids, {str(row.id): row for row in rows}

    account_expr = TraderOrder.payload_json["simulation_ledger"]["account_id"].as_string()
    rows = (
        (
            await session.execute(
                select(TraderOrder)
                .where(account_expr == account_id)
                .order_by(TraderOrder.id)
                .limit(MAX_REPAIR_ORDERS + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > MAX_REPAIR_ORDERS:
        raise SettlementRepairRejected(
            f"account has more than {MAX_REPAIR_ORDERS} repair candidates; supply explicit order_ids"
        )
    return [str(row.id) for row in rows], {str(row.id): row for row in rows}


async def _build_preview(
    session: AsyncSession,
    *,
    account_id: str,
    order_ids: list[str] | None,
    lock: bool,
) -> dict[str, Any]:
    initial_account = await session.get(SimulationAccount, account_id)
    if initial_account is None:
        raise SettlementRepairNotFound(f"Simulation account not found: {account_id}")

    selected_ids, preliminary_orders = await _load_orders(
        session,
        account_id=account_id,
        order_ids=order_ids,
    )
    preliminary_identities = {order_id: identity_from_order(order) for order_id, order in preliminary_orders.items()}
    conditions = sorted(
        {
            str(identity.condition_id or "").strip()
            for identity in preliminary_identities.values()
            if identity.status in SAFE_IDENTITY_STATUSES and str(identity.condition_id or "").strip()
        }
    )
    resolution_query = select(OnlineMarketResolution).where(OnlineMarketResolution.condition_id.in_(conditions))
    if lock and conditions:
        resolution_query = resolution_query.order_by(
            OnlineMarketResolution.condition_id,
            OnlineMarketResolution.venue,
        ).with_for_update(read=True)
    resolutions = (await session.execute(resolution_query)).scalars().all() if conditions else []
    resolution_by_key = {
        (str(row.venue or "").strip().lower(), str(row.condition_id or "").strip()): row for row in resolutions
    }

    if lock:
        account = (
            await session.execute(select(SimulationAccount).where(SimulationAccount.id == account_id).with_for_update())
        ).scalar_one_or_none()
        if account is None:
            raise SettlementRepairNotFound(f"Simulation account not found: {account_id}")
    else:
        account = initial_account

    preliminary_refs = {
        order_id: refs
        for order_id, order in preliminary_orders.items()
        if (refs := _references(order)) is not None and refs[0] == account_id
    }
    trade_ids = sorted({refs[1] for refs in preliminary_refs.values()})
    position_ids = sorted({refs[2] for refs in preliminary_refs.values()})

    trade_query = select(SimulationTrade).where(SimulationTrade.id.in_(trade_ids))
    position_query = select(SimulationPosition).where(SimulationPosition.id.in_(position_ids))
    if lock:
        trade_query = trade_query.order_by(SimulationTrade.id).with_for_update()
        position_query = position_query.order_by(SimulationPosition.id).with_for_update()
    trades = (await session.execute(trade_query)).scalars().all() if trade_ids else []
    positions = (await session.execute(position_query)).scalars().all() if position_ids else []

    target_order_ids = sorted(preliminary_refs)
    if lock and target_order_ids:
        locked_orders = (
            (
                await session.execute(
                    select(TraderOrder)
                    .where(TraderOrder.id.in_(target_order_ids))
                    .order_by(TraderOrder.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        order_by_id = dict(preliminary_orders)
        order_by_id.update({str(row.id): row for row in locked_orders})
    else:
        order_by_id = preliminary_orders

    settlement_query = select(TraderOrderSettlement).where(TraderOrderSettlement.trader_order_id.in_(selected_ids))
    if lock and selected_ids:
        settlement_query = settlement_query.order_by(TraderOrderSettlement.trader_order_id).with_for_update()
    settlements = (await session.execute(settlement_query)).scalars().all() if selected_ids else []
    settlement_by_order = {str(row.trader_order_id): row for row in settlements}
    trade_by_id = {str(row.id): row for row in trades}
    position_by_id = {str(row.id): row for row in positions}

    items: list[dict[str, Any]] = []
    total_payout = Decimal(0)
    total_pnl = Decimal(0)
    counts = {"safe": 0, "ambiguous": 0, "blocked": 0}

    for order_id in selected_ids:
        item = _base_item(order_id)
        order = order_by_id.get(order_id)
        if order is None:
            items.append(item)
            counts["blocked"] += 1
            continue

        identity = identity_from_order(order)
        refs = _populate_order_fields(item, order, identity)
        if refs is None:
            item.update(
                reason_code="simulation_ledger_reference_missing",
                reason_detail="Order payload has no complete simulation account/trade/position references.",
            )
            items.append(item)
            counts["blocked"] += 1
            continue
        if refs[0] != account_id:
            item.update(
                reason_code="order_account_mismatch",
                reason_detail="Trader order belongs to a different simulation account.",
            )
            items.append(item)
            counts["blocked"] += 1
            continue

        mode = str(order.mode or "").strip().lower()
        if mode != "shadow":
            item.update(
                reason_code="unsupported_order_mode",
                reason_detail=f"Order mode {mode or 'missing'} is not Shadow.",
            )
            items.append(item)
            counts["blocked"] += 1
            continue

        identity_status = identity.status
        if identity_status not in SAFE_IDENTITY_STATUSES:
            classification = _classification_for_identity(identity_status)
            item.update(
                classification=classification,
                reason_code="identity_not_settlement_safe",
                reason_detail=(f"Order identity status {identity_status or 'missing'} is not settlement-safe."),
            )
            items.append(item)
            counts[classification] += 1
            continue

        resolution = resolution_by_key.get(_resolution_key(identity))
        if resolution is None:
            item.update(
                reason_code="resolution_fact_missing",
                reason_detail="No persisted official resolution fact matches the order identity.",
            )
            items.append(item)
            counts["blocked"] += 1
            continue
        item.update(
            {
                "resolution_id": str(resolution.id),
                "resolution_state": str(resolution.state or "").strip().lower() or None,
                "resolution_fact_version": int(resolution.fact_version or 1),
                "resolution_evidence_hash": str(resolution.evidence_hash or "") or None,
                "resolution_updated_at": _iso_utc(resolution.updated_at),
            }
        )

        order_view = _order_with_identity(order, identity)
        resolution_block = shadow_resolution_block_reason(order_view, resolution)
        if resolution_block is not None:
            item.update(
                reason_code=resolution_block[0],
                reason_detail=resolution_block[1],
            )
            items.append(item)
            counts["blocked"] += 1
            continue

        settlement = settlement_by_order.get(order_id)
        if settlement is not None:
            item["settlement_id"] = str(settlement.id)
            item["settlement_status"] = str(settlement.status or "")
        if settlement is not None and str(settlement.status) in TERMINAL_SETTLEMENT_STATUSES:
            conflict = _terminal_settlement_conflict(settlement, order_view, resolution)
            if conflict is not None or str(resolution.state or "").strip().lower() != "final":
                item.update(
                    reason_code=conflict or "resolution_not_final",
                    reason_detail="Terminal settlement no longer matches the persisted official fact.",
                )
                items.append(item)
                counts["blocked"] += 1
                continue
            payout = quantize_usdc(settlement.net_payout_usdc or 0)
            pnl = quantize_usdc(settlement.realized_pnl_usdc or 0)
            item.update(
                classification="safe",
                reason_code=None,
                reason_detail=None,
                already_applied=True,
                candidate_net_payout_usdc=_money(payout),
                candidate_realized_pnl_usdc=_money(pnl),
            )
            items.append(item)
            counts["safe"] += 1
            total_payout += payout
            total_pnl += pnl
            continue

        trade = trade_by_id.get(refs[1])
        position = position_by_id.get(refs[2])
        item["simulation_trade_status"] = _status(trade.status) if trade is not None else None
        item["simulation_position_status"] = _status(position.status) if position is not None else None
        if trade is None or position is None:
            missing = [name for name, value in (("trade", trade), ("position", position)) if value is None]
            item.update(
                reason_code="simulation_ledger_row_missing",
                reason_detail=f"Referenced simulation rows are missing: {', '.join(missing)}.",
            )
            items.append(item)
            counts["blocked"] += 1
            continue

        inspection = inspect_shadow_market_resolution(
            account=account,
            trade=trade,
            position=position,
            order=order_view,
            resolution=resolution,
            allow_legacy_account=True,
        )
        if not inspection.eligible or inspection.economics is None:
            item.update(
                reason_code=inspection.reason_code,
                reason_detail=inspection.reason_detail,
            )
            items.append(item)
            counts["blocked"] += 1
            continue
        payout = inspection.economics.net_payout_usdc
        pnl = inspection.economics.realized_pnl_usdc
        item.update(
            classification="safe",
            reason_code=None,
            reason_detail=None,
            candidate_net_payout_usdc=_money(payout),
            candidate_realized_pnl_usdc=_money(pnl),
        )
        items.append(item)
        counts["safe"] += 1
        total_payout += payout
        total_pnl += pnl

    account_payload = {
        "account_id": account_id,
        "ledger_version": int(account.ledger_version or 1),
        "ledger_integrity_status": str(account.ledger_integrity_status or "legacy"),
        "initial_capital_usdc": _money(account.initial_capital),
        "projected_balance_usdc": _money(account.current_capital),
        "updated_at": _iso_utc(account.updated_at),
    }
    certificate = {
        "digest_version": REPAIR_DIGEST_VERSION,
        "account": account_payload,
        "order_ids": selected_ids,
        "items": items,
        "totals": {
            "candidate_net_payout_usdc": _money(total_payout),
            "candidate_realized_pnl_usdc": _money(total_pnl),
        },
    }
    return {
        **certificate,
        "counts": counts,
        "preview_digest": _canonical_digest(certificate),
    }


async def preview_settlement_repair(
    session: AsyncSession,
    *,
    account_id: str,
    order_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, network-free and write-free repair preview."""

    normalized_account = str(account_id or "").strip()
    if not normalized_account:
        raise SettlementRepairRejected("account_id is required")
    normalized_ids = _normalize_order_ids(order_ids, required=False)
    return await _build_preview(
        session,
        account_id=normalized_account,
        order_ids=normalized_ids,
        lock=False,
    )


async def apply_settlement_repair(
    session: AsyncSession,
    *,
    account_id: str,
    order_ids: Iterable[str],
    preview_digest: str,
    confirm: bool,
    operator: str = "settlement_repair_api",
) -> dict[str, Any]:
    """Apply an exact approved preview through the normal coordinator.

    The caller must own one surrounding transaction.  This function never
    commits or rolls back and performs no provider/network calls.
    """

    if confirm is not True:
        raise SettlementRepairRejected("settlement repair requires confirm=true")
    normalized_account = str(account_id or "").strip()
    if not normalized_account:
        raise SettlementRepairRejected("account_id is required")
    normalized_ids = _normalize_order_ids(order_ids, required=True)
    assert normalized_ids is not None
    normalized_digest = str(preview_digest or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized_digest):
        raise SettlementRepairRejected("preview_digest must be a 64-character SHA-256 hex digest")
    if not session.in_transaction():
        raise SettlementRepairRejected("apply requires a caller-owned database transaction")

    current = await _build_preview(
        session,
        account_id=normalized_account,
        order_ids=normalized_ids,
        lock=True,
    )
    unsafe = [item for item in current["items"] if item["classification"] != "safe"]
    if unsafe:
        codes = ",".join(f"{item['order_id']}:{item['reason_code']}" for item in unsafe)
        raise SettlementRepairConflict(f"selected orders are not repair-safe: {codes}")

    digest_matches = hmac.compare_digest(current["preview_digest"], normalized_digest)
    if not digest_matches:
        rows = (await session.execute(select(TraderOrder).where(TraderOrder.id.in_(normalized_ids)))).scalars().all()
        replay_matches = (
            len(rows) == len(normalized_ids)
            and all(_repair_marker_has_digest(order, normalized_digest, normalized_account) for order in rows)
            and all(item["already_applied"] for item in current["items"])
        )
        if not replay_matches:
            raise SettlementRepairConflict(
                "preview_digest is stale; order, account, trade, position, or resolution state changed"
            )

    for item in current["items"]:
        order = await session.get(TraderOrder, item["order_id"])
        if order is None:
            raise SettlementRepairConflict(f"order disappeared during repair: {item['order_id']}")
        _persist_inferred_identity(order, item)
    await session.flush()

    account = await session.get(SimulationAccount, normalized_account)
    if account is None:
        raise SettlementRepairNotFound(f"Simulation account not found: {normalized_account}")
    version = int(account.ledger_version or 1)
    integrity = str(account.ledger_integrity_status or "legacy").strip().lower()
    checkpointed = False
    if version < 2:
        if integrity != "legacy":
            raise SettlementRepairConflict(f"legacy account has inconsistent integrity state: v{version}/{integrity}")
        await checkpoint_legacy_account(
            session,
            account_id=normalized_account,
            reason=f"approved settlement repair {normalized_digest}",
            operator=str(operator or "settlement_repair_api"),
        )
        checkpointed = True
    elif integrity not in {"complete", "checkpointed"}:
        raise SettlementRepairConflict(f"account ledger integrity {integrity or 'missing'} blocks settlement repair")

    results: list[dict[str, Any]] = []
    applied_at = _now_utc()
    for item in current["items"]:
        resolution_id = str(item["resolution_id"] or "")
        if not resolution_id:
            raise SettlementRepairConflict(f"order {item['order_id']} has no persisted resolution ID")
        try:
            result = await apply_shadow_market_resolution(
                session,
                trader_order_id=item["order_id"],
                online_market_resolution_id=resolution_id,
            )
        except (SettlementCoordinatorError, ValueError) as exc:
            raise SettlementRepairConflict(f"coordinator rejected order {item['order_id']}: {exc}") from exc
        if result.status not in TERMINAL_SETTLEMENT_STATUSES:
            raise SettlementRepairConflict(
                f"coordinator did not produce a terminal settlement for {item['order_id']}: {result.status}"
            )
        order = await session.get(TraderOrder, item["order_id"])
        if order is None:
            raise SettlementRepairConflict(f"order disappeared during repair: {item['order_id']}")
        _record_repair_marker(
            order,
            account_id=normalized_account,
            preview_digest=normalized_digest,
            settlement_id=result.settlement_id,
            applied_at=applied_at,
        )
        results.append(
            {
                "order_id": result.trader_order_id,
                "settlement_id": result.settlement_id,
                "status": result.status,
                "applied": result.applied,
                "idempotent_replay": result.idempotent_replay,
                "net_payout_usdc": _money(result.net_payout_usdc),
                "realized_pnl_usdc": _money(result.realized_pnl_usdc),
            }
        )
    await session.flush()

    applied_count = sum(1 for result in results if result["applied"])
    return {
        "account_id": normalized_account,
        "preview_digest": normalized_digest,
        "checkpointed": checkpointed,
        "applied_count": applied_count,
        "idempotent_replay": applied_count == 0 and bool(results),
        "results": results,
    }


__all__ = [
    "MAX_REPAIR_ORDERS",
    "REPAIR_DIGEST_VERSION",
    "SettlementRepairConflict",
    "SettlementRepairError",
    "SettlementRepairNotFound",
    "SettlementRepairRejected",
    "apply_settlement_repair",
    "preview_settlement_repair",
]
