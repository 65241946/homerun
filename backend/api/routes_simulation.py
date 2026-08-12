import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.database import get_db_session
from services.polymarket import polymarket_client
from services.settlement_repair import (
    SettlementRepairConflict,
    SettlementRepairNotFound,
    SettlementRepairRejected,
    apply_settlement_repair,
    preview_settlement_repair,
)
from services.simulation import simulation_service
from services.simulation_ledger import get_ledger_integrity

simulation_router = APIRouter()


class CreateAccountRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    initial_capital: float = Field(default=10000.0, ge=100.0, le=10000000.0)
    max_position_pct: float = Field(default=10.0, ge=1.0, le=100.0)
    max_positions: int = Field(default=10, ge=1, le=100)


class ExecuteTradeRequest(BaseModel):
    opportunity_id: str
    position_size: Optional[float] = Field(default=None, ge=1.0)
    take_profit_price: Optional[float] = Field(default=None, ge=0.01, le=1.0)
    stop_loss_price: Optional[float] = Field(default=None, ge=0.01, le=1.0)


class SettlementRepairPreviewRequest(BaseModel):
    order_ids: list[str] | None = Field(default=None, max_length=200)


class SettlementRepairApplyRequest(BaseModel):
    order_ids: list[str] = Field(..., min_length=1, max_length=200)
    preview_digest: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    confirm: bool


# ==================== ACCOUNTS ====================


@simulation_router.post("/accounts")
async def create_simulation_account(request: CreateAccountRequest):
    """Create a new simulation account for shadow trading"""
    try:
        account = await simulation_service.create_account(
            name=request.name,
            initial_capital=request.initial_capital,
            max_position_pct=request.max_position_pct,
            max_positions=request.max_positions,
        )
    except OperationalError as exc:
        if simulation_service.is_retryable_db_error(exc):
            raise HTTPException(
                status_code=503,
                detail="Database is busy; please retry creating the sandbox account in a few seconds.",
            ) from exc
        raise

    return {
        "account_id": account.id,
        "name": account.name,
        "initial_capital": account.initial_capital,
        "message": "Simulation account created successfully",
    }


@simulation_router.get("/accounts")
async def list_simulation_accounts():
    """List all simulation accounts with full stats"""
    accounts_with_positions = await simulation_service.get_all_accounts_with_positions()
    result = []
    for acc, positions in accounts_with_positions:
        win_rate = acc.winning_trades / acc.total_trades * 100 if acc.total_trades > 0 else 0
        # Calculate unrealized P&L from open positions
        unrealized_pnl = sum(p.unrealized_pnl for p in positions)
        # Book value = sum of entry costs of open positions
        book_value = sum(p.entry_cost for p in positions)
        # Market value = sum of current value of open positions
        market_value = sum(
            p.quantity * (p.current_price if p.current_price is not None else p.entry_price)
            for p in positions
        )
        equity_value = float(acc.current_capital or 0.0) + float(market_value or 0.0)
        roi = (
            ((equity_value - float(acc.initial_capital)) / float(acc.initial_capital)) * 100.0
            if acc.initial_capital > 0
            else 0.0
        )
        result.append(
            {
                "id": acc.id,
                "name": acc.name,
                "initial_capital": acc.initial_capital,
                "current_capital": acc.current_capital,
                "total_pnl": acc.total_pnl,
                "total_trades": acc.total_trades,
                "winning_trades": acc.winning_trades,
                "losing_trades": acc.losing_trades,
                "win_rate": win_rate,
                "roi_percent": roi,
                "open_positions": len(positions),
                "unrealized_pnl": unrealized_pnl,
                "book_value": book_value,
                "market_value": market_value,
                "created_at": acc.created_at.isoformat() if acc.created_at else None,
            }
        )
    return result


@simulation_router.get("/accounts/{account_id}")
async def get_simulation_account(account_id: str):
    """Get detailed simulation account information"""
    stats = await simulation_service.get_account_stats(account_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Account not found")
    return stats


@simulation_router.post("/accounts/{account_id}/settlement-repair/preview")
async def preview_account_settlement_repair(
    account_id: str,
    request: SettlementRepairPreviewRequest,
    session: AsyncSession = Depends(get_db_session),  # noqa: B008 - FastAPI dependency injection
):
    """Build a persisted-facts-only settlement certificate without writes."""

    try:
        return await preview_settlement_repair(
            session,
            account_id=account_id,
            order_ids=request.order_ids,
        )
    except SettlementRepairNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "settlement_repair_account_not_found", "message": str(exc)},
        ) from exc
    except SettlementRepairRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "settlement_repair_request_invalid", "message": str(exc)},
        ) from exc


