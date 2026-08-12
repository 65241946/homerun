"""API routes for trader CRUD and trader-level runtime surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.database import (
    AsyncSessionLocal,
    ExecutionSession,
    SimulationAccount,
    TraderDecision,
    TraderOrder,
    get_db_session,
)
from services.live_price_snapshot import normalize_binary_price_history
from services.pause_state import global_pause_state
from services import shared_state as scanner_shared_state
from services.traders_copy_trade_signal_service import traders_copy_trade_signal_service
from services.strategy_tune_agent import run_strategy_tune_agent
from services import trader_binding_cache
from services.strategy_loader import strategy_loader
from services.trader_orchestrator.position_lifecycle import (
    reconcile_live_positions,
    reconcile_shadow_positions,
)
from services.trader_orchestrator.session_engine import ExecutionSessionEngine
from services.trader_orchestrator_state import (
    OPEN_ORDER_STATUSES,
    adopt_live_wallet_position,
    cleanup_trader_open_orders,
    create_config_revision,
    create_trader_decision,
    create_trader,
    create_trader_event,
    create_trader_from_template,
    delete_trader,
    get_trader_copy_analytics,
    get_trader,
    get_trader_decision_detail,
    get_open_order_summary_for_trader,
    get_open_position_summary_for_trader,
    get_serialized_execution_session_detail,
    list_live_wallet_positions_for_trader,
    list_serialized_trader_decisions,
    list_serialized_trader_events,
    list_serialized_execution_sessions,
    list_serialized_trader_orders,
    list_trader_templates,
    list_traders,
    read_orchestrator_control,
    request_trader_run,
    set_trader_paused,
    sync_trader_position_inventory,
    transfer_open_trades,
    update_trader,
    update_trader_decision,
)
from services.live_execution_service import (
    live_execution_service,
)
from utils.converters import normalize_market_id, to_iso
from utils.market_urls import infer_market_platform
from utils.logger import get_logger
from utils.utcnow import utcnow

router = APIRouter(prefix="/traders", tags=["Traders"])
_LOSS_STREAK_RESET_AT_KEY = "loss_streak_reset_at"
_LOSS_STREAK_RESET_REASON_KEY = "loss_streak_reset_reason"
logger = get_logger(__name__)
_background_tasks: set[Any] = set()


def _track_background_task(task: Any) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _invalidate_trader_strategy_caches() -> None:
    # Plan 0041: after any mutation that may touch
    # ``traders.source_configs_json[].strategy_params`` (create / update /
    # delete) we drop the trader-binding cache and every per-trader
    # strategy clone so the dispatcher rebuilds them on the next event.
    # Both caches are extremely cheap to repopulate (one SQL roundtrip
    # for the binding cache, one ``clone_for_trader`` call per
    # ``(slug, trader)`` for the strategy clones), so eagerly clearing
    # them is preferable to waiting out the 3 s soft-TTL.
    try:
        trader_binding_cache.invalidate()
    except Exception:
        logger.exception("trader_binding_cache.invalidate failed")
    try:
        strategy_loader.invalidate_per_trader()
    except Exception:
        logger.exception("strategy_loader.invalidate_per_trader failed")


def _copy_bootstrap_requested(trader_payload: dict[str, Any], override: bool | None) -> bool:
    if override is False:
        return False
    source_configs = trader_payload.get("source_configs") if isinstance(trader_payload, dict) else []
    if not isinstance(source_configs, list):
        return False
    for source_config in source_configs:
        if not isinstance(source_config, dict):
            continue
        if str(source_config.get("source_key") or "").strip().lower() != "traders":
            continue
        if str(source_config.get("strategy_key") or "").strip().lower() != "traders_copy_trade":
            continue
        params = source_config.get("strategy_params") if isinstance(source_config.get("strategy_params"), dict) else {}
        if override is True:
            return True
        return bool(params.get("copy_existing_positions_on_start", False))
    return False


async def _run_copy_bootstrap_background(
    *,
    trader_id: str,
    copy_existing_positions: bool | None,
    requested_by: str | None,
) -> None:
    try:
        summary = await traders_copy_trade_signal_service.copy_existing_open_positions_for_trader(
            trader_id=trader_id,
            copy_existing_positions=copy_existing_positions,
        )
    except Exception as exc:
        logger.warning("Copy-trade start bootstrap failed", trader_id=trader_id, exc_info=exc)
        summary = {
            "trader_id": trader_id,
            "status": "failed",
            "reason": "bootstrap_exception",
            "error": str(exc),
        }

    async with AsyncSessionLocal() as session:
        await create_trader_event(
            session,
            trader_id=trader_id,
            event_type="trader_copy_bootstrap",
            severity="warn" if str(summary.get("status") or "").strip().lower() == "failed" else "info",
            source="operator",
            operator=requested_by,
            message="Copy bootstrap evaluated on trader start",
            payload=summary,
        )


class TraderSourceConfigRequest(BaseModel):
    source_key: str
    strategy_key: str
    strategy_version: int | str | None = None
    strategy_params: dict[str, Any] = Field(default_factory=dict)


class TraderRequest(BaseModel):
    name: str
    description: Optional[str] = None
    mode: Optional[str] = None
    latency_class: Optional[str] = None
    copy_from_trader_id: Optional[str] = None
    source_configs: list[TraderSourceConfigRequest] = Field(default_factory=list)
    interval_seconds: int = Field(default=60, ge=1, le=86400)
    risk_limits: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True
    is_paused: bool = False
    block_new_orders: bool = False
    requested_by: Optional[str] = None
    reason: Optional[str] = None

    @field_validator("source_configs")
    @classmethod
    def _enforce_single_source_config(
        cls, value: list[TraderSourceConfigRequest]
    ) -> list[TraderSourceConfigRequest]:
        # A trader runs exactly one strategy on one source. Reject anything else.
        if len(value) > 1:
            raise ValueError(
                "source_configs must contain exactly one entry "
                "(a trader runs one strategy on one source)"
            )
        return value


class TraderPatchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    mode: Optional[str] = None
    latency_class: Optional[str] = None
    source_configs: Optional[list[TraderSourceConfigRequest]] = None
    interval_seconds: Optional[int] = Field(default=None, ge=1, le=86400)
    risk_limits: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    is_enabled: Optional[bool] = None
    is_paused: Optional[bool] = None
    block_new_orders: Optional[bool] = None
    requested_by: Optional[str] = None
    reason: Optional[str] = None

    @field_validator("source_configs")
    @classmethod
    def _enforce_single_source_config(
        cls, value: Optional[list[TraderSourceConfigRequest]]
    ) -> Optional[list[TraderSourceConfigRequest]]:
        if value is None:
            return value
        if len(value) > 1:
            raise ValueError(
                "source_configs must contain exactly one entry "
                "(a trader runs one strategy on one source)"
            )
        return value


class TraderTemplateCreateRequest(BaseModel):
    template_id: str
    overrides: dict[str, Any] = Field(default_factory=dict)
    requested_by: Optional[str] = None


class TraderDeleteAction(str, Enum):
    block = "block"
    disable = "disable"
    force_delete = "force_delete"
    transfer_delete = "transfer_delete"


class TraderPositionCleanupScope(str, Enum):
    shadow = "shadow"
    live = "live"
    all = "all"


class TraderPositionCleanupMethod(str, Enum):
    mark_to_market = "mark_to_market"
    cancel = "cancel"


class TraderPositionCleanupRequest(BaseModel):
    scope: TraderPositionCleanupScope = TraderPositionCleanupScope.shadow
    method: TraderPositionCleanupMethod = TraderPositionCleanupMethod.mark_to_market
    max_age_hours: Optional[int] = Field(default=None, ge=1, le=24 * 365)
    dry_run: bool = False
    target_status: str = Field(default="cancelled", min_length=1, max_length=64)
    reason: Optional[str] = None
    confirm_live: bool = False


class TraderLiveWalletPositionAdoptRequest(BaseModel):
    token_id: str = Field(..., min_length=1, max_length=256)
    reason: Optional[str] = None
    requested_by: Optional[str] = None


class TraderExecutionSessionControlRequest(BaseModel):
    reason: Optional[str] = None


class TraderTuneAgentRequest(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=12000)
    model: Optional[str] = Field(default=None, max_length=200)
    max_iterations: int = Field(default=12, ge=1, le=24)
    monitor_job_id: Optional[str] = Field(default=None, max_length=120)


class TraderOrderManualCloseRequest(BaseModel):
    requested_by: Optional[str] = None
    reason: Optional[str] = None


class ManualBuyPosition(BaseModel):
    token_id: str
    side: str = Field(default="BUY")
    price: float = Field(ge=0.01, le=0.99)
    market_id: str = ""
    market_question: str = ""
    outcome: str = ""
    direction: Optional[str] = None


class TraderManualBuyRequest(BaseModel):
    positions: list[ManualBuyPosition] = Field(..., min_length=1)
    size_usd: float = Field(..., gt=0)
    opportunity_id: Optional[str] = None
    client_request_id: str = Field(..., min_length=8, max_length=128)
    order_type: str = Field(default="market", pattern="^(market|limit)$")


def _manual_position_outcome_index(position: dict[str, Any], market: dict[str, Any]) -> int:
    token_id = str(position.get("token_id") or "").strip()
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        for index, token in enumerate(tokens):
            if not isinstance(token, dict):
                continue
            candidate = str(token.get("token_id") or token.get("tokenId") or "").strip()
            if token_id and candidate == token_id:
                return index

    outcome = str(position.get("outcome") or "").strip().lower()
    if outcome == "yes":
        return 0
    if outcome == "no":
        return 1
    raise HTTPException(
        status_code=409,
        detail="Opportunity position does not carry an unambiguous binary outcome identity.",
    )


def _build_manual_runtime_signal(
    *,
    opportunity: Any,
    request: TraderManualBuyRequest,
    trader_id: str,
    request_fingerprint: str,
) -> Any:
    authoritative_positions = [
        dict(position)
        for position in list(getattr(opportunity, "positions_to_take", None) or [])
        if isinstance(position, dict)
    ]
    if not authoritative_positions:
        raise HTTPException(status_code=409, detail="Opportunity has no executable positions.")
    if len(authoritative_positions) != len(request.positions):
        raise HTTPException(status_code=409, detail="Opportunity position count changed; refresh before trading.")

    markets = [
        dict(market)
        for market in list(getattr(opportunity, "markets", None) or [])
        if isinstance(market, dict)
    ]
    markets_by_id: dict[str, dict[str, Any]] = {}
    for market in markets:
        for candidate in (market.get("id"), market.get("condition_id"), market.get("conditionId")):
            key = str(candidate or "").strip()
            if key:
                markets_by_id[key] = market

    legs: list[dict[str, Any]] = []
    for index, (position, client_position) in enumerate(zip(authoritative_positions, request.positions, strict=True)):
        action = str(position.get("action") or "BUY").strip().upper()
        if action != "BUY":
            raise HTTPException(
                status_code=409,
                detail="Manual buy does not execute SELL/close positions; use the position close workflow.",
            )

        token_id = str(position.get("token_id") or "").strip()
        if not token_id:
            raise HTTPException(status_code=409, detail="Opportunity is missing its executable token_id.")
        client_token_id = str(client_position.token_id or "").strip()
        if client_token_id and client_token_id != token_id:
            raise HTTPException(status_code=409, detail="Opportunity token changed; refresh before trading.")

        market_id = str(position.get("market_id") or "").strip()
        market = markets_by_id.get(market_id)
        if market is None and len(markets) == 1:
            market = markets[0]
        if market is None:
            raise HTTPException(status_code=409, detail="Opportunity market identity is incomplete.")
        resolved_market_id = str(market.get("id") or market_id or "").strip()
        client_market_id = str(client_position.market_id or "").strip()
        valid_market_ids = {
            value
            for value in (
                resolved_market_id,
                str(market.get("condition_id") or market.get("conditionId") or "").strip(),
            )
            if value
        }
        if client_market_id and client_market_id not in valid_market_ids:
            raise HTTPException(status_code=409, detail="Opportunity market changed; refresh before trading.")

        outcome_index = _manual_position_outcome_index(position, market)
        if outcome_index not in {0, 1}:
            raise HTTPException(status_code=409, detail="Only binary outcome positions are supported.")
        canonical_direction = "buy_yes" if outcome_index == 0 else "buy_no"
        requested_direction = str(client_position.direction or "").strip().lower()
        if requested_direction and requested_direction != canonical_direction:
            raise HTTPException(status_code=409, detail="Opportunity direction changed; refresh before trading.")

        authoritative_price = float(position.get("price") or 0.0)
        limit_price = float(client_position.price) if request.order_type == "limit" else authoritative_price
        if not 0.01 <= limit_price <= 0.99:
            raise HTTPException(status_code=409, detail="Opportunity price is outside the executable range.")
        market_question = str(
            position.get("market")
            or market.get("question")
            or getattr(opportunity, "title", "")
            or resolved_market_id
        ).strip()
        condition_id = str(market.get("condition_id") or market.get("conditionId") or "").strip()
        legs.append(
            {
                "leg_id": f"manual_leg_{index + 1}",
                "market_id": resolved_market_id,
                "market_question": market_question,
                "token_id": token_id,
                "side": "buy",
                "outcome": "yes" if outcome_index == 0 else "no",
                "direction": canonical_direction,
                "limit_price": limit_price,
                "price_policy": "maker_limit" if request.order_type == "limit" else "taker_limit",
                "time_in_force": "GTC" if request.order_type == "limit" else "IOC",
                "post_only": request.order_type == "limit",
                "notional_weight": 1.0,
                "min_fill_ratio": 0.0,
                "metadata": {
                    "condition_id": condition_id or None,
                    "outcome_index": outcome_index,
                    "manual_request_id": request.client_request_id,
                },
            }
        )

    first_leg = legs[0]
    strategy_context = dict(getattr(opportunity, "strategy_context", None) or {})
    source = str(strategy_context.get("source_key") or "scanner").strip().lower() or "scanner"
    opportunity_payload = (
        opportunity.model_dump(mode="json")
        if hasattr(opportunity, "model_dump")
        else {}
    )
    signal_payload = dict(opportunity_payload)
    signal_payload.update(
        {
            "selected_token_id": first_leg["token_id"],
            "selected_outcome_index": first_leg["metadata"]["outcome_index"],
            "execution_plan": {
                "plan_id": f"manual_{request.client_request_id}",
                "policy": "PARALLEL_MAKER",
                "time_in_force": first_leg["time_in_force"],
                "legs": legs,
                "metadata": {
                    "manual": True,
                    "manual_request_id": request.client_request_id,
                    "trader_id": trader_id,
                },
            },
            "manual_execution": {
                "request_id": request.client_request_id,
                "request_fingerprint": request_fingerprint,
                "order_type": request.order_type,
            },
        }
    )
    return SimpleNamespace(
        id=str(getattr(opportunity, "id", "") or request.opportunity_id or ""),
        source=source,
        signal_type="manual_buy",
        strategy_type=str(getattr(opportunity, "strategy", "") or "manual_buy"),
        market_id=str(first_leg["market_id"]),
        market_question=str(first_leg["market_question"]),
        direction=str(first_leg["direction"]),
        entry_price=float(first_leg["limit_price"]),
        effective_price=None,
        edge_percent=getattr(opportunity, "edge_percent", None),
        confidence=getattr(opportunity, "confidence", None),
        payload_json=signal_payload,
        strategy_context_json=strategy_context,
        trace_id=None,
    )


def _manual_buy_request_fingerprint(trader_id: str, request: TraderManualBuyRequest) -> str:
    payload = {
        "trader_id": str(trader_id or "").strip(),
        "opportunity_id": str(request.opportunity_id or "").strip(),
        "client_request_id": str(request.client_request_id or "").strip(),
        "order_type": str(request.order_type or "market").strip().lower(),
        "size_usd": float(request.size_usd),
        "positions": [position.model_dump(mode="json") for position in request.positions],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _manual_buy_decision_id(trader_id: str, client_request_id: str) -> str:
    identity = f"homerun:manual-buy:{str(trader_id or '').strip()}:{str(client_request_id or '').strip()}"
    return uuid.uuid5(uuid.NAMESPACE_URL, identity).hex


async def _read_current_manual_opportunity(session: AsyncSession, opportunity_id: str) -> Any:
    normalized_id = str(opportunity_id or "").strip()
    if not normalized_id:
        raise HTTPException(status_code=422, detail="opportunity_id is required for manual execution.")

    trader_opportunities, _ = await scanner_shared_state.read_traders_snapshot(session)
    for opportunity in trader_opportunities:
        if str(getattr(opportunity, "id", "") or "").strip() == normalized_id:
            return opportunity

    market_opportunities, _ = await scanner_shared_state.read_scanner_snapshot(session)
    for opportunity in market_opportunities:
        if str(getattr(opportunity, "id", "") or "").strip() == normalized_id:
            return opportunity
    raise HTTPException(status_code=404, detail=f"Current opportunity not found: {normalized_id}")


def _manual_shadow_account_id(control: dict[str, Any], trader: dict[str, Any]) -> str:
    settings_payload = control.get("settings")
    settings_payload = settings_payload if isinstance(settings_payload, dict) else {}
    metadata_payload = trader.get("metadata")
    metadata_payload = metadata_payload if isinstance(metadata_payload, dict) else {}
    for candidate in (
        settings_payload.get("shadow_account_id"),
        metadata_payload.get("shadow_account_id"),
    ):
        account_id = str(candidate or "").strip()
        if account_id:
            return account_id
    return ""


def _serialize_manual_order(order: TraderOrder) -> dict[str, Any]:
    payload = dict(order.payload_json or {})
    effective_price = float(order.effective_price or order.entry_price or 0.0)
    notional_usd = float(order.notional_usd or 0.0)
    size_shares = notional_usd / effective_price if effective_price > 0.0 else 0.0
    return {
        "order_id": str(order.id),
        "market_id": str(order.market_id or ""),
        "market_question": str(order.market_question or ""),
        "direction": str(order.direction or ""),
        "status": str(order.status or ""),
        "notional_usd": notional_usd,
        "entry_price": float(order.entry_price or 0.0),
        "effective_price": effective_price,
        "size_shares": size_shares,
        "error": order.error_message,
        "simulation_ledger": (
            dict(payload.get("simulation_ledger"))
            if isinstance(payload.get("simulation_ledger"), dict)
            else None
        ),
    }


async def _manual_buy_response_for_decision(
    session: AsyncSession,
    *,
    trader_id: str,
    decision: TraderDecision,
    request_fingerprint: str,
    expected_mode: str,
    expected_shadow_account_id: str | None,
) -> dict[str, Any]:
    decision_payload = dict(decision.payload_json or {})
    manual_payload = decision_payload.get("manual_execution")
    manual_payload = manual_payload if isinstance(manual_payload, dict) else {}
    persisted_fingerprint = str(manual_payload.get("request_fingerprint") or "").strip()
    if persisted_fingerprint and persisted_fingerprint != request_fingerprint:
        raise HTTPException(status_code=409, detail="client_request_id was already used for a different request.")

    order_rows = list(
        (
            await session.execute(
                select(TraderOrder)
                .where(TraderOrder.trader_id == trader_id, TraderOrder.decision_id == decision.id)
                .order_by(TraderOrder.created_at.asc(), TraderOrder.id.asc())
            )
        )
        .scalars()
        .all()
    )
    serialized_orders = [_serialize_manual_order(order) for order in order_rows]
    session_id = str(decision_payload.get("execution_session_id") or "").strip()
    if not session_id:
        session_id = str(
            (
                await session.execute(
                    select(ExecutionSession.id)
                    .where(ExecutionSession.decision_id == decision.id)
                    .order_by(ExecutionSession.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            or ""
        ).strip()

    mode = str(expected_mode or "shadow").strip().lower()
    if serialized_orders:
        if mode == "shadow":
            decision_shadow_account_id = str(
                manual_payload.get("shadow_account_id")
                or expected_shadow_account_id
                or ""
            ).strip()
            ledger_rows = [
                order.get("simulation_ledger")
                for order in serialized_orders
                if float(order.get("notional_usd") or 0.0) > 0.0
            ]
            if ledger_rows and all(isinstance(ledger, dict) and ledger.get("account_id") for ledger in ledger_rows):
                ledger_account_ids = {
                    str(ledger.get("account_id") or "").strip()
                    for ledger in ledger_rows
                    if isinstance(ledger, dict)
                }
                if len(ledger_account_ids) != 1 or (
                    decision_shadow_account_id
                    and ledger_account_ids != {decision_shadow_account_id}
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Manual execution ledger was recorded against a different Shadow account.",
                    )
                account_id = next(iter(ledger_account_ids))
                return {
                    "status": "success",
                    "trader_id": trader_id,
                    "mode": mode,
                    "decision_id": str(decision.id),
                    "session_id": session_id or None,
                    "account_id": account_id or None,
                    "orders": serialized_orders,
                    "message": f"{len(serialized_orders)} shadow order(s) executed and recorded",
                }
        else:
            live_success_statuses = {"executed", "open", "working", "submitted", "partially_filled", "placing"}
            if any(str(order.get("status") or "").strip().lower() in live_success_statuses for order in serialized_orders):
                return {
                    "status": "success",
                    "trader_id": trader_id,
                    "mode": mode,
                    "decision_id": str(decision.id),
                    "session_id": session_id or None,
                    "account_id": None,
                    "orders": serialized_orders,
                    "message": f"{len(serialized_orders)} live order(s) accepted by the execution session",
                }

    decision_status = str(decision.decision or "").strip().lower()
    detail = str(decision.reason or "").strip() or "Manual execution has not produced a committed fill."
    if decision_status in {"failed", "skipped", "blocked"}:
        raise HTTPException(status_code=409, detail=detail)
    raise HTTPException(status_code=409, detail="The same manual request is already in progress.")


class TraderStartRequest(BaseModel):
    copy_existing_positions: bool | None = None
    requested_by: Optional[str] = None


class TraderActivationRequest(BaseModel):
    requested_by: Optional[str] = None
    reason: Optional[str] = None


class TraderStopLifecycleMode(str, Enum):
    keep_positions = "keep_positions"
    close_shadow_positions = "close_shadow_positions"
    close_all_positions = "close_all_positions"


class TraderStopRequest(BaseModel):
    stop_lifecycle: TraderStopLifecycleMode = TraderStopLifecycleMode.keep_positions
    confirm_live: bool = False
    requested_by: Optional[str] = None
    reason: Optional[str] = None


def _collect_market_aliases(raw_market: Any) -> list[str]:
    if not isinstance(raw_market, dict):
        return []
    aliases: list[str] = []
    for candidate in (
        raw_market.get("id"),
        raw_market.get("market_id"),
        raw_market.get("condition_id"),
        raw_market.get("conditionId"),
        raw_market.get("slug"),
        raw_market.get("market_slug"),
        raw_market.get("marketSlug"),
        raw_market.get("event_slug"),
        raw_market.get("eventSlug"),
        raw_market.get("event_ticker"),
        raw_market.get("eventTicker"),
        raw_market.get("ticker"),
        raw_market.get("token_id"),
        raw_market.get("tokenId"),
        raw_market.get("selected_token_id"),
        raw_market.get("selectedTokenId"),
        raw_market.get("asset_id"),
        raw_market.get("assetId"),
        raw_market.get("yes_token_id"),
        raw_market.get("no_token_id"),
    ):
        normalized = normalize_market_id(candidate)
        if normalized and normalized not in aliases:
            aliases.append(normalized)

    for key in ("clob_token_ids", "clobTokenIds", "token_ids", "tokenIds"):
        values = raw_market.get(key)
        if not isinstance(values, list):
            continue
        for candidate in values:
            normalized = normalize_market_id(candidate)
            if normalized and normalized not in aliases:
                aliases.append(normalized)

    tokens = raw_market.get("tokens")
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            for candidate in (
                token.get("token_id"),
                token.get("tokenId"),
                token.get("asset_id"),
                token.get("assetId"),
                token.get("id"),
            ):
                normalized = normalize_market_id(candidate)
                if normalized and normalized not in aliases:
                    aliases.append(normalized)
    return aliases


def _merge_normalized_binary_history(
    existing: list[dict[str, float]],
    incoming: list[dict[str, float]],
    limit: int,
) -> list[dict[str, float]]:
    if not existing:
        return incoming[-limit:]
    if not incoming:
        return existing[-limit:]

    merged_by_ts: dict[int, dict[str, float]] = {}
    for point in existing:
        try:
            ts_ms = int(float(point.get("t", 0)))
        except Exception:
            continue
        if ts_ms <= 0:
            continue
        merged_by_ts[ts_ms] = point

    for point in incoming:
        try:
            ts_ms = int(float(point.get("t", 0)))
        except Exception:
            continue
        if ts_ms <= 0:
            continue
        merged_by_ts[ts_ms] = point

    merged = [merged_by_ts[key] for key in sorted(merged_by_ts.keys())]
    return merged[-limit:]


def _ensure_normalized_market_history(
    normalized_history: dict[str, list[dict[str, float]]],
    raw_history_lookup: dict[str, Any],
    market_ids: list[str],
    limit: int,
    *,
    now_ms: int,
    window_seconds: int,
    max_points: int,
) -> None:
    for market_id in market_ids:
        normalized_market_id = normalize_market_id(market_id)
        if not normalized_market_id or normalized_market_id in normalized_history:
            continue
        raw_points = raw_history_lookup.get(normalized_market_id)
        if raw_points is None:
            continue
        normalized_points = normalize_binary_price_history(
            raw_points,
            now_ms=now_ms,
            window_seconds=window_seconds,
            max_points=max_points,
        )
        if len(normalized_points) >= 2:
            normalized_history[normalized_market_id] = normalized_points[-limit:]


def _bind_market_payload_history(
    raw_market: Any,
    normalized_history: dict[str, list[dict[str, float]]],
    raw_history_lookup: dict[str, Any],
    alias_to_history_key: dict[str, str],
    limit: int,
    *,
    keep_existing_aliases: bool = False,
    now_ms: int | None = None,
    window_seconds: int | None = None,
    max_points: int | None = None,
) -> None:
    if not isinstance(raw_market, dict):
        return

    aliases = _collect_market_aliases(raw_market)
    if not aliases:
        return

    if now_ms is not None and window_seconds is not None and max_points is not None:
        _ensure_normalized_market_history(
            normalized_history,
            raw_history_lookup,
            aliases,
            limit,
            now_ms=int(now_ms),
            window_seconds=max(60, int(window_seconds)),
            max_points=max(2, int(max_points)),
        )

    history_key = next((alias for alias in aliases if alias in normalized_history), None)
    normalize_kwargs: dict[str, Any] = {}
    if now_ms is not None:
        normalize_kwargs["now_ms"] = int(now_ms)
    if window_seconds is not None:
        normalize_kwargs["window_seconds"] = max(60, int(window_seconds))
    if max_points is not None:
        normalize_kwargs["max_points"] = max(2, int(max_points))
    normalized_points = normalize_binary_price_history(raw_market.get("price_history"), **normalize_kwargs)
    if len(normalized_points) >= 2:
        resolved_history_key = history_key or aliases[0]
        merged = _merge_normalized_binary_history(
            normalized_history.get(resolved_history_key, []),
            normalized_points,
            limit,
        )
        if len(merged) >= 2:
            normalized_history[resolved_history_key] = merged
            history_key = resolved_history_key

    if history_key:
        for alias in aliases:
            if keep_existing_aliases and alias in alias_to_history_key:
                continue
            alias_to_history_key[alias] = history_key


def _assert_not_globally_paused() -> None:
    if global_pause_state.is_paused:
        raise HTTPException(
            status_code=409,
            detail="Global pause is active. Use /workers/resume-all first.",
        )


def _apply_loss_streak_reset_metadata(
    metadata: dict[str, Any],
    *,
    reason: str,
) -> tuple[dict[str, Any], str]:
    stamped = dict(metadata or {})
    reset_at = to_iso(datetime.now(timezone.utc))
    if reset_at:
        stamped[_LOSS_STREAK_RESET_AT_KEY] = reset_at
    stamped[_LOSS_STREAK_RESET_REASON_KEY] = reason
    return stamped, reset_at or ""


@router.get("")
async def get_all_traders(
    mode: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
):
    mode_key = str(mode or "").strip().lower()
    if mode_key and mode_key not in {"shadow", "live"}:
        raise HTTPException(status_code=422, detail="mode must be 'shadow' or 'live'")
    return {"traders": await list_traders(session, mode=mode_key or None)}


@router.get("/templates")
async def get_templates():
    return {"templates": list_trader_templates()}


@router.post("/from-template")
async def create_from_template(
    request: TraderTemplateCreateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        trader = await create_trader_from_template(
            session,
            template_id=request.template_id,
            overrides=request.overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    _invalidate_trader_strategy_caches()

    await create_trader_event(
        session,
        trader_id=trader["id"],
        event_type="trader_created",
        source="operator",
        operator=request.requested_by,
        message="Trader created from template",
        payload={"template_id": request.template_id},
    )
    return trader


@router.post("")
async def create_trader_route(
    request: TraderRequest,
    session: AsyncSession = Depends(get_db_session),
):
    payload = request.model_dump(exclude_unset=True, exclude={"requested_by", "reason"})
    try:
        trader = await create_trader(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    _invalidate_trader_strategy_caches()

    await create_config_revision(
        session,
        trader_id=trader["id"],
        operator=request.requested_by,
        reason=request.reason or "trader_create",
        orchestrator_before={},
        orchestrator_after={},
        trader_before={},
        trader_after=trader,
    )
    copy_from_trader_id = str(request.copy_from_trader_id or "").strip() or None
    event_payload: dict[str, Any] = {"trader": trader}
    event_message = "Trader created"
    if copy_from_trader_id:
        event_payload["copy_from_trader_id"] = copy_from_trader_id
        event_message = "Trader created from existing trader settings"
    await create_trader_event(
        session,
        trader_id=trader["id"],
        event_type="trader_created",
        source="operator",
        operator=request.requested_by,
        message=event_message,
        payload=event_payload,
    )
    return trader


@router.get("/orders/all")
async def get_all_trader_orders_all(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    since_seconds: int = Query(
        default=14 * 24 * 3600,
        ge=0,
        le=365 * 24 * 3600,
        description=(
            "Cap the historical window by age in seconds (default 14 days, "
            "matching the bots-overview chart window).  Open orders are "
            "always returned unbounded; only the paginated historical tail "
            "is date-filtered.  Pass 0 to disable the cap."
        ),
    ),
):
    # Cache key includes pagination + status + window so different
    # views don't cross-contaminate.  See ``_TTLCache`` above for why
    # this route does not declare a session dependency.
    cache_key = f"{(status or '').strip().lower()}|{int(limit)}|{int(offset)}|{int(since_seconds)}"

    async def _load():
        async with AsyncSessionLocal() as session:
            orders = await list_serialized_trader_orders(
                session,
                trader_id=None,
                status=status,
                limit=limit,
                offset=offset,
                since_seconds=int(since_seconds) if since_seconds > 0 else None,
            )
        return {
            "orders": orders,
            "limit": limit,
            "offset": offset,
        }

    return await _orders_all_cache.get_or_load(cache_key, _load)


@router.get("/orders/pnl-breakdown")
async def get_orders_pnl_breakdown(
    group_by: str = Query(
        default="strategy_version",
        description="Group field: strategy_version, close_trigger, direction, or status",
    ),
    strategy_key: Optional[str] = Query(default=None),
    since_seconds: int = Query(default=0, ge=0, le=365 * 24 * 3600),
    limit: int = Query(default=5000, ge=1, le=20000),
):
    """Realized NET PnL grouped by a configuration dimension.

    Computes net per terminal order defensively from the cost basis and the
    win/loss settlement, so it is correct even if a stored ``actual_profit``
    was written by a legacy (gross/notional) path:

    * ``resolved_win``  -> shares*$1 - cost   (held to settlement)
    * ``resolved_loss`` -> -cost
    * ``closed_*``      -> stored actual_profit clamped into [-cost, shares-cost]

    Use to find which strategy versions / exit triggers actually make money
    (e.g. tail_end_carry's edge lives in held-to-resolution, stops bleed it).
    """
    from utils.pnl import TERMINAL_STATES, canonical_terminal_net_pnl

    allowed = {"strategy_version", "close_trigger", "direction", "status"}
    key = group_by if group_by in allowed else "strategy_version"

    async with AsyncSessionLocal() as session:
        orders = await list_serialized_trader_orders(
            session,
            trader_id=None,
            status="all",
            limit=limit,
            offset=0,
            since_seconds=int(since_seconds) if since_seconds > 0 else None,
        )

    def _f(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    buckets: dict[str, dict[str, Any]] = {}
    grand = {"n": 0, "net": 0.0, "wins": 0, "losses": 0}
    for o in orders:
        if strategy_key and str(o.get("strategy_key") or "") != strategy_key:
            continue
        status = str(o.get("status") or "")
        if status not in TERMINAL_STATES:
            continue
        entry = _f(o.get("entry_price"))
        cost = _f(o.get("filled_notional_usd")) or _f(o.get("notional_usd"))
        shares = _f(o.get("filled_shares")) or (cost / entry if entry > 0 else 0.0)
        net = canonical_terminal_net_pnl(status, cost, shares, stored_pnl=_f(o.get("actual_profit")))
        if net is None:
            continue
        gk = str(o.get(key))
        b = buckets.setdefault(gk, {"n": 0, "net": 0.0, "wins": 0, "losses": 0})
        for tgt in (b, grand):
            tgt["n"] += 1
            tgt["net"] += net
            if net > 0:
                tgt["wins"] += 1
            elif net < 0:
                tgt["losses"] += 1

    rows = []
    for gk, b in buckets.items():
        rows.append({
            "group": gk,
            "trades": b["n"],
            "net_pnl": round(b["net"], 2),
            "avg_pnl": round(b["net"] / b["n"], 4) if b["n"] else 0.0,
            "win_rate": round(100.0 * b["wins"] / b["n"], 1) if b["n"] else 0.0,
        })
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    return {
        "group_by": key,
        "strategy_key": strategy_key,
        "since_seconds": since_seconds,
        "total_trades": grand["n"],
        "total_net_pnl": round(grand["net"], 2),
        "overall_win_rate": round(100.0 * grand["wins"] / grand["n"], 1) if grand["n"] else 0.0,
        "groups": rows,
    }


# Process-local TTL cache shared by the bots-overview / left-sidebar
# endpoints.  Three of the routes below are polled by the dashboard on
# a few-second cadence and each underlying query pulls full rows that
# include multi-MB ``json`` columns (``payload_json``, ``events_json``
# style blobs).  asyncpg decodes the JSON on the event loop, so each
# call costs ~1-2s of connection-wall time and contributes to the
# cumulative loop contention that triggers asyncpg "cannot switch to
# state X" tears on the trader fast-tier path.
#
# A short TTL cache, well under the dashboard's perceptible refresh
# threshold, collapses dozens of concurrent and back-to-back polls
# into a single DB read.  Per-key asyncio.Lock coalesces in-flight
# misses (only the first poll in a window does the DB query; others
# wait on the lock and pick up the populated entry).
class _TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_or_load(self, key: str, fetcher):
        cached = self._entries.get(key)
        if cached is not None and (time.monotonic() - cached["loaded_at_mono"]) < self._ttl:
            return cached["payload"]
        lock = await self._lock_for(key)
        async with lock:
            cached = self._entries.get(key)
            if cached is not None and (time.monotonic() - cached["loaded_at_mono"]) < self._ttl:
                return cached["payload"]
            payload = await fetcher()
            self._entries[key] = {
                "payload": payload,
                "loaded_at_mono": time.monotonic(),
            }
            return payload


# TTLs sized below the dashboard's actual poll cadence so the cache
# is invisible: /orders/summary (UI polls every 10s) caches 3s,
# /orders/all (UI polls every 8s) caches 5s, /events/all (UI polls
# every 30s) caches 10s.  Every response is at most TTL seconds older
# than what the user would have seen without caching, far below the
# perceptible refresh threshold for an aggregate-stats dashboard.
_orders_summary_cache = _TTLCache(3.0)
_orders_all_cache = _TTLCache(5.0)
_events_all_cache = _TTLCache(10.0)


@router.get("/orders/summary")
async def get_trader_orders_summary(
    mode: Optional[str] = Query(default=None),
):
    # Note: these cache-fronted routes deliberately do not declare a
    # ``session`` dependency.  The cache-hit path returns without
    # touching the DB at all — declaring ``session: AsyncSession =
    # Depends(...)`` would acquire a pool connection just to discard
    # it, undoing part of the point of caching.  The miss path opens
    # its own session inline.
    from services.trader_orchestrator_state import (
        get_trader_orders_summary as _get_trader_orders_summary_db,
    )

    mode_key = (mode or "").strip().lower()

    async def _load():
        async with AsyncSessionLocal() as session:
            return await _get_trader_orders_summary_db(session, mode=mode)

    return await _orders_summary_cache.get_or_load(mode_key, _load)


@router.get("/events/all")
async def get_all_trader_events_bulk(
    trader_ids: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    types: Optional[str] = Query(default=None),
):
    from services.trader_orchestrator_state import list_serialized_trader_events_bulk
    normalized_trader_ids: list[str] | None = None
    if trader_ids:
        seen: set[str] = set()
        ids: list[str] = []
        for raw in str(trader_ids).split(","):
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            ids.append(value)
        normalized_trader_ids = ids or None
    event_types: list[str] | None = None
    if types:
        event_types = [t.strip() for t in str(types).split(",") if t.strip()]

    # Cache key includes the normalized trader_ids and types so a
    # filtered view doesn't override the all-traders view in cache.
    cache_key = "|".join(
        [
            ",".join(sorted(normalized_trader_ids)) if normalized_trader_ids else "",
            ",".join(sorted(event_types)) if event_types else "",
            str(int(limit)),
        ]
    )

    async def _load():
        async with AsyncSessionLocal() as session:
            events = await list_serialized_trader_events_bulk(
                session,
                trader_ids=normalized_trader_ids,
                limit=limit,
                event_types=event_types or None,
            )
        return {"events": events}

    return await _events_all_cache.get_or_load(cache_key, _load)


@router.get("/firehose/recent")
async def get_recent_firehose_events(
    source_key: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
):
    """Replay recent firehose telemetry from the capped Redis Stream.

    Firehose events (gate evaluations / rejections / emits) are
    ephemeral, loss-tolerant telemetry.  They are NOT persisted to
    Postgres — they live only in Redis: pub/sub for the live terminal
    feed, plus a capped Stream (``trader_firehose``, MAXLEN ~50k) for
    bounded reload history.  This endpoint reads that Stream so the
    terminal can repopulate scrollback after a page reload.

    Each Stream entry stores the same serialized event shape that is
    PUBLISH'd to the live feed under the ``data`` field.  Returns
    newest-first, optionally filtered to a single ``source_key``.
    """
    import json as _json

    from services import redis_client
    from services.trader_hot_state import _FIREHOSE_STREAM

    # Over-fetch when filtering so a source_key with sparse traffic can
    # still return up to ``limit`` matches from the shared stream.
    fetch_count = min(2000, limit * 4) if source_key else limit
    raw = await redis_client.safe_xrevrange(_FIREHOSE_STREAM, count=fetch_count)

    wanted_source = str(source_key).strip() if source_key else None
    events: list[dict[str, Any]] = []
    for _entry_id, fields in raw:
        data = fields.get("data")
        if not data:
            continue
        try:
            parsed = _json.loads(data)
        except (ValueError, TypeError):
            continue
        if wanted_source is not None:
            payload = parsed.get("payload") or {}
            if str(payload.get("source_key") or "") != wanted_source:
                continue
        events.append(parsed)
        if len(events) >= limit:
            break

    return {"events": events}


@router.get("/decisions/all")
async def get_all_trader_decisions_all(
    decision: Optional[str] = Query(default=None),
    trader_ids: Optional[str] = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=5000),
    per_trader_limit: int = Query(default=160, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
):
    normalized_trader_ids: list[str] = []
    if trader_ids:
        seen: set[str] = set()
        for raw in str(trader_ids).split(","):
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized_trader_ids.append(value)
    return {
        "decisions": await list_serialized_trader_decisions(
            session,
            trader_id=None,
            trader_ids=normalized_trader_ids or None,
            decision=decision,
            limit=limit,
            per_trader_limit=per_trader_limit if normalized_trader_ids else None,
        )
    }


@router.get("/market-history")
async def get_trader_market_history(
    market_ids: str = Query(default=""),
    limit: int = Query(default=120, ge=2, le=5000),
    session: AsyncSession = Depends(get_db_session),
):
    requested_ids: list[str] = []
    seen: set[str] = set()
    for raw in str(market_ids or "").split(","):
        normalized = normalize_market_id(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        requested_ids.append(normalized)

    if not requested_ids:
        return {"histories": {}, "updated_at": None}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Use a wider window for the position modal than the scanner sparklines.
    # The modal has 7d/3d/24h presets, so we need at least 7 days of data.
    scanner_window_seconds = max(7 * 24 * 3600, int(getattr(settings, "SCANNER_SPARKLINE_WINDOW_HOURS", 24)) * 3600)
    scanner_max_points = max(2, int(getattr(settings, "SCANNER_SPARKLINE_MAX_POINTS", 960)))
    # For the position modal, allow many more points than the sparkline setting.
    # The frontend requests limit=960 but the sparkline setting may be lower (e.g. 180).
    # Use at least 960 points to fill the 7d/3d/24h chart windows properly.
    normalize_max_points = max(limit, scanner_max_points, 960)

    history_map = await scanner_shared_state.read_scanner_market_history(
        session, market_ids=requested_ids
    )
    opportunities = await scanner_shared_state.read_active_opportunity_payloads(session)
    # Reuse the cached, off-loop catalog reader rather than a direct
    # ``select(MarketCatalog.markets_json)`` — the latter forces asyncpg
    # to ``json.loads`` the multi-MB blob on the event loop.
    _events, market_catalog, _meta = await scanner_shared_state.read_market_catalog(
        session,
        include_events=False,
        include_markets=True,
        validate=False,
    )
    if not isinstance(market_catalog, list):
        market_catalog = []

    raw_history_lookup: dict[str, Any] = {}
    for raw_market_id, raw_points in history_map.items():
        normalized_market_id = normalize_market_id(raw_market_id)
        if not normalized_market_id:
            continue
        if normalized_market_id not in raw_history_lookup:
            raw_history_lookup[normalized_market_id] = raw_points

    normalized_history: dict[str, list[dict[str, float]]] = {}
    _ensure_normalized_market_history(
        normalized_history,
        raw_history_lookup,
        requested_ids,
        limit,
        now_ms=now_ms,
        window_seconds=scanner_window_seconds,
        max_points=normalize_max_points,
    )

    alias_to_history_key: dict[str, str] = {}
    catalog_by_alias: dict[str, dict[str, Any]] = {}
    for raw_opportunity in opportunities:
        if not isinstance(raw_opportunity, dict):
            continue
        markets = raw_opportunity.get("markets")
        if not isinstance(markets, list):
            continue
        for raw_market in markets:
            _bind_market_payload_history(
                raw_market,
                normalized_history,
                raw_history_lookup,
                alias_to_history_key,
                limit,
                now_ms=now_ms,
                window_seconds=scanner_window_seconds,
                max_points=normalize_max_points,
            )

    for raw_market in market_catalog:
        if not isinstance(raw_market, dict):
            continue
        _bind_market_payload_history(
            raw_market,
            normalized_history,
            raw_history_lookup,
            alias_to_history_key,
            limit,
            keep_existing_aliases=True,
            now_ms=now_ms,
            window_seconds=scanner_window_seconds,
            max_points=normalize_max_points,
        )
        aliases = _collect_market_aliases(raw_market)
        for alias in aliases:
            if alias not in catalog_by_alias:
                catalog_by_alias[alias] = raw_market

    unresolved_catalog_ids = [
        market_id
        for market_id in requested_ids
        if market_id not in catalog_by_alias
    ]
    if unresolved_catalog_ids:
        try:
            from services.polymarket import polymarket_client
        except Exception:
            polymarket_client = None  # type: ignore[assignment]
        if polymarket_client is not None:
            for market_id in unresolved_catalog_ids:
                try:
                    if market_id.startswith("0x"):
                        resolved_market = await polymarket_client.get_market_by_condition_id(market_id)
                    else:
                        resolved_market = await polymarket_client.get_market_by_token_id(market_id)
                except Exception:
                    resolved_market = None
                if not isinstance(resolved_market, dict):
                    continue
                _bind_market_payload_history(
                    resolved_market,
                    normalized_history,
                    raw_history_lookup,
                    alias_to_history_key,
                    limit,
                    keep_existing_aliases=True,
                    now_ms=now_ms,
                    window_seconds=scanner_window_seconds,
                    max_points=normalize_max_points,
                )
                for alias in _collect_market_aliases(resolved_market):
                    if alias not in catalog_by_alias:
                        catalog_by_alias[alias] = resolved_market

    backfill_market_ids = list(requested_ids)

    if backfill_market_ids:
        try:
            from models.opportunity import Opportunity
            from services.scanner import scanner as market_scanner
        except Exception:
            market_scanner = None
            Opportunity = None  # type: ignore[assignment]

        if market_scanner is not None and Opportunity is not None:
            backfill_targets: list[Any] = []
            for market_id in backfill_market_ids:
                catalog_market = catalog_by_alias.get(market_id)
                market_payload: dict[str, Any] = {
                    "id": market_id,
                }
                if isinstance(catalog_market, dict):
                    for key in (
                        "market_id",
                        "condition_id",
                        "conditionId",
                        "slug",
                        "market_slug",
                        "marketSlug",
                        "event_slug",
                        "eventSlug",
                        "event_ticker",
                        "eventTicker",
                        "ticker",
                        "yes_price",
                        "no_price",
                        "clob_token_ids",
                        "clobTokenIds",
                        "token_ids",
                        "tokenIds",
                        "tokens",
                    ):
                        value = catalog_market.get(key)
                        if value is not None:
                            market_payload[key] = value

                market_payload["platform"] = infer_market_platform(market_payload)
                backfill_targets.append(
                    Opportunity(
                        strategy="trader_history",
                        title=str(market_payload.get("question") or market_id),
                        description="Trader modal market history hydration",
                        total_cost=0.0,
                        expected_payout=1.0,
                        gross_profit=0.0,
                        fee=0.0,
                        net_profit=0.0,
                        roi_percent=0.0,
                        risk_score=0.5,
                        confidence=0.5,
                        markets=[market_payload],
                        min_liquidity=0.0,
                        max_position_size=0.0,
                        positions_to_take=[],
                    )
                )

            if backfill_targets:
                try:
                    backfill_attempt_map = getattr(market_scanner, "_market_history_backfill_attempt_ms", None)
                    if isinstance(backfill_attempt_map, dict):
                        for market_id in backfill_market_ids:
                            backfill_attempt_map.pop(str(market_id), None)
                    backfill_done_set = getattr(market_scanner, "_market_history_backfill_done", None)
                    if isinstance(backfill_done_set, set):
                        for market_id in backfill_market_ids:
                            backfill_done_set.discard(str(market_id))
                    await market_scanner.attach_price_history_to_opportunities(
                        backfill_targets,
                        now=datetime.now(timezone.utc),
                        timeout_seconds=8.0,
                        block_for_backfill=True,
                    )
                    for opportunity in backfill_targets:
                        for raw_market in opportunity.markets:
                            _bind_market_payload_history(
                                raw_market,
                                normalized_history,
                                raw_history_lookup,
                                alias_to_history_key,
                                limit,
                                now_ms=now_ms,
                                window_seconds=scanner_window_seconds,
                                max_points=normalize_max_points,
                            )
                except Exception:
                    pass

    row = None
    histories: dict[str, list[dict[str, float]]] = {}
    for market_id in requested_ids:
        resolved_key = alias_to_history_key.get(market_id, market_id)
        histories[market_id] = normalized_history.get(resolved_key, [])

    # Always run the direct Polymarket API fetch for the position modal.
    # The scanner backfill uses a short window (sparkline hours) and may
    # only produce a small amount of data. The modal needs days of history
    # to fill the 7d/3d/24h time-window presets, so we always fetch and merge.
    empty_market_ids = list(requested_ids)
    if empty_market_ids:
        try:
            from services.polymarket import polymarket_client as _pm_client
        except Exception:
            _pm_client = None  # type: ignore[assignment]
        if _pm_client is not None:
            # Use a much wider window for the position modal — 7 days
            # to fill the 7d/3d/24h time window presets, not the scanner's
            # short sparkline window.
            modal_backfill_seconds = max(scanner_window_seconds, 7 * 24 * 3600)
            start_s = (now_ms - modal_backfill_seconds * 1000) // 1000
            now_s = now_ms // 1000
            for market_id in empty_market_ids:
                try:
                    catalog_market = catalog_by_alias.get(market_id)
                    token_pair: list[str] = []
                    if isinstance(catalog_market, dict):
                        for key in ("clob_token_ids", "clobTokenIds", "token_ids", "tokenIds"):
                            raw_tokens = catalog_market.get(key)
                            if isinstance(raw_tokens, (list, tuple)):
                                token_pair = [str(t).strip() for t in raw_tokens if str(t).strip() and len(str(t).strip()) > 20]
                            if len(token_pair) >= 2:
                                break
                    if len(token_pair) < 2:
                        # Try resolving the market to get token IDs.
                        if market_id.startswith("0x"):
                            resolved = await _pm_client.get_market_by_condition_id(market_id)
                        else:
                            resolved = await _pm_client.get_market_by_token_id(market_id)
                        if isinstance(resolved, dict):
                            for key in ("clob_token_ids", "clobTokenIds", "token_ids", "tokenIds"):
                                raw_tokens = resolved.get(key)
                                if isinstance(raw_tokens, (list, tuple)):
                                    token_pair = [str(t).strip() for t in raw_tokens if str(t).strip() and len(str(t).strip()) > 20]
                                if len(token_pair) >= 2:
                                    break
                    if len(token_pair) < 2:
                        continue
                    token_results = await asyncio.gather(
                        _pm_client.get_prices_history(token_pair[0], start_ts=start_s, end_ts=now_s),
                        _pm_client.get_prices_history(token_pair[1], start_ts=start_s, end_ts=now_s),
                        return_exceptions=True,
                    )
                    points: list[dict[str, float]] = []
                    buckets: dict[int, list[float | None]] = {}
                    for idx, result in enumerate(token_results):
                        if isinstance(result, Exception):
                            continue
                        for pt in result:
                            if not isinstance(pt, dict):
                                continue
                            try:
                                t = int(float(pt.get("t", 0)))
                                p = float(pt.get("p", 0))
                            except (TypeError, ValueError):
                                continue
                            if t <= 0 or not (0 <= p <= 1.01):
                                continue
                            if t not in buckets:
                                buckets[t] = [None, None]
                            buckets[t][idx] = p
                    for t in sorted(buckets):
                        vals = buckets[t]
                        yes = vals[0]
                        no = vals[1]
                        if yes is None and no is not None:
                            yes = round(1.0 - no, 6)
                        if no is None and yes is not None:
                            no = round(1.0 - yes, 6)
                        if yes is not None and no is not None:
                            points.append({"t": float(t), "yes": round(yes, 6), "no": round(no, 6), "idx_0": round(yes, 6), "idx_1": round(no, 6)})
                    if len(points) >= 2:
                        # Downsample to fit within limit while covering full span
                        downsampled = normalize_binary_price_history(
                            points,
                            now_ms=now_ms,
                            window_seconds=modal_backfill_seconds,
                            max_points=min(limit, 2000),
                        )
                        if len(downsampled) >= 2:
                            existing = histories.get(market_id, [])
                            if existing:
                                merged = _merge_normalized_binary_history(downsampled, existing, min(limit, 2000))
                                histories[market_id] = merged
                            else:
                                histories[market_id] = downsampled
                except Exception:
                    continue

    return {
        "histories": histories,
        "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at is not None else None,
    }


@router.get("/{trader_id}/positions/live-wallet")
async def get_trader_live_wallet_positions(
    trader_id: str,
    include_managed: bool = Query(default=True),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    try:
        return await list_live_wallet_positions_for_trader(
            session,
            trader_id=trader_id,
            include_managed=bool(include_managed),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{trader_id}/positions/live-wallet/adopt")
async def adopt_trader_live_wallet_position(
    trader_id: str,
    request: TraderLiveWalletPositionAdoptRequest,
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    try:
        result = await adopt_live_wallet_position(
            session,
            trader_id=trader_id,
            token_id=request.token_id,
            reason=request.reason,
        )
    except ValueError as exc:
        message = str(exc)
        if "already managed" in message:
            raise HTTPException(status_code=409, detail=message)
        if "not found in execution wallet positions" in message:
            raise HTTPException(status_code=404, detail=message)
        if "Execution wallet address is not configured" in message:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=422, detail=message)

    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_live_position_adopted",
        severity="warn",
        source="operator",
        operator=request.requested_by,
        message="Live wallet position adopted for trader management",
        payload={
            "token_id": result.get("token_id"),
            "market_id": result.get("market_id"),
            "direction": result.get("direction"),
            "wallet_address": result.get("wallet_address"),
            "order_id": ((result.get("order") or {}).get("id") if isinstance(result.get("order"), dict) else None),
            "reason": request.reason,
        },
    )
    return result


@router.get("/{trader_id}/copy-analytics")
async def get_trader_copy_analytics_route(
    trader_id: str,
    mode: Optional[str] = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10000),
    leader_limit: int = Query(default=20, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    mode_key = str(mode or "").strip().lower()
    if mode_key and mode_key not in {"shadow", "live"}:
        raise HTTPException(status_code=422, detail="mode must be 'shadow' or 'live'")

    return await get_trader_copy_analytics(
        session,
        trader_id=trader_id,
        mode=mode_key or None,
        limit=limit,
        leader_limit=leader_limit,
    )


@router.get("/{trader_id}")
async def get_trader_route(trader_id: str, session: AsyncSession = Depends(get_db_session)):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    return trader


@router.put("/{trader_id}")
async def update_trader_route(
    trader_id: str,
    request: TraderPatchRequest,
    session: AsyncSession = Depends(get_db_session),
):
    before = await get_trader(session, trader_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    payload = request.model_dump(exclude_none=True, exclude={"requested_by", "reason"})
    was_running = bool(before.get("is_enabled", True)) and not bool(before.get("is_paused", False))
    will_be_enabled = bool(payload.get("is_enabled", before.get("is_enabled", True)))
    will_be_paused = bool(payload.get("is_paused", before.get("is_paused", False)))
    will_be_running = will_be_enabled and not will_be_paused
    loss_streak_reset_at = ""
    if will_be_running and not was_running:
        merged_metadata = dict(before.get("metadata") or {})
        incoming_metadata = payload.get("metadata")
        if isinstance(incoming_metadata, dict):
            merged_metadata.update(incoming_metadata)
        merged_metadata, loss_streak_reset_at = _apply_loss_streak_reset_metadata(
            merged_metadata,
            reason="operator_resume",
        )
        payload["metadata"] = merged_metadata

    try:
        after = await update_trader(session, trader_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if after is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    _invalidate_trader_strategy_caches()

    await create_config_revision(
        session,
        trader_id=trader_id,
        operator=request.requested_by,
        reason=request.reason or "trader_update",
        orchestrator_before={},
        orchestrator_after={},
        trader_before=before,
        trader_after=after,
    )
    event_payload: dict[str, Any] = {"changes": payload}
    if loss_streak_reset_at:
        event_payload["loss_streak_reset_at"] = loss_streak_reset_at
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_updated",
        source="operator",
        operator=request.requested_by,
        message="Trader updated",
        payload=event_payload,
    )
    return after


@router.delete("/{trader_id}")
async def delete_trader_route(
    trader_id: str,
    action: TraderDeleteAction = Query(default=TraderDeleteAction.block),
    transfer_to_trader_id: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    await sync_trader_position_inventory(session, trader_id=trader_id)
    open_summary = await get_open_position_summary_for_trader(session, trader_id)
    open_order_summary = await get_open_order_summary_for_trader(session, trader_id)
    open_live_positions = int(open_summary.get("live", 0))
    open_shadow_positions = int(open_summary.get("shadow", 0))
    open_other_positions = int(open_summary.get("other", 0))
    open_total_positions = int(open_summary.get("total", 0))
    open_live_orders = int(open_order_summary.get("live", 0))
    open_shadow_orders = int(open_order_summary.get("shadow", 0))
    open_other_orders = int(open_order_summary.get("other", 0))
    open_total_orders = int(open_order_summary.get("total", 0))

    if action == TraderDeleteAction.disable:
        updated = await update_trader(
            session,
            trader_id,
            {
                "is_enabled": False,
                "is_paused": True,
            },
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Trader not found")
        _invalidate_trader_strategy_caches()
        await create_trader_event(
            session,
            trader_id=trader_id,
            event_type="trader_delete_requested",
            severity="warn",
            source="operator",
            message="Trader disabled instead of deleted",
            payload={
                "open_live_positions": open_live_positions,
                "open_shadow_positions": open_shadow_positions,
                "open_other_positions": open_other_positions,
                "open_live_orders": open_live_orders,
                "open_shadow_orders": open_shadow_orders,
                "open_other_orders": open_other_orders,
            },
        )
        return {
            "status": "disabled",
            "trader_id": trader_id,
            "open_live_positions": open_live_positions,
            "open_shadow_positions": open_shadow_positions,
            "open_other_positions": open_other_positions,
            "open_live_orders": open_live_orders,
            "open_shadow_orders": open_shadow_orders,
            "open_other_orders": open_other_orders,
            "message": "Trader disabled and paused. Resolve active exposure before permanent deletion.",
        }

    if action == TraderDeleteAction.transfer_delete:
        if not transfer_to_trader_id:
            raise HTTPException(status_code=400, detail="transfer_to_trader_id is required for transfer_delete action")
        if transfer_to_trader_id == trader_id:
            raise HTTPException(status_code=400, detail="Cannot transfer trades to the same bot being deleted")
        target_trader = await get_trader(session, transfer_to_trader_id)
        if target_trader is None:
            raise HTTPException(status_code=404, detail="Target trader not found")
        transferred = await transfer_open_trades(session, trader_id, transfer_to_trader_id)
        await create_trader_event(
            session,
            trader_id=transfer_to_trader_id,
            event_type="trades_transferred_in",
            severity="info",
            source="operator",
            message=f"Received {transferred['orders']} order(s) and {transferred['positions']} position(s) from deleted bot",
            payload={
                "from_trader_id": trader_id,
                "orders_transferred": transferred["orders"],
                "positions_transferred": transferred["positions"],
            },
        )
        try:
            ok = await delete_trader(session, trader_id, force=True)
        except Exception as exc:
            logger.error("delete_trader failed after transfer for %s: %s", trader_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to delete trader after transfer: {exc}")
        if not ok:
            raise HTTPException(status_code=404, detail="Trader not found after transfer")
        _invalidate_trader_strategy_caches()
        await create_trader_event(
            session,
            trader_id=None,
            event_type="trader_deleted",
            severity="info",
            source="operator",
            message="Trader deleted with trades transferred",
            payload={
                "deleted_trader_id": trader_id,
                "action": action.value,
                "transfer_to_trader_id": transfer_to_trader_id,
                "orders_transferred": transferred["orders"],
                "positions_transferred": transferred["positions"],
            },
        )
        return {
            "status": "deleted",
            "trader_id": trader_id,
            "action": action.value,
            "transfer_to_trader_id": transfer_to_trader_id,
            "orders_transferred": transferred["orders"],
            "positions_transferred": transferred["positions"],
            "open_live_positions": 0,
            "open_shadow_positions": 0,
            "open_other_positions": 0,
            "open_live_orders": 0,
            "open_shadow_orders": 0,
            "open_other_orders": 0,
            "open_total_positions": 0,
            "open_total_orders": 0,
        }

    if (
        open_live_positions > 0
        or open_live_orders > 0
    ) and action != TraderDeleteAction.force_delete:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "open_live_exposure",
                "message": (
                    "Trader has active exposure. Choose disable to pause safely, "
                    "or action=force_delete to permanently delete now."
                ),
                "trader_id": trader_id,
                "open_live_positions": open_live_positions,
                "open_shadow_positions": open_shadow_positions,
                "open_other_positions": open_other_positions,
                "open_live_orders": open_live_orders,
                "open_shadow_orders": open_shadow_orders,
                "open_other_orders": open_other_orders,
                "suggested_action": TraderDeleteAction.force_delete.value,
                "safe_action": TraderDeleteAction.disable.value,
            },
        )

    try:
        ok = await delete_trader(
            session,
            trader_id,
            force=(action == TraderDeleteAction.force_delete),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error("delete_trader failed for %s: %s", trader_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete trader: {exc}")
    if not ok:
        raise HTTPException(status_code=404, detail="Trader not found")
    _invalidate_trader_strategy_caches()
    await create_trader_event(
        session,
        trader_id=None,
        event_type="trader_deleted",
        severity="warn" if action == TraderDeleteAction.force_delete else "info",
        source="operator",
        message="Trader deleted",
        payload={
            "deleted_trader_id": trader_id,
            "action": action.value,
            "open_live_positions_at_delete": open_live_positions,
            "open_shadow_positions_at_delete": open_shadow_positions,
            "open_other_positions_at_delete": open_other_positions,
            "open_live_orders_at_delete": open_live_orders,
            "open_shadow_orders_at_delete": open_shadow_orders,
            "open_other_orders_at_delete": open_other_orders,
        },
    )
    return {
        "status": "deleted",
        "trader_id": trader_id,
        "action": action.value,
        "open_live_positions": open_live_positions,
        "open_shadow_positions": open_shadow_positions,
        "open_other_positions": open_other_positions,
        "open_live_orders": open_live_orders,
        "open_shadow_orders": open_shadow_orders,
        "open_other_orders": open_other_orders,
        "open_total_positions": open_total_positions,
        "open_total_orders": open_total_orders,
    }


@router.post("/{trader_id}/start")
async def start_trader(
    trader_id: str,
    request: TraderStartRequest = TraderStartRequest(),
    session: AsyncSession = Depends(get_db_session),
):
    _assert_not_globally_paused()
    before = await get_trader(session, trader_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    if not bool(before.get("is_enabled", True)):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "trader_inactive",
                "message": "Trader is inactive. Activate the trader before starting.",
                "trader_id": trader_id,
            },
        )
    was_running = bool(before.get("is_enabled", True)) and not bool(before.get("is_paused", False))
    metadata, loss_streak_reset_at = _apply_loss_streak_reset_metadata(
        dict(before.get("metadata") or {}),
        reason="operator_start",
    )
    trader = await update_trader(
        session,
        trader_id,
        {
            "is_paused": False,
            "metadata": metadata,
        },
    )
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    _invalidate_trader_strategy_caches()
    await request_trader_run(session, trader_id)

    control = await read_orchestrator_control(session)
    mode = str(control.get("mode") or "shadow").strip().lower()
    await sync_trader_position_inventory(
        session,
        trader_id=trader_id,
        mode=mode if mode in {"shadow", "live"} else None,
    )
    open_summary = await get_open_position_summary_for_trader(session, trader_id)
    open_live = int(open_summary.get("live", 0))
    open_shadow = int(open_summary.get("shadow", 0))
    if mode == "live" and open_live > 0:
        await create_trader_event(
            session,
            trader_id=trader_id,
            event_type="trader_start_warning",
            severity="warn",
            source="operator",
            message="Trader started with existing live open positions",
            payload={
                "open_live_positions": open_live,
                "open_shadow_positions": open_shadow,
            },
        )
    elif mode == "shadow" and open_shadow > 0:
        await create_trader_event(
            session,
            trader_id=trader_id,
            event_type="trader_start_notice",
            source="operator",
            message="Trader started with existing shadow open positions",
            payload={"open_shadow_positions": open_shadow},
        )

    copy_bootstrap_summary: dict[str, Any] | None = None
    should_schedule_bootstrap = (not was_running) and _copy_bootstrap_requested(before, request.copy_existing_positions)
    if should_schedule_bootstrap:
        copy_bootstrap_summary = {
            "trader_id": trader_id,
            "status": "scheduled",
            "reason": "bootstrap_running_in_background",
        }
        _track_background_task(
            asyncio.create_task(
                _run_copy_bootstrap_background(
                    trader_id=trader_id,
                    copy_existing_positions=request.copy_existing_positions,
                    requested_by=request.requested_by,
                ),
                name=f"trader-copy-bootstrap:{trader_id}",
            )
        )

    started_payload: dict[str, Any] = {}
    if loss_streak_reset_at:
        started_payload[_LOSS_STREAK_RESET_AT_KEY] = loss_streak_reset_at
    if copy_bootstrap_summary is not None:
        started_payload["copy_bootstrap"] = copy_bootstrap_summary
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_started",
        source="operator",
        operator=request.requested_by,
        message="Trader resumed",
        payload=started_payload,
    )
    if copy_bootstrap_summary is None:
        return trader
    response = dict(trader)
    response["copy_bootstrap"] = copy_bootstrap_summary
    return response


@router.post("/{trader_id}/stop")
async def stop_trader(
    trader_id: str,
    request: TraderStopRequest = TraderStopRequest(),
    session: AsyncSession = Depends(get_db_session),
):
    before = await get_trader(session, trader_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    stop_lifecycle = request.stop_lifecycle
    if stop_lifecycle == TraderStopLifecycleMode.close_all_positions and not bool(request.confirm_live):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "confirm_live_required",
                "message": "close_all_positions requires confirm_live=true.",
                "stop_lifecycle": stop_lifecycle.value,
            },
        )

    trader = await set_trader_paused(session, trader_id, True)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    _invalidate_trader_strategy_caches()

    lifecycle_summary: dict[str, Any] = {
        "mode": stop_lifecycle.value,
        "cleanup": {},
    }
    cleanup_reason = str(request.reason or "manual_stop_lifecycle_cleanup")
    cleanup_scope = "shadow"
    if stop_lifecycle == TraderStopLifecycleMode.close_all_positions:
        cleanup_scope = "all"

    if stop_lifecycle in {
        TraderStopLifecycleMode.close_shadow_positions,
        TraderStopLifecycleMode.close_all_positions,
    }:
        try:
            cancel_result = await cleanup_trader_open_orders(
                session,
                trader_id=trader_id,
                scope=cleanup_scope,
                max_age_hours=None,
                dry_run=False,
                target_status="cancelled",
                reason=cleanup_reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        lifecycle_summary["cleanup"]["orders"] = cancel_result

        shadow_cleanup = await reconcile_shadow_positions(
            session,
            trader_id=trader_id,
            trader_params={},
            dry_run=False,
            force_mark_to_market=True,
            max_age_hours=None,
            reason=cleanup_reason,
        )
        lifecycle_summary["cleanup"]["shadow"] = {
            "matched": int(shadow_cleanup.get("matched", 0)),
            "closed": int(shadow_cleanup.get("closed", 0)),
            "held": int(shadow_cleanup.get("held", 0)),
            "skipped": int(shadow_cleanup.get("skipped", 0)),
            "total_realized_pnl": float(shadow_cleanup.get("total_realized_pnl", 0.0)),
        }
        await sync_trader_position_inventory(session, trader_id=trader_id, mode="shadow")

        if stop_lifecycle == TraderStopLifecycleMode.close_all_positions:
            live_cleanup = await reconcile_live_positions(
                session,
                trader_id=trader_id,
                trader_params={},
                dry_run=False,
                force_mark_to_market=True,
                max_age_hours=None,
                reason=cleanup_reason,
            )
            lifecycle_summary["cleanup"]["live"] = {
                "matched": int(live_cleanup.get("matched", 0)),
                "closed": int(live_cleanup.get("closed", 0)),
                "held": int(live_cleanup.get("held", 0)),
                "skipped": int(live_cleanup.get("skipped", 0)),
                "total_realized_pnl": float(live_cleanup.get("total_realized_pnl", 0.0)),
            }
            await sync_trader_position_inventory(session, trader_id=trader_id, mode="live")

    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_stopped",
        severity="warn" if stop_lifecycle != TraderStopLifecycleMode.keep_positions else "info",
        source="operator",
        operator=request.requested_by,
        message="Trader stopped",
        payload=lifecycle_summary,
    )
    response = dict(trader)
    response["stop_lifecycle"] = lifecycle_summary
    return response


@router.post("/{trader_id}/activate")
async def activate_trader(
    trader_id: str,
    request: TraderActivationRequest = TraderActivationRequest(),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await update_trader(
        session,
        trader_id,
        {
            "is_enabled": True,
        },
    )
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    _invalidate_trader_strategy_caches()

    payload: dict[str, Any] = {}
    if request.reason:
        payload["reason"] = request.reason
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_activated",
        source="operator",
        operator=request.requested_by,
        message="Trader activated",
        payload=payload or None,
    )
    return trader


@router.post("/{trader_id}/deactivate")
async def deactivate_trader(
    trader_id: str,
    request: TraderActivationRequest = TraderActivationRequest(),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await update_trader(
        session,
        trader_id,
        {
            "is_enabled": False,
            "is_paused": True,
        },
    )
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    _invalidate_trader_strategy_caches()

    payload: dict[str, Any] = {}
    if request.reason:
        payload["reason"] = request.reason
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_deactivated",
        severity="warn",
        source="operator",
        operator=request.requested_by,
        message="Trader set inactive",
        payload=payload or None,
    )
    return trader


class TraderBlockNewOrdersRequest(BaseModel):
    enabled: bool
    requested_by: Optional[str] = None
    reason: Optional[str] = None


@router.post("/{trader_id}/block-new-orders")
async def set_trader_block_new_orders(
    trader_id: str,
    request: TraderBlockNewOrdersRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Per-bot analogue of the global kill switch.

    When enabled, the trader keeps running (reconciliation, exits,
    manage-only mode) but the entry-decision path short-circuits before
    opening any new positions.
    """
    before = await get_trader(session, trader_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    after = await update_trader(
        session,
        trader_id,
        {"block_new_orders": bool(request.enabled)},
    )
    if after is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    _invalidate_trader_strategy_caches()
    payload: dict[str, Any] = {"enabled": bool(request.enabled)}
    if request.reason:
        payload["reason"] = request.reason
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_block_new_orders",
        severity="warn" if request.enabled else "info",
        source="operator",
        operator=request.requested_by,
        message="block_new_orders enabled" if request.enabled else "block_new_orders disabled",
        payload=payload,
    )
    return after


@router.post("/{trader_id}/positions/cleanup")
async def cleanup_trader_positions(
    trader_id: str,
    request: TraderPositionCleanupRequest,
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    if request.scope in {TraderPositionCleanupScope.live, TraderPositionCleanupScope.all} and not request.confirm_live:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "confirm_live_required",
                "message": "Live cleanup requires explicit confirmation. Re-submit with confirm_live=true.",
                "scope": request.scope.value,
            },
        )

    if request.method == TraderPositionCleanupMethod.mark_to_market and request.scope not in {
        TraderPositionCleanupScope.shadow,
        TraderPositionCleanupScope.live,
    }:
        raise HTTPException(
            status_code=422,
            detail="mark_to_market cleanup supports shadow and live scopes",
        )

    if request.method == TraderPositionCleanupMethod.cancel:
        try:
            result = await cleanup_trader_open_orders(
                session,
                trader_id=trader_id,
                scope=request.scope.value,
                max_age_hours=request.max_age_hours,
                dry_run=request.dry_run,
                target_status=request.target_status,
                reason=request.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        if request.scope == TraderPositionCleanupScope.live:
            lifecycle_result = await reconcile_live_positions(
                session,
                trader_id=trader_id,
                trader_params={},
                dry_run=request.dry_run,
                force_mark_to_market=True,
                max_age_hours=request.max_age_hours,
                reason=str(request.reason or "manual_mark_to_market_cleanup"),
            )
            if not request.dry_run:
                await sync_trader_position_inventory(session, trader_id=trader_id, mode="live")
        elif request.scope == TraderPositionCleanupScope.shadow:
            lifecycle_result = await reconcile_shadow_positions(
                session,
                trader_id=trader_id,
                trader_params={},
                dry_run=request.dry_run,
                force_mark_to_market=True,
                max_age_hours=request.max_age_hours,
                reason=str(request.reason or "manual_mark_to_market_cleanup"),
            )
            if not request.dry_run:
                await sync_trader_position_inventory(session, trader_id=trader_id, mode="shadow")
        else:
            raise HTTPException(
                status_code=422,
                detail="mark_to_market cleanup supports shadow and live scopes",
            )
        result = {
            "trader_id": trader_id,
            "scope": request.scope.value,
            "method": request.method.value,
            "dry_run": request.dry_run,
            "max_age_hours": request.max_age_hours,
            "matched": int(lifecycle_result.get("matched", 0)),
            "would_close": int(lifecycle_result.get("would_close", 0)),
            "updated": int(lifecycle_result.get("closed", 0)),
            "skipped": int(lifecycle_result.get("skipped", 0)),
            "held": int(lifecycle_result.get("held", 0)),
            "total_realized_pnl": float(lifecycle_result.get("total_realized_pnl", 0.0)),
            "by_status": lifecycle_result.get("by_status", {}),
            "skipped_reasons": lifecycle_result.get("skipped_reasons", {}),
            "details": lifecycle_result.get("details", []),
        }

    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_positions_cleanup",
        severity=(
            "warn" if request.scope in {TraderPositionCleanupScope.live, TraderPositionCleanupScope.all} else "info"
        ),
        source="operator",
        message="Trader position cleanup executed" if not request.dry_run else "Trader position cleanup dry-run",
        payload=result,
    )
    return result


