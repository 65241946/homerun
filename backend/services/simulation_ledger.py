"""Proof-grade append-only cash journal for HOMERUN Shadow v2 accounts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import SimulationAccount, SimulationCashLedgerEntry

USDC_QUANTUM = Decimal("0.000001")
SHARE_QUANTUM = Decimal("0.000000000000000001")


class SimulationLedgerError(RuntimeError):
    """Base error for proof-grade simulation ledger operations."""


class LedgerNotEnabled(SimulationLedgerError):
    """Raised when a journal write targets a legacy account."""


class LedgerIntegrityBlocked(SimulationLedgerError):
    """Raised when account integrity state blocks economic writes."""


class LedgerIdempotencyConflict(SimulationLedgerError):
    """Raised when one idempotency key is reused for different economics."""


class LedgerAlreadyReversed(SimulationLedgerError):
    """Raised when an immutable cash entry already has a reversal."""


class LedgerConcurrencyError(SimulationLedgerError):
    """Raised when a database conflict cannot be resolved deterministically."""


@dataclass(frozen=True, slots=True)
class CashLedgerWriteResult:
    entry: SimulationCashLedgerEntry
    inserted: bool


@dataclass(frozen=True, slots=True)
class LedgerIntegritySnapshot:
    account_id: str
    ledger_version: int
    status: str
    coverage: str
    initial_capital_usdc: Decimal
    journal_delta_usdc: Decimal
    rebuilt_balance_usdc: Decimal
    projected_balance_usdc: Decimal
    difference_usdc: Decimal
    entry_count: int
    max_sequence: int
    sequence_contiguous: bool


@dataclass(frozen=True, slots=True)
class ShadowCloseEconomics:
    quantity: Decimal
    close_price: Decimal
    cost_basis_usdc: Decimal
    gross_payout_usdc: Decimal
    gross_pnl_usdc: Decimal
    explicit_close_fee_usdc: Decimal
    winner_fee_usdc: Decimal
    total_fee_usdc: Decimal
    net_payout_usdc: Decimal
    realized_pnl_usdc: Decimal
    won: bool


def quantize_usdc(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid USDC amount")  # noqa: TRY004 - stable service contract
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid USDC amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"USDC amount must be finite: {value!r}")
    return amount.quantize(USDC_QUANTUM, rounding=ROUND_HALF_UP)


def _finite_decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"boolean is not a valid {field}")  # noqa: TRY004 - stable service contract
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite: {value!r}")
    return result


def calculate_shadow_close_economics(
    *,
    quantity: Any,
    entry_cost_usdc: Any,
    close_price: Any,
    explicit_close_fee_usdc: Any = 0,
    winner_fee_rate: Any = Decimal("0.02"),
) -> ShadowCloseEconomics:
    """Return the canonical six-decimal Shadow close cash economics."""

    normalized_quantity = _finite_decimal(quantity, field="quantity").quantize(
        SHARE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    normalized_close_price = _finite_decimal(close_price, field="close_price")
    cost_basis = quantize_usdc(entry_cost_usdc)
    explicit_fee = quantize_usdc(explicit_close_fee_usdc)
    fee_rate = _finite_decimal(winner_fee_rate, field="winner_fee_rate")
    if normalized_quantity < 0:
        raise ValueError("quantity cannot be negative")
    if normalized_close_price < 0:
        raise ValueError("close_price cannot be negative")
    if cost_basis < 0:
        raise ValueError("entry_cost_usdc cannot be negative")
    if explicit_fee < 0:
        raise ValueError("explicit_close_fee_usdc cannot be negative")
    if fee_rate < 0:
        raise ValueError("winner_fee_rate cannot be negative")

    gross_payout = quantize_usdc(normalized_quantity * normalized_close_price)
    gross_pnl = quantize_usdc(gross_payout - cost_basis)
    winner_fee = quantize_usdc(max(Decimal(0), gross_pnl) * fee_rate)
    total_fee = quantize_usdc(explicit_fee + winner_fee)
    net_payout = quantize_usdc(max(Decimal(0), gross_payout - total_fee))
    realized_pnl = quantize_usdc(net_payout - cost_basis)
    return ShadowCloseEconomics(
        quantity=normalized_quantity,
        close_price=normalized_close_price,
        cost_basis_usdc=cost_basis,
        gross_payout_usdc=gross_payout,
        gross_pnl_usdc=gross_pnl,
        explicit_close_fee_usdc=explicit_fee,
        winner_fee_usdc=winner_fee,
        total_fee_usdc=total_fee,
        net_payout_usdc=net_payout,
        realized_pnl_usdc=realized_pnl,
        won=realized_pnl >= 0,
    )


def _now_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _stable_id(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}\x1f{value}".encode()).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"{quantize_usdc(value):.6f}"
    if isinstance(value, datetime):
        return _now_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _combined_evidence(
    evidence: Mapping[str, Any] | None,
    authoritative: Mapping[str, Any],
) -> dict[str, Any]:
    combined = _json_safe(dict(evidence or {}))
    combined.update(_json_safe(dict(authoritative)))
    return combined


def initialize_new_v2_account(
    session: AsyncSession,
    account: SimulationAccount,
    *,
    occurred_at: datetime | None = None,
) -> SimulationCashLedgerEntry:
    """Mark a newly-created account v2 and append its zero-delta opening marker."""

    if not str(account.id or "").strip():
        raise ValueError("account.id is required before ledger initialization")
    initial = quantize_usdc(account.initial_capital)
    if initial <= 0:
        raise ValueError("initial capital must be greater than zero")

    now = _now_utc(occurred_at)
    account.initial_capital = float(initial)
    account.current_capital = float(initial)
    account.ledger_version = 2
    account.ledger_integrity_status = "complete"
    account.ledger_verified_at = now

    key = f"shadow-opening:{account.id}"
    entry = SimulationCashLedgerEntry(
        id=_stable_id("simulation-cash-entry-v1", key),
        account_id=str(account.id),
        ledger_sequence=1,
        entry_type="opening_balance",
        amount_usdc=Decimal("0.000000"),
        currency="USDC",
        occurred_at=now,
        idempotency_key=key,
        reversal_of_entry_id=None,
        evidence_json={
            "kind": "v2_account_opening",
            "initial_capital_usdc": f"{initial:.6f}",
            "balance_formula": "initial_capital_plus_journal_delta",
        },
        created_at=now,
    )
    session.add(entry)
    return entry


async def _lock_account(session: AsyncSession, account_id: str) -> SimulationAccount:
    account = (
        await session.execute(
            select(SimulationAccount)
            .where(SimulationAccount.id == str(account_id))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise ValueError(f"Simulation account not found: {account_id}")
    return account


def _validate_existing_entry(
    existing: SimulationCashLedgerEntry,
    *,
    account_id: str,
    entry_type: str,
    amount_usdc: Decimal,
    simulation_trade_id: str | None,
    trader_order_id: str | None,
    trader_order_settlement_id: str | None,
    reversal_of_entry_id: str | None,
    economic_evidence: Mapping[str, Any],
) -> None:
    expected = {
        "account_id": str(account_id),
        "entry_type": entry_type,
        "amount_usdc": quantize_usdc(amount_usdc),
        "simulation_trade_id": simulation_trade_id,
        "trader_order_id": trader_order_id,
        "trader_order_settlement_id": trader_order_settlement_id,
        "reversal_of_entry_id": reversal_of_entry_id,
    }
    actual = {
        "account_id": str(existing.account_id),
        "entry_type": str(existing.entry_type),
        "amount_usdc": quantize_usdc(existing.amount_usdc),
        "simulation_trade_id": existing.simulation_trade_id,
        "trader_order_id": existing.trader_order_id,
        "trader_order_settlement_id": existing.trader_order_settlement_id,
        "reversal_of_entry_id": existing.reversal_of_entry_id,
    }
    for key, expected_value in expected.items():
        if actual[key] != expected_value:
            raise LedgerIdempotencyConflict(
                f"idempotency key already exists with different {key}: "
                f"expected={expected_value!r} actual={actual[key]!r}"
            )

    stored_evidence = existing.evidence_json if isinstance(existing.evidence_json, Mapping) else {}
    for key, expected_value in _json_safe(dict(economic_evidence)).items():
        if stored_evidence.get(key) != expected_value:
            raise LedgerIdempotencyConflict(
                f"idempotency key already exists with different evidence.{key}"
            )


async def _append_cash_entry(
    session: AsyncSession,
    *,
    account_id: str,
    entry_type: str,
    amount_usdc: Decimal,
    idempotency_key: str,
    simulation_trade_id: str | None = None,
    trader_order_id: str | None = None,
    trader_order_settlement_id: str | None = None,
    reversal_of_entry_id: str | None = None,
    economic_evidence: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
    apply_projection: bool = True,
    allow_legacy: bool = False,
    allow_integrity_repair: bool = False,
) -> CashLedgerWriteResult:
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")
    amount = quantize_usdc(amount_usdc)
    now = _now_utc(occurred_at)
    economic = dict(economic_evidence or {})

    account = await _lock_account(session, account_id)
    ledger_version = int(account.ledger_version or 1)
    integrity_status = str(account.ledger_integrity_status or "legacy")
    if ledger_version < 2 and not allow_legacy:
        raise LedgerNotEnabled(f"Simulation account {account_id} is not a v2 ledger account")
    if integrity_status in {"mismatch", "blocked"} and not allow_integrity_repair:
        raise LedgerIntegrityBlocked(
            f"Simulation account {account_id} ledger is {integrity_status}; economic writes are blocked"
        )

    existing = (
        await session.execute(
            select(SimulationCashLedgerEntry).where(
                SimulationCashLedgerEntry.idempotency_key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        _validate_existing_entry(
            existing,
            account_id=account_id,
            entry_type=entry_type,
            amount_usdc=amount,
            simulation_trade_id=simulation_trade_id,
            trader_order_id=trader_order_id,
            trader_order_settlement_id=trader_order_settlement_id,
            reversal_of_entry_id=reversal_of_entry_id,
            economic_evidence=economic,
        )
        return CashLedgerWriteResult(existing, False)

    max_sequence = await session.scalar(
        select(func.max(SimulationCashLedgerEntry.ledger_sequence)).where(
            SimulationCashLedgerEntry.account_id == str(account_id)
        )
    )
    next_sequence = int(max_sequence or 0) + 1
    entry_id = _stable_id("simulation-cash-entry-v1", key)
    evidence_json = _combined_evidence(evidence, economic)
    statement = (
        pg_insert(SimulationCashLedgerEntry)
        .values(
            id=entry_id,
            account_id=str(account_id),
            simulation_trade_id=simulation_trade_id,
            trader_order_id=trader_order_id,
            trader_order_settlement_id=trader_order_settlement_id,
            ledger_sequence=next_sequence,
            entry_type=entry_type,
            amount_usdc=amount,
            currency="USDC",
            occurred_at=now,
            idempotency_key=key,
            reversal_of_entry_id=reversal_of_entry_id,
            evidence_json=evidence_json,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(SimulationCashLedgerEntry.id)
    )
    inserted_id = await session.scalar(statement)
    if inserted_id is None:
        existing = (
            await session.execute(
                select(SimulationCashLedgerEntry).where(
                    SimulationCashLedgerEntry.idempotency_key == key
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise LedgerConcurrencyError(
                f"cash entry conflict did not resolve to idempotency key {key!r}"
            )
        _validate_existing_entry(
            existing,
            account_id=account_id,
            entry_type=entry_type,
            amount_usdc=amount,
            simulation_trade_id=simulation_trade_id,
            trader_order_id=trader_order_id,
            trader_order_settlement_id=trader_order_settlement_id,
            reversal_of_entry_id=reversal_of_entry_id,
            economic_evidence=economic,
        )
        return CashLedgerWriteResult(existing, False)

    if apply_projection:
        projected = quantize_usdc(account.current_capital) + amount
        account.current_capital = float(quantize_usdc(projected))
        account.updated_at = now

    entry = (
        await session.execute(
            select(SimulationCashLedgerEntry).where(
                SimulationCashLedgerEntry.id == str(inserted_id)
            )
        )
    ).scalar_one()
    await session.flush()
    return CashLedgerWriteResult(entry, True)


async def record_entry_debit(
    session: AsyncSession,
    *,
    account_id: str,
    simulation_trade_id: str,
    entry_cost_usdc: Any,
    trader_order_id: str | None = None,
    idempotency_key: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> CashLedgerWriteResult:
    cost = quantize_usdc(entry_cost_usdc)
    if cost <= 0:
        raise ValueError("entry_cost_usdc must be greater than zero")
    trade_id = str(simulation_trade_id or "").strip()
    if not trade_id:
        raise ValueError("simulation_trade_id is required")
    return await _append_cash_entry(
        session,
        account_id=account_id,
        entry_type="entry_debit",
        amount_usdc=-cost,
        idempotency_key=idempotency_key or f"shadow-open:{trade_id}",
        simulation_trade_id=trade_id,
        trader_order_id=trader_order_id,
        economic_evidence={"entry_cost_usdc": f"{cost:.6f}"},
        evidence=evidence,
        occurred_at=occurred_at,
    )


async def record_settlement_credit(
    session: AsyncSession,
    *,
    account_id: str,
    idempotency_key: str,
    payout_usdc: Any,
    realized_pnl_usdc: Any,
    won: bool,
    simulation_trade_id: str | None = None,
    trader_order_id: str | None = None,
    trader_order_settlement_id: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> CashLedgerWriteResult:
    payout = quantize_usdc(payout_usdc)
    pnl = quantize_usdc(realized_pnl_usdc)
    if payout < 0:
        raise ValueError("payout_usdc cannot be negative")
    if not isinstance(won, bool):
        raise ValueError("won must be a boolean")  # noqa: TRY004 - stable service contract

    result = await _append_cash_entry(
        session,
        account_id=account_id,
        entry_type="settlement_credit",
        amount_usdc=payout,
        idempotency_key=idempotency_key,
        simulation_trade_id=simulation_trade_id,
        trader_order_id=trader_order_id,
        trader_order_settlement_id=trader_order_settlement_id,
        economic_evidence={
            "payout_usdc": f"{payout:.6f}",
            "realized_pnl_usdc": f"{pnl:.6f}",
            "won": won,
        },
        evidence=evidence,
        occurred_at=occurred_at,
    )
    if result.inserted:
        account = await _lock_account(session, account_id)
        account.total_pnl = float(
            quantize_usdc(quantize_usdc(account.total_pnl) + pnl)
        )
        if won:
            account.winning_trades = int(account.winning_trades or 0) + 1
        else:
            account.losing_trades = int(account.losing_trades or 0) + 1
        await session.flush()
    return result


async def reverse_cash_entry(
    session: AsyncSession,
    *,
    entry_id: str,
    idempotency_key: str,
    reason: str,
    operator: str | None = None,
    occurred_at: datetime | None = None,
) -> CashLedgerWriteResult:
    initial = await session.get(SimulationCashLedgerEntry, str(entry_id))
    if initial is None:
        raise ValueError(f"Cash ledger entry not found: {entry_id}")
    account = await _lock_account(session, str(initial.account_id))
    original = (
        await session.execute(
            select(SimulationCashLedgerEntry)
            .where(SimulationCashLedgerEntry.id == str(entry_id))
            .with_for_update()
        )
    ).scalar_one()
    if original.entry_type in {"opening_balance", "reversal"}:
        raise ValueError(f"entry type {original.entry_type!r} cannot be reversed")
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("reversal reason is required")

    prior_reversal = (
        await session.execute(
            select(SimulationCashLedgerEntry).where(
                SimulationCashLedgerEntry.reversal_of_entry_id == original.id
            )
        )
    ).scalar_one_or_none()
    if prior_reversal is not None:
        if str(prior_reversal.idempotency_key) != str(idempotency_key):
            raise LedgerAlreadyReversed(
                f"Cash ledger entry {entry_id} was already reversed by {prior_reversal.id}"
            )
        return CashLedgerWriteResult(prior_reversal, False)

    original_amount = quantize_usdc(original.amount_usdc)
    result = await _append_cash_entry(
        session,
        account_id=str(original.account_id),
        entry_type="reversal",
        amount_usdc=-original_amount,
        idempotency_key=idempotency_key,
        simulation_trade_id=original.simulation_trade_id,
        trader_order_id=original.trader_order_id,
        trader_order_settlement_id=original.trader_order_settlement_id,
        reversal_of_entry_id=str(original.id),
        economic_evidence={
            "reversal_of_entry_id": str(original.id),
            "reversed_amount_usdc": f"{original_amount:.6f}",
        },
        evidence={
            "kind": "cash_entry_reversal",
            "reason": normalized_reason,
            "operator": str(operator or "").strip() or None,
            "reversed_entry_type": str(original.entry_type),
        },
        occurred_at=occurred_at,
        allow_integrity_repair=True,
    )

    if result.inserted and original.entry_type == "settlement_credit":
        original_evidence = original.evidence_json if isinstance(original.evidence_json, Mapping) else {}
        pnl = quantize_usdc(original_evidence.get("realized_pnl_usdc", 0))
        account.total_pnl = float(
            quantize_usdc(quantize_usdc(account.total_pnl) - pnl)
        )
        if original_evidence.get("won") is True:
            account.winning_trades = max(0, int(account.winning_trades or 0) - 1)
        else:
            account.losing_trades = max(0, int(account.losing_trades or 0) - 1)
        await session.flush()
    return result


async def checkpoint_legacy_account(
    session: AsyncSession,
    *,
    account_id: str,
    reason: str,
    operator: str,
    occurred_at: datetime | None = None,
) -> CashLedgerWriteResult:
    normalized_reason = str(reason or "").strip()
    normalized_operator = str(operator or "").strip()
    if not normalized_reason or not normalized_operator:
        raise ValueError("checkpoint reason and operator are required")

    account = await _lock_account(session, account_id)
    version = int(account.ledger_version or 1)
    status = str(account.ledger_integrity_status or "legacy")
    if version >= 2 and status != "checkpointed":
        raise SimulationLedgerError(
            f"Account {account_id} is already v{version}/{status} and cannot be legacy-checkpointed"
        )
    if version < 2 and status != "legacy":
        raise SimulationLedgerError(
            f"Account {account_id} has inconsistent legacy state v{version}/{status}"
        )

    initial = quantize_usdc(account.initial_capital)
    projected = quantize_usdc(account.current_capital)
    checkpoint_delta = quantize_usdc(projected - initial)
    result = await _append_cash_entry(
        session,
        account_id=account_id,
        entry_type="manual_adjustment",
        amount_usdc=checkpoint_delta,
        idempotency_key=f"shadow-checkpoint:{account_id}:v2",
        economic_evidence={
            "checkpoint_delta_usdc": f"{checkpoint_delta:.6f}",
            "checkpoint_balance_usdc": f"{projected:.6f}",
        },
        evidence={
            "kind": "legacy_checkpoint",
            "reason": normalized_reason,
            "operator": normalized_operator,
            "prior_ledger_version": 1,
            "coverage": "checkpoint_forward_only",
        },
        occurred_at=occurred_at,
        apply_projection=False,
        allow_legacy=True,
        allow_integrity_repair=True,
    )
    account.ledger_version = 2
    account.ledger_integrity_status = "checkpointed"
    account.ledger_verified_at = _now_utc(occurred_at)
    await session.flush()
    return result


async def get_ledger_integrity(
    session: AsyncSession,
    account_id: str,
    *,
    lock: bool = False,
) -> LedgerIntegritySnapshot:
    query = select(SimulationAccount).where(SimulationAccount.id == str(account_id))
    if lock:
        query = query.with_for_update()
    account = (await session.execute(query)).scalar_one_or_none()
    if account is None:
        raise ValueError(f"Simulation account not found: {account_id}")

    count_value, delta_value, min_sequence, max_sequence = (
        await session.execute(
            select(
                func.count(SimulationCashLedgerEntry.id),
                func.coalesce(func.sum(SimulationCashLedgerEntry.amount_usdc), 0),
                func.min(SimulationCashLedgerEntry.ledger_sequence),
                func.max(SimulationCashLedgerEntry.ledger_sequence),
            ).where(SimulationCashLedgerEntry.account_id == str(account_id))
        )
    ).one()
    entry_count = int(count_value or 0)
    journal_delta = quantize_usdc(delta_value or 0)
    initial = quantize_usdc(account.initial_capital)
    rebuilt = quantize_usdc(initial + journal_delta)
    projected = quantize_usdc(account.current_capital)
    difference = quantize_usdc(projected - rebuilt)
    max_seq = int(max_sequence or 0)
    contiguous = bool(
        entry_count > 0
        and int(min_sequence or 0) == 1
        and max_seq == entry_count
    )

    version = int(account.ledger_version or 1)
    stored_status = str(account.ledger_integrity_status or "legacy")
    coverage = "legacy"
    expected_status = "legacy"
    if version >= 2:
        first_entry = (
            await session.execute(
                select(SimulationCashLedgerEntry)
                .where(SimulationCashLedgerEntry.account_id == str(account_id))
                .order_by(SimulationCashLedgerEntry.ledger_sequence)
                .limit(1)
            )
        ).scalar_one_or_none()
        first_evidence = (
            first_entry.evidence_json
            if first_entry is not None and isinstance(first_entry.evidence_json, Mapping)
            else {}
        )
        checkpointed = first_evidence.get("kind") == "legacy_checkpoint"
        coverage = "checkpoint" if checkpointed else "full"
        expected_status = "checkpointed" if checkpointed else "complete"

    if stored_status == "blocked":
        status = "blocked"
    elif version < 2:
        status = "legacy"
    elif difference != Decimal("0.000000") or not contiguous:
        status = "mismatch"
    else:
        status = expected_status

    return LedgerIntegritySnapshot(
        account_id=str(account.id),
        ledger_version=version,
        status=status,
        coverage=coverage,
        initial_capital_usdc=initial,
        journal_delta_usdc=journal_delta,
        rebuilt_balance_usdc=rebuilt,
        projected_balance_usdc=projected,
        difference_usdc=difference,
        entry_count=entry_count,
        max_sequence=max_seq,
        sequence_contiguous=contiguous,
    )


async def verify_ledger_integrity(
    session: AsyncSession,
    account_id: str,
    *,
    occurred_at: datetime | None = None,
) -> LedgerIntegritySnapshot:
    snapshot = await get_ledger_integrity(session, account_id, lock=True)
    account = await session.get(SimulationAccount, str(account_id))
    assert account is not None
    account.ledger_integrity_status = snapshot.status
    account.ledger_verified_at = _now_utc(occurred_at)
    await session.flush()
    return snapshot


__all__ = [
    "SHARE_QUANTUM",
    "USDC_QUANTUM",
    "CashLedgerWriteResult",
    "LedgerAlreadyReversed",
    "LedgerConcurrencyError",
    "LedgerIdempotencyConflict",
    "LedgerIntegrityBlocked",
    "LedgerIntegritySnapshot",
    "LedgerNotEnabled",
    "ShadowCloseEconomics",
    "SimulationLedgerError",
    "calculate_shadow_close_economics",
    "checkpoint_legacy_account",
    "get_ledger_integrity",
    "initialize_new_v2_account",
    "quantize_usdc",
    "record_entry_debit",
    "record_settlement_credit",
    "reverse_cash_entry",
    "verify_ledger_integrity",
]