@simulation_router.post("/accounts/{account_id}/settlement-repair/apply")
async def apply_account_settlement_repair(
    account_id: str,
    request: SettlementRepairApplyRequest,
    session: AsyncSession = Depends(get_db_session),  # noqa: B008 - FastAPI dependency injection
):
    """Apply one exact approved digest atomically; disabled by default."""

    if not settings.HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "settlement_repair_apply_disabled",
                "message": "Historical settlement repair apply is disabled by runtime configuration.",
            },
        )
    try:
        async with session.begin():
            return await apply_settlement_repair(
                session,
                account_id=account_id,
                order_ids=request.order_ids,
                preview_digest=request.preview_digest,
                confirm=request.confirm,
            )
    except SettlementRepairNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "settlement_repair_account_not_found", "message": str(exc)},
        ) from exc
    except SettlementRepairRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "settlement_repair_request_invalid", "message": str(exc)},
        ) from exc
    except SettlementRepairConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "settlement_repair_preview_stale", "message": str(exc)},
        ) from exc


@simulation_router.get("/accounts/{account_id}/ledger-integrity")
async def get_account_ledger_integrity(
    account_id: str,
    session: AsyncSession = Depends(get_db_session),  # noqa: B008 - FastAPI dependency injection
):
    """Expose rebuildable cash-journal coverage and six-decimal deltas."""

    try:
        snapshot = await get_ledger_integrity(session, account_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "simulation_account_not_found", "message": str(exc)},
        ) from exc
    return {
        "account_id": snapshot.account_id,
        "ledger_version": snapshot.ledger_version,
        "status": snapshot.status,
        "coverage": snapshot.coverage,
        "initial_capital_usdc": f"{snapshot.initial_capital_usdc:.6f}",
        "journal_delta_usdc": f"{snapshot.journal_delta_usdc:.6f}",
        "journal_balance_usdc": f"{snapshot.rebuilt_balance_usdc:.6f}",
        "rebuilt_balance_usdc": f"{snapshot.rebuilt_balance_usdc:.6f}",
        "projected_balance_usdc": f"{snapshot.projected_balance_usdc:.6f}",
        "difference_usdc": f"{snapshot.difference_usdc:.6f}",
        "entry_count": snapshot.entry_count,
        "max_sequence": snapshot.max_sequence,
        "sequence_contiguous": snapshot.sequence_contiguous,
    }


@simulation_router.delete("/accounts/{account_id}")
async def delete_simulation_account(account_id: str):
    """Delete a simulation account and all its trades/positions"""
    try:
        deleted = await simulation_service.delete_account(account_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "append_only_ledger_account",
                "message": str(exc),
            },
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"message": "Account deleted successfully", "account_id": account_id}


@simulation_router.get("/accounts/{account_id}/positions")
async def get_account_positions(account_id: str):
    """Get open positions for a simulation account"""
    positions = await simulation_service.get_open_positions(account_id)

    lookups = {}
    for pos in positions:
        market_id = pos.market_id
        if not market_id or market_id in lookups:
            continue
        if market_id.startswith("0x"):
            lookups[market_id] = polymarket_client.get_market_by_condition_id(market_id)
        else:
            lookups[market_id] = polymarket_client.get_market_by_token_id(market_id)

    market_info_by_id: dict[str, dict] = {}
    if lookups:
        results = await asyncio.gather(*lookups.values(), return_exceptions=True)
        for market_id, info in zip(lookups.keys(), results):
            if isinstance(info, dict):
                market_info_by_id[market_id] = info

    return [
        {
            "id": pos.id,
            "market_id": pos.market_id,
            "market_slug": market_info_by_id.get(pos.market_id, {}).get("slug", ""),
            "event_slug": market_info_by_id.get(pos.market_id, {}).get("event_slug", ""),
            "market_question": pos.market_question,
            "token_id": pos.token_id,
            "side": pos.side.value,
            "quantity": pos.quantity,
            "entry_price": pos.entry_price,
            "entry_cost": pos.entry_cost,
            "current_price": pos.current_price,
            "unrealized_pnl": pos.unrealized_pnl,
            "take_profit_price": pos.take_profit_price,
            "stop_loss_price": pos.stop_loss_price,
            "opened_at": pos.opened_at.isoformat(),
            "status": pos.status.value,
        }
        for pos in positions
    ]


