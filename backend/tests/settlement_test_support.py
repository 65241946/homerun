from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from models.database import (
    OnlineMarketResolution,
    SimulationAccount,
    TraderOrder,
)
from services.simulation import SimulationService
from services.simulation_ledger import initialize_new_v2_account


@dataclass(frozen=True)
class SeededShadowOrder:
    account_id: str
    order_id: str
    trade_id: str
    position_id: str
    resolution_id: str
    condition_id: str
    held_token_id: str
    winning_token_id: str | None


def _evidence_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _proof_condition_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) == 66 and normalized.startswith("0x"):
        try:
            bytes.fromhex(normalized[2:])
        except ValueError:
            pass
        else:
            return normalized
    return "0x" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _proof_token_id(condition_id: str, outcome_index: int) -> str:
    digest = hashlib.sha256(f"{condition_id}:{outcome_index}".encode()).digest()
    return str(int.from_bytes(digest, byteorder="big", signed=False))


async def seed_shadow_order(
    session_factory,
    *,
    account_id: str,
    order_id: str,
    condition_id: str,
    provider_market_id: str,
    initial_capital: float = 1_000.0,
    notional_usd: float = 100.0,
    entry_price: float = 0.4,
    held_outcome_index: int = 0,
    winning_outcome_index: int | None = 0,
    resolution_state: str = "final",
    identity_status: str = "complete",
    mode: str = "shadow",
    order_status: str = "open",
    include_simulation_refs: bool = True,
) -> SeededShadowOrder:
    if held_outcome_index not in {0, 1}:
        raise ValueError("held_outcome_index must be 0 or 1")
    if winning_outcome_index not in {None, 0, 1}:
        raise ValueError("winning_outcome_index must be 0, 1, or None")

    condition_id = _proof_condition_id(condition_id)
    token_ids = [
        _proof_token_id(condition_id, 0),
        _proof_token_id(condition_id, 1),
    ]
    outcomes = ["YES", "NO"]
    held_token_id = token_ids[held_outcome_index]
    winning_token_id = token_ids[winning_outcome_index] if winning_outcome_index is not None else None
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        account = await session.get(SimulationAccount, account_id)
        if account is None:
            account = SimulationAccount(
                id=account_id,
                name=f"Settlement proof {account_id}",
                initial_capital=initial_capital,
                current_capital=initial_capital,
                ledger_version=2,
                ledger_integrity_status="complete",
            )
            session.add(account)
            initialize_new_v2_account(session, account)
            await session.commit()

    direction = "buy_yes" if held_outcome_index == 0 else "buy_no"
    async with session_factory() as session:
        opened = await SimulationService().record_orchestrator_shadow_fill(
            account_id=account_id,
            trader_id=f"trader-{account_id}",
            signal_id=f"signal-{order_id}",
            market_id=provider_market_id,
            market_question=f"Settlement proof for {condition_id}",
            direction=direction,
            notional_usd=notional_usd,
            entry_price=entry_price,
            strategy_type="settlement_proof",
            token_id=held_token_id,
            payload={
                "market": {
                    "condition_id": condition_id,
                    "token_ids": token_ids,
                    "outcomes": outcomes,
                }
            },
            session=session,
            commit=False,
        )

        resolution_id = f"resolution-{order_id}"
        resolution = OnlineMarketResolution(
            id=resolution_id,
            venue="polymarket",
            provider="gamma",
            provider_market_id=provider_market_id,
            condition_id=condition_id,
            token_ids_json=token_ids,
            outcomes_json=outcomes,
            outcome_prices_json=(
                ["1", "0"]
                if winning_outcome_index == 0
                else ["0", "1"]
                if winning_outcome_index == 1
                else ["0.5", "0.5"]
            ),
            state=resolution_state,
            winning_token_id=winning_token_id,
            winning_outcome_index=winning_outcome_index,
            winning_outcome=(outcomes[winning_outcome_index] if winning_outcome_index is not None else None),
            provider_resolved_at=now if resolution_state == "final" else None,
            first_observed_at=now,
            last_observed_at=now,
            finalized_at=now if resolution_state == "final" else None,
            fact_version=1,
            evidence_hash=_evidence_hash(
                condition_id,
                resolution_state,
                winning_token_id or "",
            ),
            evidence_json={
                "provider": "gamma",
                "condition_id": condition_id,
                "state": resolution_state,
                "winning_token_id": winning_token_id,
            },
            created_at=now,
            updated_at=now,
        )
        session.add(resolution)

        simulation_ledger = (
            {
                "account_id": account_id,
                "trade_id": opened["trade_id"],
                "position_id": opened["position_id"],
            }
            if include_simulation_refs
            else {}
        )
        order = TraderOrder(
            id=order_id,
            trader_id=f"trader-{account_id}",
            signal_id=f"signal-{order_id}",
            source="scanner",
            strategy_key="settlement_proof",
            market_id=provider_market_id,
            venue="polymarket",
            provider_market_id=provider_market_id,
            condition_id=condition_id,
            token_id=held_token_id,
            outcome_index=held_outcome_index,
            identity_status=identity_status,
            market_question=f"Settlement proof for {condition_id}",
            direction=direction,
            mode=mode,
            status=order_status,
            notional_usd=notional_usd,
            entry_price=entry_price,
            effective_price=entry_price,
            actual_profit=None,
            payload_json={
                "market": {
                    "condition_id": condition_id,
                    "token_ids": token_ids,
                    "outcomes": outcomes,
                },
                "simulation_ledger": simulation_ledger,
            },
            created_at=now,
            executed_at=now,
            updated_at=now,
        )
        session.add(order)
        await session.commit()

    return SeededShadowOrder(
        account_id=account_id,
        order_id=order_id,
        trade_id=str(opened["trade_id"]),
        position_id=str(opened["position_id"]),
        resolution_id=resolution_id,
        condition_id=condition_id,
        held_token_id=held_token_id,
        winning_token_id=winning_token_id,
    )


async def load_seeded_rows(session, seeded: SeededShadowOrder):
    from models.database import SimulationPosition, SimulationTrade

    account = await session.get(SimulationAccount, seeded.account_id)
    order = await session.get(TraderOrder, seeded.order_id)
    trade = await session.get(SimulationTrade, seeded.trade_id)
    position = await session.get(SimulationPosition, seeded.position_id)
    resolution = (
        await session.execute(select(OnlineMarketResolution).where(OnlineMarketResolution.id == seeded.resolution_id))
    ).scalar_one()
    return account, order, trade, position, resolution
