"""Cold-plane orchestration for online market-final observations and settlement.

The cycle deliberately separates its three phases:

1. read a bounded candidate batch in a short database session;
2. release that session before any provider HTTP call;
3. persist each observation and apply each order in independent transactions.

Only Shadow v2 economics are supported here.  Live mode remains fail-closed
until the separate Live verification adapter and readiness checks are present.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any

from sqlalchemy import func, or_, select, text

from config import settings
from models.database import AsyncSessionLocal, OnlineMarketResolution, TraderOrder
from services.market_identity import identity_from_order
from services.online_resolution import (
    GammaResolutionProvider,
    ResolutionLookup,
    ResolutionObservation,
    persist_observation,
)
from services.settlement_coordinator import (
    ACTIVE_SHADOW_ORDER_STATUSES,
    SAFE_IDENTITY_STATUSES,
    SettlementIdempotencyConflict,
    apply_shadow_market_resolution,
)

MAX_MARKETS_PER_CYCLE = 100
MAX_HTTP_CONCURRENCY = 4
PER_REQUEST_TIMEOUT_SECONDS = 8.0
SUPPORTED_RUNTIME_MODES = frozenset({"off", "observe", "shadow", "live"})


@dataclass(frozen=True, slots=True)
class SettlementMarketCandidate:
    venue: str
    condition_id: str
    provider_market_id: str | None
    resolution_id: str | None
    resolution_state: str | None

    @property
    def lookup(self) -> ResolutionLookup:
        return ResolutionLookup(
            condition_id=self.condition_id,
            provider_market_id=self.provider_market_id,
        )


@dataclass(frozen=True, slots=True)
class ResolutionFetchBatch:
    observations: tuple[ResolutionObservation, ...]
    network_errors: int
    errors: tuple[dict[str, str], ...]


def _normalize_runtime_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    if mode not in SUPPORTED_RUNTIME_MODES:
        allowed = ", ".join(sorted(SUPPORTED_RUNTIME_MODES))
        raise ValueError(f"Unsupported settlement runtime mode {value!r}; expected one of {allowed}")
    return mode


def _base_stats(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "health": "healthy",
        "live_status": "not_applicable",
        "candidates": 0,
        "observed": 0,
        "observation_inserted": 0,
        "final": 0,
        "would_settle": 0,
        "applied": 0,
        "idempotent_replays": 0,
        "manual_review": 0,
        "conflicted": 0,
        "blocked": 0,
        "network_errors": 0,
        "persistence_errors": 0,
        "apply_errors": 0,
        "claim_busy": 0,
        "duration_ms": 0,
        "errors": [],
    }


def _live_credentials_configured() -> bool:
    return bool(
        str(getattr(settings, "POLYMARKET_PRIVATE_KEY", "") or "").strip()
        and str(getattr(settings, "POLYMARKET_API_KEY", "") or "").strip()
        and str(getattr(settings, "POLYMARKET_API_SECRET", "") or "").strip()
        and str(getattr(settings, "POLYMARKET_API_PASSPHRASE", "") or "").strip()
    )


async def _load_candidate_batch(
    session_factory,
    *,
    max_markets: int,
) -> tuple[list[SettlementMarketCandidate], int]:
    """Return unique typed market keys and the number of unsafe active rows."""

    active_statuses = tuple(sorted(ACTIVE_SHADOW_ORDER_STATUSES))
    safe_identity_statuses = tuple(sorted(SAFE_IDENTITY_STATUSES))
    normalized_mode = func.lower(func.coalesce(TraderOrder.mode, ""))
    normalized_status = func.lower(func.coalesce(TraderOrder.status, ""))
    normalized_identity = func.lower(func.coalesce(TraderOrder.identity_status, ""))
    normalized_venue = func.lower(func.coalesce(TraderOrder.venue, "polymarket"))
    valid_condition_pattern = r"^0x[0-9a-fA-F]{64}$"
    active_scope = (
        normalized_mode.in_(("shadow", "live")),
        normalized_status.in_(active_statuses),
    )

    async with session_factory() as session:
        unsafe_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TraderOrder)
                .where(
                    *active_scope,
                    or_(
                        TraderOrder.condition_id.is_(None),
                        TraderOrder.condition_id == "",
                        TraderOrder.condition_id.op("!~")(valid_condition_pattern),
                        ~normalized_identity.in_(safe_identity_statuses),
                        normalized_venue != "polymarket",
                    ),
                )
            )
            or 0
        )

        provider_market_count = func.count(func.distinct(TraderOrder.provider_market_id))
        oldest_update = func.min(TraderOrder.updated_at)
        provider_conflicts = (
            select(
                normalized_venue.label("venue"),
                TraderOrder.condition_id.label("condition_id"),
            )
            .where(
                *active_scope,
                normalized_identity.in_(safe_identity_statuses),
                normalized_venue == "polymarket",
                TraderOrder.condition_id.is_not(None),
                TraderOrder.condition_id.op("~")(valid_condition_pattern),
            )
            .group_by(normalized_venue, TraderOrder.condition_id)
            .having(provider_market_count > 1)
            .subquery()
        )
        unsafe_count += int(await session.scalar(select(func.count()).select_from(provider_conflicts)) or 0)
        grouped_rows = (
            await session.execute(
                select(
                    normalized_venue.label("venue"),
                    TraderOrder.condition_id.label("condition_id"),
                    func.min(TraderOrder.provider_market_id).label("provider_market_id"),
                    provider_market_count.label("provider_market_count"),
                    oldest_update.label("oldest_update"),
                )
                .where(
                    *active_scope,
                    normalized_identity.in_(safe_identity_statuses),
                    normalized_venue == "polymarket",
                    TraderOrder.condition_id.is_not(None),
                    TraderOrder.condition_id != "",
                    TraderOrder.condition_id.op("~")(valid_condition_pattern),
                )
                .group_by(normalized_venue, TraderOrder.condition_id)
                .having(provider_market_count <= 1)
                .order_by(oldest_update.asc(), TraderOrder.condition_id.asc())
                .limit(max(1, int(max_markets)))
            )
        ).all()

        condition_ids = tuple(str(row.condition_id) for row in grouped_rows)
        current_by_identity: dict[tuple[str, str], OnlineMarketResolution] = {}
        if condition_ids:
            current_rows = (
                (
                    await session.execute(
                        select(OnlineMarketResolution).where(OnlineMarketResolution.condition_id.in_(condition_ids))
                    )
                )
                .scalars()
                .all()
            )
            current_by_identity = {
                (
                    str(row.venue or "").strip().lower(),
                    str(row.condition_id or "").strip().lower(),
                ): row
                for row in current_rows
            }

    candidates: list[SettlementMarketCandidate] = []
    for row in grouped_rows:
        venue = str(row.venue or "").strip().lower()
        condition_id = str(row.condition_id or "").strip().lower()
        provider_market_id = str(row.provider_market_id or "").strip() or None
        if venue != "polymarket":
            unsafe_count += 1
            continue
        if len(condition_id) != 66 or not condition_id.startswith("0x"):
            unsafe_count += 1
            continue
        try:
            int(condition_id, 0)
        except ValueError:
            unsafe_count += 1
            continue
        if int(row.provider_market_count or 0) > 1:
            unsafe_count += 1
            continue

        current = current_by_identity.get((venue, condition_id))
        candidates.append(
            SettlementMarketCandidate(
                venue=venue,
                condition_id=condition_id,
                provider_market_id=provider_market_id,
                resolution_id=str(current.id) if current is not None else None,
                resolution_state=(str(current.state or "").strip().lower() if current is not None else None),
            )
        )
    remaining = max(0, int(max_markets) - len(candidates))
    if remaining:
        inferred_candidates = await _load_legacy_inferred_candidates(
            session_factory,
            max_markets=remaining,
            existing_candidates=candidates,
        )
        candidates.extend(inferred_candidates)
    return candidates, unsafe_count


async def _load_legacy_inferred_candidates(
    session_factory,
    *,
    max_markets: int,
    existing_candidates: Sequence[SettlementMarketCandidate],
) -> list[SettlementMarketCandidate]:
    """Discover legacy market identities without mutating historical orders."""

    if max_markets <= 0:
        return []

    active_statuses = tuple(sorted(ACTIVE_SHADOW_ORDER_STATUSES))
    safe_identity_statuses = tuple(sorted(SAFE_IDENTITY_STATUSES))
    normalized_mode = func.lower(func.coalesce(TraderOrder.mode, ""))
    normalized_status = func.lower(func.coalesce(TraderOrder.status, ""))
    normalized_identity = func.lower(func.coalesce(TraderOrder.identity_status, ""))
    scan_limit = max(1_000, int(max_markets) * 10)

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(TraderOrder)
                    .where(
                        normalized_mode.in_(("shadow", "live")),
                        normalized_status.in_(active_statuses),
                        ~normalized_identity.in_(safe_identity_statuses),
                    )
                    .order_by(TraderOrder.updated_at.asc().nullsfirst(), TraderOrder.id.asc())
                    .limit(scan_limit)
                )
            )
            .scalars()
            .all()
        )

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for order in rows:
            identity = identity_from_order(order)
            if (
                identity.status != "legacy_inferred"
                or identity.venue != "polymarket"
                or not identity.condition_id
                or not identity.token_id
                or identity.outcome_index is None
            ):
                continue
            key = (identity.venue, identity.condition_id)
            group = grouped.setdefault(
                key,
                {
                    "venue": identity.venue,
                    "condition_id": identity.condition_id,
                    "provider_market_ids": set(),
                },
            )
            if identity.provider_market_id:
                group["provider_market_ids"].add(identity.provider_market_id)

        existing_by_key = {(candidate.venue, candidate.condition_id): candidate for candidate in existing_candidates}
        selected: list[dict[str, Any]] = []
        for key, group in grouped.items():
            provider_ids = group["provider_market_ids"]
            if len(provider_ids) > 1:
                continue
            provider_market_id = next(iter(provider_ids), None)
            existing = existing_by_key.get(key)
            if existing is not None:
                if (
                    existing.provider_market_id
                    and provider_market_id
                    and existing.provider_market_id != provider_market_id
                ):
                    continue
                continue
            if len(selected) >= max_markets:
                break
            group["provider_market_id"] = provider_market_id
            selected.append(group)

        condition_ids = tuple(str(group["condition_id"]) for group in selected)
        current_by_identity: dict[tuple[str, str], OnlineMarketResolution] = {}
        if condition_ids:
            current_rows = (
                (
                    await session.execute(
                        select(OnlineMarketResolution).where(OnlineMarketResolution.condition_id.in_(condition_ids))
                    )
                )
                .scalars()
                .all()
            )
            current_by_identity = {
                (
                    str(row.venue or "").strip().lower(),
                    str(row.condition_id or "").strip().lower(),
                ): row
                for row in current_rows
            }

    candidates: list[SettlementMarketCandidate] = []
    for group in selected:
        venue = str(group["venue"])
        condition_id = str(group["condition_id"])
        current = current_by_identity.get((venue, condition_id))
        candidates.append(
            SettlementMarketCandidate(
                venue=venue,
                condition_id=condition_id,
                provider_market_id=group["provider_market_id"],
                resolution_id=str(current.id) if current is not None else None,
                resolution_state=(str(current.state or "").strip().lower() if current is not None else None),
            )
        )
    return candidates


async def _fetch_resolution_observations(
    lookups: Sequence[ResolutionLookup],
    *,
    provider,
    max_concurrency: int = MAX_HTTP_CONCURRENCY,
    request_timeout_seconds: float = PER_REQUEST_TIMEOUT_SECONDS,
) -> ResolutionFetchBatch:
    """Fetch a batch without fail-fast cancellation of unrelated markets."""

    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be positive")
    if request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_one(lookup: ResolutionLookup):
        try:
            async with semaphore:
                async with asyncio.timeout(request_timeout_seconds):
                    observation = await provider.fetch_one(lookup)
            if not isinstance(observation, ResolutionObservation):
                raise TypeError("resolution provider must return ResolutionObservation")
            return observation
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one provider lookup from the batch
            return {
                "condition_id": str(lookup.condition_id),
                "provider_market_id": str(lookup.provider_market_id or ""),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    results = await asyncio.gather(*(_fetch_one(lookup) for lookup in lookups))
    observations = tuple(result for result in results if isinstance(result, ResolutionObservation))
    errors = tuple(result for result in results if isinstance(result, dict))
    return ResolutionFetchBatch(
        observations=observations,
        network_errors=len(errors),
        errors=errors,
    )


async def _load_matching_orders(
    session_factory,
    *,
    venue: str,
    condition_id: str,
    runtime_mode: str,
) -> list[tuple[str, str]]:
    modes = ("shadow", "live") if runtime_mode == "observe" else ("shadow",)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(TraderOrder.id, TraderOrder.mode)
                .where(
                    func.lower(func.coalesce(TraderOrder.venue, "polymarket")) == venue,
                    func.lower(TraderOrder.condition_id) == condition_id,
                    func.lower(func.coalesce(TraderOrder.mode, "")).in_(modes),
                    func.lower(func.coalesce(TraderOrder.status, "")).in_(tuple(sorted(ACTIVE_SHADOW_ORDER_STATUSES))),
                    func.lower(func.coalesce(TraderOrder.identity_status, "")).in_(
                        tuple(sorted(SAFE_IDENTITY_STATUSES))
                    ),
                )
                .order_by(TraderOrder.id.asc())
            )
        ).all()
    return [(str(row.id), str(row.mode or "").strip().lower()) for row in rows]


async def _apply_shadow_resolution(
    session_factory,
    *,
    order_id: str,
    resolution_id: str,
):
    async with session_factory() as session, session.begin():
        claimed = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:claim_key, 0))"),
            {"claim_key": f"online-settlement:{order_id}"},
        )
        if not bool(claimed):
            return None
        return await apply_shadow_market_resolution(
            session,
            trader_order_id=order_id,
            online_market_resolution_id=resolution_id,
        )


async def run_online_settlement_cycle(
    *,
    runtime_mode: str | None = None,
    session_factory=AsyncSessionLocal,
    provider=None,
    max_markets: int = MAX_MARKETS_PER_CYCLE,
) -> dict[str, Any]:
    """Run one bounded observation/settlement cycle on the cold worker plane."""

    started = monotonic()
    mode = _normalize_runtime_mode(
        runtime_mode if runtime_mode is not None else getattr(settings, "HOMERUN_SETTLEMENT_RUNTIME_MODE", "observe")
    )
    stats = _base_stats(mode)

    if mode == "off":
        stats["health"] = "disabled"
        stats["duration_ms"] = int((monotonic() - started) * 1000)
        return stats

    if mode == "live":
        stats["applied"] = 0
        if not _live_credentials_configured():
            stats["health"] = "not_configured"
            stats["live_status"] = "not_configured"
            stats["duration_ms"] = int((monotonic() - started) * 1000)
            return stats
        stats["health"] = "readiness_blocked"
        stats["live_status"] = "adapter_not_ready"
        stats["error_code"] = "live_adapter_not_ready"
        stats["duration_ms"] = int((monotonic() - started) * 1000)
        return stats

    candidates, unsafe_count = await _load_candidate_batch(
        session_factory,
        max_markets=min(MAX_MARKETS_PER_CYCLE, max(1, int(max_markets))),
    )
    stats["candidates"] = len(candidates)
    stats["blocked"] = unsafe_count

    final_resolution_by_identity: dict[tuple[str, str], str] = {}
    lookups: list[ResolutionLookup] = []
    for candidate in candidates:
        if candidate.resolution_state == "final" and candidate.resolution_id:
            final_resolution_by_identity[(candidate.venue, candidate.condition_id)] = candidate.resolution_id
        elif candidate.resolution_state == "conflicted":
            stats["conflicted"] += 1
            stats["blocked"] += 1
        else:
            lookups.append(candidate.lookup)

    resolution_provider = provider or GammaResolutionProvider(
        request_timeout_seconds=PER_REQUEST_TIMEOUT_SECONDS,
        max_concurrency=MAX_HTTP_CONCURRENCY,
    )
    fetch_batch = await _fetch_resolution_observations(
        lookups,
        provider=resolution_provider,
        max_concurrency=MAX_HTTP_CONCURRENCY,
        request_timeout_seconds=PER_REQUEST_TIMEOUT_SECONDS,
    )
    stats["network_errors"] = fetch_batch.network_errors
    stats["errors"].extend(fetch_batch.errors)

    candidate_by_condition = {candidate.condition_id: candidate for candidate in candidates}
    for observation in fetch_batch.observations:
        try:
            async with session_factory() as session, session.begin():
                persisted = await persist_observation(
                    session,
                    observation,
                    commit=False,
                )
            stats["observed"] += 1
            if persisted.observation_inserted:
                stats["observation_inserted"] += 1
            candidate = candidate_by_condition.get(str(observation.condition_id or ""))
            if persisted.current_state == "final" and candidate is not None:
                final_resolution_by_identity[(candidate.venue, candidate.condition_id)] = persisted.resolution_id
            elif persisted.current_state == "conflicted":
                stats["conflicted"] += 1
                stats["blocked"] += 1
            elif observation.state == "invalid":
                stats["blocked"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - persist failures are isolated per market
            stats["persistence_errors"] += 1
            stats["blocked"] += 1
            stats["errors"].append(
                {
                    "condition_id": str(observation.condition_id or ""),
                    "provider_market_id": str(observation.provider_market_id or ""),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    stats["final"] = len(final_resolution_by_identity)
    for (venue, condition_id), resolution_id in sorted(final_resolution_by_identity.items()):
        matching_orders = await _load_matching_orders(
            session_factory,
            venue=venue,
            condition_id=condition_id,
            runtime_mode=mode,
        )
        stats["would_settle"] += len(matching_orders)
        if mode != "shadow":
            continue
        for order_id, order_mode in matching_orders:
            if order_mode != "shadow":
                continue
            try:
                result = await _apply_shadow_resolution(
                    session_factory,
                    order_id=order_id,
                    resolution_id=resolution_id,
                )
                if result is None:
                    stats["claim_busy"] += 1
                elif result.applied:
                    stats["applied"] += 1
                elif result.idempotent_replay:
                    stats["idempotent_replays"] += 1
                elif result.status == "manual_review":
                    stats["manual_review"] += 1
                    stats["blocked"] += 1
            except SettlementIdempotencyConflict as exc:
                stats["conflicted"] += 1
                stats["blocked"] += 1
                stats["errors"].append(
                    {
                        "order_id": order_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - apply failures are isolated per order
                stats["apply_errors"] += 1
                stats["blocked"] += 1
                stats["errors"].append(
                    {
                        "order_id": order_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

    if stats["network_errors"] or stats["persistence_errors"] or stats["apply_errors"]:
        stats["health"] = "degraded"
    elif stats["conflicted"] or stats["manual_review"] or stats["blocked"]:
        stats["health"] = "attention"
    stats["errors"] = stats["errors"][:100]
    stats["duration_ms"] = int((monotonic() - started) * 1000)
    return stats


__all__ = [
    "MAX_HTTP_CONCURRENCY",
    "MAX_MARKETS_PER_CYCLE",
    "PER_REQUEST_TIMEOUT_SECONDS",
    "ResolutionFetchBatch",
    "SettlementMarketCandidate",
    "run_online_settlement_cycle",
]