@simulation_router.get("/accounts/{account_id}/equity-history")
async def get_equity_history(account_id: str):
    """Get equity curve data for an account over time"""
    account = await simulation_service.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    trades = await simulation_service.get_trade_history(account_id, 10000)
    positions = await simulation_service.get_open_positions(account_id)

    # Build equity curve from trades (oldest first)
    sorted_trades = sorted(trades, key=lambda t: t.executed_at)
    equity_points = []
    cumulative_pnl = 0.0
    total_invested = 0.0
    total_returned = 0.0

    # Starting point
    equity_points.append(
        {
            "date": account.created_at.isoformat()
            if account.created_at
            else sorted_trades[0].executed_at.isoformat()
            if sorted_trades
            else None,
            "equity": account.initial_capital,
            "pnl": 0.0,
            "cumulative_pnl": 0.0,
            "trade_count": 0,
        }
    )

    for i, trade in enumerate(sorted_trades):
        total_invested += trade.total_cost
        if trade.actual_pnl is not None:
            cumulative_pnl += trade.actual_pnl
        if trade.actual_payout is not None:
            total_returned += trade.actual_payout

        equity_points.append(
            {
                "date": trade.executed_at.isoformat(),
                "equity": account.initial_capital + cumulative_pnl,
                "pnl": trade.actual_pnl or 0,
                "cumulative_pnl": cumulative_pnl,
                "trade_count": i + 1,
                "trade_id": trade.id,
                "status": trade.status.value,
            }
        )

        # If trade was resolved, add resolution point
        if trade.resolved_at and trade.resolved_at != trade.executed_at:
            equity_points.append(
                {
                    "date": trade.resolved_at.isoformat(),
                    "equity": account.initial_capital + cumulative_pnl,
                    "pnl": 0,
                    "cumulative_pnl": cumulative_pnl,
                    "trade_count": i + 1,
                }
            )

    # Calculate max drawdown
    peak = account.initial_capital
    max_drawdown = 0
    max_drawdown_pct = 0
    for point in equity_points:
        if point["equity"] > peak:
            peak = point["equity"]
        dd = peak - point["equity"]
        if dd > max_drawdown:
            max_drawdown = dd
            max_drawdown_pct = (dd / peak) * 100 if peak > 0 else 0

    # Calculate profit factor
    gains = sum(t.actual_pnl for t in trades if t.actual_pnl and t.actual_pnl > 0)
    losses_abs = abs(sum(t.actual_pnl for t in trades if t.actual_pnl and t.actual_pnl < 0))
    profit_factor = gains / losses_abs if losses_abs > 0 else 0

    # Best/worst trades
    resolved = [t for t in trades if t.actual_pnl is not None]
    best_trade = max((t.actual_pnl for t in resolved), default=0)
    worst_trade = min((t.actual_pnl for t in resolved), default=0)
    avg_win = gains / account.winning_trades if account.winning_trades > 0 else 0
    avg_loss = losses_abs / account.losing_trades if account.losing_trades > 0 else 0

    # Unrealized P&L
    unrealized_pnl = sum(p.unrealized_pnl for p in positions)
    book_value = sum(p.entry_cost for p in positions)
    market_value = sum(p.quantity * (p.current_price or p.entry_price) for p in positions)

    return {
        "account_id": account_id,
        "initial_capital": account.initial_capital,
        "current_capital": account.current_capital,
        "equity_points": equity_points,
        "summary": {
            "total_trades": account.total_trades,
            "winning_trades": account.winning_trades,
            "losing_trades": account.losing_trades,
            "open_trades": len([t for t in trades if t.status.value == "open"]),
            "total_invested": total_invested,
            "total_returned": total_returned,
            "realized_pnl": account.total_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": account.total_pnl + unrealized_pnl,
            "book_value": book_value,
            "market_value": market_value,
            "max_drawdown": max_drawdown,
            "max_drawdown_pct": max_drawdown_pct,
            "profit_factor": profit_factor,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "win_rate": account.winning_trades / account.total_trades * 100 if account.total_trades > 0 else 0,
            "roi_percent": (account.current_capital - account.initial_capital) / account.initial_capital * 100
            if account.initial_capital > 0
            else 0,
        },
    }