@router.post("/{trader_id}/orders/{order_id}/sell")
async def sell_trader_order_now(
    trader_id: str,
    order_id: str,
    request: TraderOrderManualCloseRequest = TraderOrderManualCloseRequest(),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    order = (
        await session.execute(
            select(TraderOrder).where(
                TraderOrder.id == order_id,
                TraderOrder.trader_id == trader_id,
            )
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    mode_key = str(order.mode or "").strip().lower()

    status_key = str(order.status or "").strip().lower()
    if status_key not in OPEN_ORDER_STATUSES:
        raise HTTPException(status_code=409, detail="Order is not active")

    close_reason = str(request.reason or "manual_trade_sell").strip() or "manual_trade_sell"
    if mode_key == "shadow":
        lifecycle_result = await reconcile_shadow_positions(
            session,
            trader_id=trader_id,
            trader_params={},
            dry_run=False,
            force_mark_to_market=True,
            max_age_hours=None,
            order_ids=[order_id],
            reason=close_reason,
        )
        await sync_trader_position_inventory(session, trader_id=trader_id, mode="shadow")
    elif mode_key == "live":
        lifecycle_result = await reconcile_live_positions(
            session,
            trader_id=trader_id,
            trader_params={},
            dry_run=False,
            force_mark_to_market=True,
            max_age_hours=None,
            order_ids=[order_id],
            reason=close_reason,
        )
        await sync_trader_position_inventory(session, trader_id=trader_id, mode="live")
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported order mode: {mode_key or 'unknown'}")

    matched = int(lifecycle_result.get("matched", 0))
    closed = int(lifecycle_result.get("closed", 0))
    state_updates = int(lifecycle_result.get("state_updates", 0))
    if matched <= 0 and closed <= 0 and state_updates <= 0:
        raise HTTPException(status_code=409, detail="No active position could be sold for this order")

    payload = {
        "order_id": order_id,
        "mode": mode_key,
        "matched": matched,
        "closed": closed,
        "state_updates": state_updates,
        "would_close": int(lifecycle_result.get("would_close", 0)),
        "details": lifecycle_result.get("details", []),
    }
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_order_manual_sell",
        severity="warn" if mode_key == "live" else "info",
        source="operator",
        operator=request.requested_by,
        message="Manual sell requested for trader order",
        payload=payload,
    )
    return {
        "status": "submitted",
        "trader_id": trader_id,
        "order_id": order_id,
        "mode": mode_key,
        "result": payload,
    }


@router.post("/{trader_id}/orders/{order_id}/reconcile")
async def reconcile_trader_order(
    trader_id: str,
    order_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Reconcile an order's recorded notional/price with actual Polymarket position."""
    from services.polymarket import polymarket_client
    from services.trader_orchestrator_state import _extract_order_token_id

    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    order = (
        await session.execute(
            select(TraderOrder).where(
                TraderOrder.id == order_id,
                TraderOrder.trader_id == trader_id,
            )
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    mode_key = str(order.mode or "").strip().lower()
    if mode_key != "live":
        raise HTTPException(status_code=422, detail="Reconcile only applies to live orders")

    status_key = str(order.status or "").strip().lower()
    if status_key not in OPEN_ORDER_STATUSES:
        raise HTTPException(status_code=409, detail="Order is not active")

    token_id = _extract_order_token_id(order)
    if not token_id:
        raise HTTPException(status_code=422, detail="Cannot determine token_id from order payload")

    wallet = str(live_execution_service.get_execution_wallet_address() or "").strip()
    if not wallet:
        raise HTTPException(status_code=503, detail="Live execution wallet not available")

    positions = await polymarket_client.get_wallet_positions_with_prices(wallet)
    matched_pos = None
    for pos in positions:
        pos_asset = str(pos.get("asset") or pos.get("asset_id") or pos.get("token_id") or "").strip()
        if pos_asset == token_id:
            matched_pos = pos
            break

    old_notional = float(order.notional_usd or 0)
    old_effective_price = float(order.effective_price or 0)

    if matched_pos is not None:
        new_size = float(matched_pos.get("size", 0) or 0)
        new_avg_price = float(matched_pos.get("avgPrice", 0) or 0)
        new_initial_value = float(matched_pos.get("initialValue", 0) or 0)
        new_current_value = float(matched_pos.get("currentValue", 0) or 0)
        new_notional = new_initial_value if new_initial_value > 0 else (new_size * new_avg_price)
        new_effective_price = new_avg_price
    else:
        new_size = 0.0
        new_avg_price = 0.0
        new_notional = 0.0
        new_effective_price = 0.0
        new_initial_value = 0.0
        new_current_value = 0.0

    order.notional_usd = new_notional
    order.effective_price = new_effective_price
    payload = dict(order.payload_json or {})
    payload["manual_reconciliation"] = {
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "before": {"notional_usd": old_notional, "effective_price": old_effective_price},
        "after": {"notional_usd": new_notional, "effective_price": new_effective_price},
        "polymarket": {
            "size": new_size,
            "avg_price": new_avg_price,
            "initial_value": new_initial_value,
            "current_value": new_current_value,
        },
    }
    order.payload_json = payload
    await session.commit()
    await sync_trader_position_inventory(session, trader_id=trader_id, mode="live")

    result = {
        "before": {"notional_usd": round(old_notional, 4), "effective_price": round(old_effective_price, 4)},
        "after": {"notional_usd": round(new_notional, 4), "effective_price": round(new_effective_price, 4)},
        "polymarket": {"size": round(new_size, 4), "avg_price": round(new_avg_price, 4), "current_value": round(new_current_value, 4)},
    }
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="trader_order_manual_reconcile",
        severity="warn",
        source="operator",
        message=f"Manual reconcile: ${old_notional:.2f} -> ${new_notional:.2f}",
        payload={"order_id": order_id, **result},
    )
    return {"status": "reconciled", "trader_id": trader_id, "order_id": order_id, **result}


@router.post("/{trader_id}/manual-buy")
async def manual_buy(
    trader_id: str,
    request: TraderManualBuyRequest,
    session: AsyncSession = Depends(get_db_session),
):
    _assert_not_globally_paused()
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    if not bool(trader.get("is_enabled", False)):
        raise HTTPException(status_code=409, detail="Selected trader is disabled.")
    if bool(trader.get("is_paused", False)):
        raise HTTPException(status_code=409, detail="Selected trader is paused.")
    if bool(trader.get("block_new_orders", False)):
        raise HTTPException(status_code=409, detail="Selected trader is blocking new orders.")

    mode = str(trader.get("mode") or "shadow").strip().lower()
    if mode not in {"shadow", "live"}:
        raise HTTPException(status_code=409, detail=f"Unsupported trader execution mode: {mode or 'unknown'}")
    if mode == "shadow" and request.order_type == "limit":
        raise HTTPException(
            status_code=409,
            detail="Shadow manual execution supports immediate marketable-limit matching only; resting limits are not simulated.",
        )

    control = await read_orchestrator_control(session, read_only=True)
    if not bool(control.get("is_enabled", False)):
        raise HTTPException(status_code=409, detail="Trader orchestrator is disabled.")
    if bool(control.get("is_paused", False)):
        raise HTTPException(status_code=409, detail="Trader orchestrator is paused.")
    if bool(control.get("kill_switch", False)):
        raise HTTPException(status_code=409, detail="Trader orchestrator kill switch is active.")

    request_fingerprint = _manual_buy_request_fingerprint(trader_id, request)
    decision_id = _manual_buy_decision_id(trader_id, request.client_request_id)
    shadow_account_id = _manual_shadow_account_id(control, trader) if mode == "shadow" else ""
    if mode == "shadow":
        if not shadow_account_id:
            raise HTTPException(status_code=409, detail="No Shadow account is configured for manual execution.")
        if await session.get(SimulationAccount, shadow_account_id) is None:
            raise HTTPException(status_code=409, detail=f"Configured Shadow account was not found: {shadow_account_id}")

    existing_decision = await session.get(TraderDecision, decision_id)
    if existing_decision is not None:
        return await _manual_buy_response_for_decision(
            session,
            trader_id=trader_id,
            decision=existing_decision,
            request_fingerprint=request_fingerprint,
            expected_mode=mode,
            expected_shadow_account_id=shadow_account_id or None,
        )

    opportunity = await _read_current_manual_opportunity(session, str(request.opportunity_id or ""))
    runtime_signal = _build_manual_runtime_signal(
        opportunity=opportunity,
        request=request,
        trader_id=trader_id,
        request_fingerprint=request_fingerprint,
    )
    decision_payload = {
        "manual_execution": {
            "request_id": request.client_request_id,
            "request_fingerprint": request_fingerprint,
            "opportunity_id": str(request.opportunity_id or ""),
            "order_type": request.order_type,
            "size_usd": float(request.size_usd),
            "mode": mode,
            "shadow_account_id": shadow_account_id or None,
        }
    }
    try:
        decision_row = await create_trader_decision(
            session,
            decision_id=decision_id,
            trader_id=trader_id,
            signal=runtime_signal,
            strategy_key="manual_buy",
            strategy_version=None,
            decision="selected",
            reason="Operator confirmed manual opportunity execution.",
            score=getattr(runtime_signal, "confidence", None),
            checks_summary={"count": 0, "manual": True},
            risk_snapshot=dict(trader.get("risk_limits") or {}),
            payload=decision_payload,
            trace_id=None,
        )
    except IntegrityError:
        await session.rollback()
        raced_decision = await session.get(TraderDecision, decision_id)
        if raced_decision is None:
            raise
        return await _manual_buy_response_for_decision(
            session,
            trader_id=trader_id,
            decision=raced_decision,
            request_fingerprint=request_fingerprint,
            expected_mode=mode,
            expected_shadow_account_id=shadow_account_id or None,
        )

    execution_engine = ExecutionSessionEngine(session)
    try:
        execution_result = await execution_engine.execute_signal(
            trader_id=trader_id,
            signal=runtime_signal,
            decision_id=decision_row.id,
            strategy_key="manual_buy",
            strategy_version=None,
            strategy_params={},
            risk_limits=dict(trader.get("risk_limits") or {}),
            mode=mode,
            size_usd=float(request.size_usd),
            reason="Manual buy from UI",
            explicit_strategy_params={},
            execution_timeout_seconds=8.0 if mode == "shadow" else None,
            shadow_account_id=shadow_account_id or None,
        )
    except Exception as exc:
        error_message = f"Manual execution failed: {type(exc).__name__}: {str(exc)[:240]}"
        await update_trader_decision(
            session,
            decision_id=decision_row.id,
            decision="failed",
            reason=error_message,
            payload_patch={"execution_status": "failed", "execution_error": error_message},
        )
        raise HTTPException(status_code=409, detail=error_message) from exc

    normalized_status = str(execution_result.status or "").strip().lower()
    decision_status = "selected"
    if normalized_status == "skipped":
        decision_status = "skipped"
    elif normalized_status in {"failed", "cancelled", "expired"}:
        decision_status = "failed"
    elif int(execution_result.orders_written or 0) <= 0:
        decision_status = "failed"
    result_reason = str(execution_result.error_message or "").strip() or (
        "Manual execution completed."
        if int(execution_result.orders_written or 0) > 0
        else "Manual execution did not produce a committed order."
    )
    refreshed_decision = await update_trader_decision(
        session,
        decision_id=decision_row.id,
        decision=decision_status,
        reason=result_reason,
        payload_patch={
            "execution_status": normalized_status,
            "execution_session_id": str(execution_result.session_id or ""),
            "orders_written": int(execution_result.orders_written or 0),
        },
    )
    if refreshed_decision is None:
        raise HTTPException(status_code=500, detail="Manual decision disappeared after execution.")
    return await _manual_buy_response_for_decision(
        session,
        trader_id=trader_id,
        decision=refreshed_decision,
        request_fingerprint=request_fingerprint,
        expected_mode=mode,
        expected_shadow_account_id=shadow_account_id or None,
    )


@router.post("/{trader_id}/run-once")
async def run_once(trader_id: str, session: AsyncSession = Depends(get_db_session)):
    _assert_not_globally_paused()
    trader = await request_trader_run(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    await create_trader_event(
        session,
        trader_id=trader_id,
        event_type="run_once_requested",
        source="operator",
        message="Run-once requested",
    )
    return trader


@router.get("/orders")
async def get_all_trader_orders(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    orders = await list_serialized_trader_orders(
        session,
        trader_id=None,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "orders": orders,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{trader_id}/decisions")
async def get_trader_decisions(
    trader_id: str,
    decision: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
):
    return {
        "decisions": await list_serialized_trader_decisions(
            session,
            trader_id=trader_id,
            decision=decision,
            limit=limit,
        )
    }


@router.get("/{trader_id}/orders")
async def get_trader_orders(
    trader_id: str,
    status: Optional[str] = Query(default=None),
    mode: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=20000),
    session: AsyncSession = Depends(get_db_session),
):
    orders = await list_serialized_trader_orders(
        session,
        trader_id=trader_id,
        status=status,
        mode=mode,
        limit=limit,
    )
    return {
        "orders": orders
    }


@router.get("/{trader_id}/events")
async def get_events(
    trader_id: str,
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    types: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
):
    event_types = [item.strip() for item in (types or "").split(",") if item.strip()]
    events, next_cursor = await list_serialized_trader_events(
        session,
        trader_id=trader_id,
        limit=limit,
        cursor=cursor,
        event_types=event_types or None,
    )
    return {"events": events, "next_cursor": next_cursor}


@router.get("/{trader_id}/execution-sessions")
async def get_execution_sessions(
    trader_id: str,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")
    return {
        "sessions": await list_serialized_execution_sessions(
            session,
            trader_id=trader_id,
            status=status,
            limit=limit,
        )
    }


@router.get("/execution-sessions/{session_id}")
async def get_execution_session(
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    detail = await get_serialized_execution_session_detail(session, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Execution session not found")
    return detail


@router.post("/execution-sessions/{session_id}/cancel")
async def cancel_execution_session(
    session_id: str,
    request: TraderExecutionSessionControlRequest = TraderExecutionSessionControlRequest(),
    session: AsyncSession = Depends(get_db_session),
):
    engine = ExecutionSessionEngine(session)
    reason = str(request.reason or "manual_cancel").strip()
    ok = await engine.cancel_session(session_id=session_id, reason=reason)
    if not ok:
        raise HTTPException(status_code=404, detail="Execution session not found")
    return {"status": "cancelled", "session_id": session_id}


@router.post("/execution-sessions/{session_id}/pause")
async def pause_execution_session(
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    engine = ExecutionSessionEngine(session)
    ok = await engine.pause_session(session_id=session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Execution session not found")
    return {"status": "paused", "session_id": session_id}


@router.post("/execution-sessions/{session_id}/resume")
async def resume_execution_session(
    session_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    engine = ExecutionSessionEngine(session)
    ok = await engine.resume_session(session_id=session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Execution session not found")
    return {"status": "working", "session_id": session_id}


@router.get("/decisions/{decision_id}")
async def get_decision_detail(
    decision_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    detail = await get_trader_decision_detail(session, decision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return detail


@router.post("/{trader_id}/tune/iterate")
async def run_trader_tune_iteration(
    trader_id: str,
    request: TraderTuneAgentRequest,
    session: AsyncSession = Depends(get_db_session),
):
    trader = await get_trader(session, trader_id)
    if trader is None:
        raise HTTPException(status_code=404, detail="Trader not found")

    try:
        result = await run_strategy_tune_agent(
            trader_id=trader_id,
            prompt=request.prompt,
            model=request.model,
            max_iterations=request.max_iterations,
            monitor_job_id=request.monitor_job_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return result