@simulation_router.get("/accounts/{account_id}/trades")
async def get_account_trades(account_id: str, limit: int = Query(default=50, ge=1, le=500)):
    """Get trade history for a simulation account"""
    trades = await simulation_service.get_trade_history(account_id, limit)
    return [
        {
            "id": trade.id,
            "opportunity_id": trade.opportunity_id,
            "strategy_type": trade.strategy_type,
            "total_cost": trade.total_cost,
            "expected_profit": trade.expected_profit,
            "slippage": trade.slippage,
            "status": trade.status.value,
            "actual_payout": trade.actual_payout,
            "actual_pnl": trade.actual_pnl,
            "fees_paid": trade.fees_paid,
            "executed_at": trade.executed_at.isoformat(),
            "resolved_at": trade.resolved_at.isoformat() if trade.resolved_at else None,
            "copied_from": trade.copied_from_wallet,
        }
        for trade in trades
    ]


# ==================== TRADING ====================


@simulation_router.post("/accounts/{account_id}/execute")
async def execute_opportunity(
    account_id: str,
    request: ExecuteTradeRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Execute an arbitrage opportunity in simulation"""
    from services import shared_state

    opportunities = await shared_state.get_opportunities_from_db(session, None)
    opportunity = next((o for o in opportunities if o.id == request.opportunity_id), None)

    if not opportunity:
        raise HTTPException(status_code=404, detail=f"Opportunity not found: {request.opportunity_id}")

    try:
        trade = await simulation_service.execute_opportunity(
            account_id=account_id,
            opportunity=opportunity,
            position_size=request.position_size,
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
        )

        return {
            "trade_id": trade.id,
            "status": trade.status.value,
            "total_cost": trade.total_cost,
            "expected_profit": trade.expected_profit,
            "slippage": trade.slippage,
            "message": "Trade executed successfully in simulation",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@simulation_router.post("/trades/{trade_id}/resolve")
async def resolve_trade(
    trade_id: str,
    winning_outcome: str = Query(..., description="The outcome that won (YES or NO)"),
):
    """Manually resolve a simulated trade (for testing)"""
    try:
        trade = await simulation_service.resolve_trade(trade_id=trade_id, winning_outcome=winning_outcome)

        return {
            "trade_id": trade.id,
            "status": trade.status.value,
            "actual_payout": trade.actual_payout,
            "actual_pnl": trade.actual_pnl,
            "fees_paid": trade.fees_paid,
            "message": f"Trade resolved as {trade.status.value}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== PERFORMANCE ====================


@simulation_router.get("/accounts/{account_id}/performance")
async def get_account_performance(account_id: str):
    """Get detailed performance metrics for an account"""
    stats = await simulation_service.get_account_stats(account_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Account not found")

    trades = await simulation_service.get_trade_history(account_id, 1000)

    # Calculate additional metrics
    profits = [t.actual_pnl for t in trades if t.actual_pnl and t.actual_pnl > 0]
    losses = [t.actual_pnl for t in trades if t.actual_pnl and t.actual_pnl < 0]

    avg_win = sum(profits) / len(profits) if profits else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # Calculate max drawdown
    cumulative_pnl = 0
    peak = 0
    max_drawdown = 0
    for trade in reversed(trades):
        if trade.actual_pnl:
            cumulative_pnl += trade.actual_pnl
            if cumulative_pnl > peak:
                peak = cumulative_pnl
            drawdown = peak - cumulative_pnl
            if drawdown > max_drawdown:
                max_drawdown = drawdown

    return {
        **stats,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": abs(sum(profits) / sum(losses)) if losses else 0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": (max_drawdown / stats["initial_capital"]) * 100 if stats["initial_capital"] > 0 else 0,
    }
