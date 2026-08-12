"""Strict, auditable normalization of online market-resolution evidence.

This module deliberately has no database dependency.  Provider I/O is injected
and bounded; callers persist the resulting immutable observations in a separate
short transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


ResolutionState = Literal["open", "closed_pending", "final", "invalid"]

_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID_RE = re.compile(r"^[0-9]+$")
_RESOLVED_STATUSES = frozenset({"resolved", "settled", "final", "finalized"})
_UNRESOLVED_STATUSES = frozenset(
    {
        "open",
        "pending",
        "proposed",
        "review",
        "disputed",
        "dispute",
        "challenged",
    }
)


@dataclass(frozen=True, slots=True)
class ResolutionLookup:
    """Typed provider lookup key; condition ID is mandatory before settlement."""

    condition_id: str
    provider_market_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResolutionObservation:
    provider: str
    venue: str
    provider_market_id: str | None
    condition_id: str | None
    token_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    outcome_prices: tuple[Decimal, ...]
    closed: bool | None
    accepting_orders: bool | None
    resolution_status: str | None
    state: ResolutionState
    reason: str
    winning_token_id: str | None
    winning_outcome_index: int | None
    winning_outcome: str | None
    provider_resolved_at: datetime | None
    provider_updated_at: datetime | None
    observed_at: datetime
    evidence_hash: str
    evidence_json: dict[str, Any]


class MarketFetcher(Protocol):
    def __call__(self, lookup: ResolutionLookup) -> Awaitable[Mapping[str, Any] | None]: ...


class ResolutionProviderError(RuntimeError):
    """Provider returned no usable payload for a typed lookup."""


@dataclass(frozen=True, slots=True)
class PersistObservationResult:
    observation_id: str
    observation_inserted: bool
    resolution_id: str
    previous_state: str | None
    current_state: str
    fact_changed: bool
    conflict_detected: bool
    fact_version: int


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    parsed = _utc_datetime(value)
    return parsed or datetime.now(timezone.utc)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _json_list(value: Any) -> tuple[list[Any], bool]:
    if isinstance(value, (list, tuple)):
        return list(value), False
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return [], False
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [], True
        if isinstance(parsed, list):
            return parsed, False
        return [], True
    if value is None:
        return [], False
    return [], True


def _strict_bool(value: Any) -> tuple[bool | None, bool]:
    if value is None:
        return None, False
    if isinstance(value, bool):
        return value, False
    if isinstance(value, int) and value in (0, 1):
        return bool(value), False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True, False
        if normalized in {"false", "0"}:
            return False, False
    return None, True


def _condition_id(value: Any) -> str | None:
    raw = _text(value)
    if raw is None or not _CONDITION_ID_RE.fullmatch(raw):
        return None
    return raw.lower()


def _condition_candidates(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], bool]:
    values: list[str] = []
    invalid = False
    for key in ("condition_id", "conditionId"):
        if key not in payload or payload.get(key) is None:
            continue
        normalized = _condition_id(payload.get(key))
        if normalized is None:
            invalid = True
        elif normalized not in values:
            values.append(normalized)
    return tuple(values), invalid


def _provider_market_candidates(payload: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("provider_market_id", "providerMarketId", "id", "market_id", "marketId"):
        if key not in payload or payload.get(key) is None:
            continue
        normalized = _text(payload.get(key))
        if normalized is not None and normalized not in values:
            values.append(normalized)
    return tuple(values)


def _token_id(value: Any) -> str | None:
    raw = _text(value)
    if raw is None or not _TOKEN_ID_RE.fullmatch(raw):
        return None
    return raw


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > 1:
        return None
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_status(value: Any) -> str | None:
    raw = _text(value)
    if raw is None:
        return None
    return re.sub(r"[\s-]+", "_", raw.lower())


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(namespace: str, *parts: str) -> str:
    material = "\x1f".join((namespace, *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _normalized_string_list(value: Any) -> tuple[tuple[str, ...], bool]:
    raw_values, malformed = _json_list(value)
    if malformed:
        return (), True
    normalized: list[str] = []
    for item in raw_values:
        text = _text(item)
        if text is None:
            return (), True
        normalized.append(text)
    return tuple(normalized), False


def _normalized_token_list(value: Any) -> tuple[tuple[str, ...], bool]:
    raw_values, malformed = _json_list(value)
    if malformed:
        return (), True
    normalized: list[str] = []
    for item in raw_values:
        token = _token_id(item)
        if token is None:
            return (), True
        normalized.append(token)
    return tuple(normalized), False


def _normalized_price_list(value: Any) -> tuple[tuple[Decimal, ...], bool]:
    raw_values, malformed = _json_list(value)
    if malformed:
        return (), True
    normalized: list[Decimal] = []
    for item in raw_values:
        price = _decimal(item)
        if price is None:
            return (), True
        normalized.append(price)
    return tuple(normalized), False


def _label_index(outcomes: tuple[str, ...], value: Any) -> int | None:
    label = _text(value)
    if label is None:
        return None
    matches = [
        index
        for index, outcome in enumerate(outcomes)
        if outcome.casefold() == label.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _explicit_winner_index(
    payload: Mapping[str, Any],
    *,
    token_ids: tuple[str, ...],
    outcomes: tuple[str, ...],
) -> tuple[int | None, str | None]:
    token_raw = _first_value(
        payload,
        (
            "winningTokenId",
            "winning_token_id",
            "winnerTokenId",
            "winner_token_id",
        ),
    )
    index_raw = _first_value(
        payload,
        (
            "winningOutcomeIndex",
            "winning_outcome_index",
            "winnerIndex",
            "winner_index",
        ),
    )
    label_raw = _first_value(payload, ("winningOutcome", "winning_outcome", "winner"))

    token_index: int | None = None
    if token_raw is not None:
        token = _token_id(token_raw)
        if token is None or token not in token_ids:
            return None, "winning_token_not_in_market"
        token_index = token_ids.index(token)

    index_value: int | None = None
    if index_raw is not None:
        if isinstance(index_raw, bool):
            return None, "invalid_winning_outcome_index"
        try:
            index_value = int(str(index_raw).strip())
        except (TypeError, ValueError):
            return None, "invalid_winning_outcome_index"
        if index_value < 0 or index_value >= len(token_ids):
            return None, "invalid_winning_outcome_index"

    # Some provider variants place a token or numeric index in ``winner``.
    # Only accept those forms when they map exactly to this market.
    label_index: int | None = None
    if token_index is None and label_raw is not None:
        raw_label = _text(label_raw)
        if raw_label in token_ids:
            label_index = token_ids.index(raw_label)
        else:
            label_index = _label_index(outcomes, label_raw)
            if label_index is None and raw_label is not None and raw_label.isdigit():
                candidate = int(raw_label)
                if 0 <= candidate < len(token_ids):
                    label_index = candidate
            if label_index is None:
                return None, "winning_outcome_not_in_market"

    if token_index is not None:
        if index_value is not None and index_value != token_index:
            return None, "conflicting_winner_identity"
        # A label is display metadata and cannot override an explicit outcome
        # token.  The returned label is derived from the aligned outcome array.
        return token_index, None

    candidates = {value for value in (index_value, label_index) if value is not None}
    if len(candidates) > 1:
        return None, "conflicting_winner_identity"
    return (next(iter(candidates)), None) if candidates else (None, None)


def normalize_gamma_resolution(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> ResolutionObservation:
    """Normalize one Gamma/Polymarket market payload without heuristic payout.

    A final observation requires explicit closure, stopped order acceptance, a
    provider-resolved signal, aligned market identity arrays, and one unique
    winner.  In current Gamma responses the provider-resolved signal is usually
    ``umaResolutionStatus=resolved`` while the legacy ``resolved`` field is null.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    provider = _text(payload.get("provider")) or "gamma"
    venue = (_text(payload.get("venue")) or "polymarket").lower()
    provider_market_values = _provider_market_candidates(payload)
    provider_market_id = provider_market_values[0] if len(provider_market_values) == 1 else None
    condition_values, malformed_condition = _condition_candidates(payload)
    raw_condition_present = any(
        key in payload and payload.get(key) is not None
        for key in ("condition_id", "conditionId")
    )
    condition_id = condition_values[0] if len(condition_values) == 1 else None

    token_ids, malformed_tokens = _normalized_token_list(
        _first_value(payload, ("token_ids", "tokenIds", "clob_token_ids", "clobTokenIds"))
    )
    outcomes, malformed_outcomes = _normalized_string_list(
        _first_value(payload, ("outcomes", "outcome_labels", "outcomeLabels"))
    )
    outcome_prices, malformed_prices = _normalized_price_list(
        _first_value(payload, ("outcome_prices", "outcomePrices"))
    )

    closed, malformed_closed = _strict_bool(_first_value(payload, ("closed", "isClosed", "is_closed")))
    accepting_orders, malformed_accepting = _strict_bool(
        _first_value(payload, ("accepting_orders", "acceptingOrders"))
    )
    resolved_flag, malformed_resolved = _strict_bool(
        _first_value(payload, ("resolved", "isResolved", "is_resolved"))
    )
    resolution_status = _normalized_status(
        _first_value(
            payload,
            (
                "uma_resolution_status",
                "umaResolutionStatus",
                "resolution_status",
                "resolutionStatus",
                "status",
                "marketStatus",
                "market_status",
            ),
        )
    )
    status_resolved = resolution_status in _RESOLVED_STATUSES
    status_unresolved = resolution_status in _UNRESOLVED_STATUSES

    provider_resolved_at = _utc_datetime(
        _first_value(payload, ("resolved_at", "resolvedAt", "resolution_time", "resolutionTime"))
    )
    provider_updated_at = _utc_datetime(_first_value(payload, ("updated_at", "updatedAt")))
    observation_time = _utc_now(observed_at)

    reason: str | None = None
    if not raw_condition_present:
        reason = "missing_condition_id"
    elif malformed_condition:
        reason = "invalid_condition_id"
    elif len(condition_values) > 1:
        reason = "conflicting_condition_ids"
    elif len(provider_market_values) > 1:
        reason = "conflicting_provider_market_ids"
    elif malformed_tokens:
        reason = "invalid_token_ids"
    elif not token_ids:
        reason = "missing_token_ids"
    elif malformed_outcomes:
        reason = "invalid_outcomes"
    elif not outcomes:
        reason = "missing_outcomes"
    elif malformed_prices:
        reason = "invalid_outcome_prices"
    elif not outcome_prices:
        reason = "missing_outcome_prices"
    elif len(token_ids) != len(outcomes) or len(outcomes) != len(outcome_prices):
        reason = "market_arrays_misaligned"
    elif len(set(token_ids)) != len(token_ids):
        reason = "duplicate_token_ids"
    elif len({label.casefold() for label in outcomes}) != len(outcomes):
        reason = "duplicate_outcomes"
    elif malformed_closed or malformed_accepting or malformed_resolved:
        reason = "invalid_market_status_flag"
    elif resolved_flag is False and status_resolved or resolved_flag is True and status_unresolved:
        reason = "conflicting_resolution_status"

    explicit_index: int | None = None
    explicit_error: str | None = None
    price_index: int | None = None
    price_winners = [index for index, price in enumerate(outcome_prices) if price == Decimal(1)]
    exact_price_vector = len(outcome_prices) == 2 and all(
        price in (Decimal(0), Decimal(1)) for price in outcome_prices
    )

    if reason is None:
        explicit_index, explicit_error = _explicit_winner_index(
            payload,
            token_ids=token_ids,
            outcomes=outcomes,
        )
        if explicit_error:
            reason = explicit_error
        elif exact_price_vector and len(price_winners) > 1:
            reason = "multiple_price_winners"
        elif exact_price_vector and len(price_winners) == 1:
            price_index = price_winners[0]
        elif exact_price_vector:
            reason = "missing_price_winner"

    if reason is None and explicit_index is not None and price_index is not None and explicit_index != price_index:
        reason = "conflicting_winner_evidence"

    resolved = resolved_flag is True or status_resolved
    winner_index = explicit_index if explicit_index is not None else price_index
    finality_ready = (
        reason is None
        and closed is True
        and accepting_orders is False
        and resolved
        and winner_index is not None
    )

    if reason is not None:
        state: ResolutionState = "invalid"
        state_reason = reason
    elif finality_ready:
        state = "final"
        state_reason = "final"
    elif closed is not True and accepting_orders is not False:
        state = "open"
        state_reason = "market_open"
    elif not resolved:
        state = "closed_pending"
        state_reason = "provider_not_resolved"
    elif winner_index is None:
        state = "closed_pending"
        state_reason = "winner_not_final"
    else:
        state = "closed_pending"
        state_reason = "closure_not_final"

    final_winner_index = winner_index if state == "final" else None
    final_winning_token = token_ids[final_winner_index] if final_winner_index is not None else None
    final_winning_outcome = outcomes[final_winner_index] if final_winner_index is not None else None

    history_statuses, history_statuses_malformed = _normalized_string_list(
        _first_value(payload, ("uma_resolution_statuses", "umaResolutionStatuses"))
    )
    evidence_json: dict[str, Any] = {
        "schema_version": 1,
        "provider": provider,
        "venue": venue,
        "provider_market_id": provider_market_id,
        "condition_id": condition_id,
        "token_ids": list(token_ids),
        "outcomes": list(outcomes),
        "outcome_prices": [_decimal_text(value) for value in outcome_prices],
        "closed": closed,
        "accepting_orders": accepting_orders,
        "resolved": resolved_flag,
        "resolution_status": resolution_status,
        "resolution_status_history": list(history_statuses) if not history_statuses_malformed else [],
        "active": _strict_bool(payload.get("active"))[0],
        "winning_token_id": final_winning_token,
        "winning_outcome_index": final_winner_index,
        "winning_outcome": final_winning_outcome,
        "candidate_winner_index": winner_index,
        "state": state,
        "reason": state_reason,
        "provider_resolved_at": _datetime_text(provider_resolved_at),
        "provider_updated_at": _datetime_text(provider_updated_at),
        "provider_closed_at": _datetime_text(
            _utc_datetime(_first_value(payload, ("closed_at", "closedAt", "closed_time", "closedTime")))
        ),
    }

    return ResolutionObservation(
        provider=provider,
        venue=venue,
        provider_market_id=provider_market_id,
        condition_id=condition_id,
        token_ids=token_ids,
        outcomes=outcomes,
        outcome_prices=outcome_prices,
        closed=closed,
        accepting_orders=accepting_orders,
        resolution_status=resolution_status,
        state=state,
        reason=state_reason,
        winning_token_id=final_winning_token,
        winning_outcome_index=final_winner_index,
        winning_outcome=final_winning_outcome,
        provider_resolved_at=provider_resolved_at,
        provider_updated_at=provider_updated_at,
        observed_at=observation_time,
        evidence_hash=_canonical_hash(evidence_json),
        evidence_json=evidence_json,
    )


class GammaResolutionProvider:
    """Bounded provider adapter with injected I/O and no owned DB session."""

    def __init__(
        self,
        *,
        fetch_market: MarketFetcher | None = None,
        market_client: Any | None = None,
        request_timeout_seconds: float = 8.0,
        max_concurrency: int = 4,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if fetch_market is not None and market_client is not None:
            raise ValueError("provide fetch_market or market_client, not both")

        if fetch_market is None:
            if market_client is None:
                from services.polymarket import polymarket_client

                market_client = polymarket_client

            async def _client_fetch(lookup: ResolutionLookup) -> Mapping[str, Any] | None:
                return await market_client.get_market_by_condition_id(
                    lookup.condition_id,
                    force_refresh=True,
                )

            fetch_market = _client_fetch

        self._fetch_market = fetch_market
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_concurrency = int(max_concurrency)

    async def fetch_one(self, lookup: ResolutionLookup) -> ResolutionObservation:
        expected_condition_id = _condition_id(lookup.condition_id)
        if expected_condition_id is None:
            raise ValueError("lookup.condition_id must be 0x followed by 64 hex characters")

        async with asyncio.timeout(self.request_timeout_seconds):
            payload = await self._fetch_market(lookup)
        if not isinstance(payload, Mapping):
            raise ResolutionProviderError(f"Gamma returned no market for {lookup.condition_id}")

        normalized_payload = dict(payload)
        response_conditions, response_condition_invalid = _condition_candidates(normalized_payload)
        if response_condition_invalid:
            raise ResolutionProviderError(
                f"Gamma condition mismatch for {lookup.condition_id}: malformed response condition"
            )
        if response_conditions and (
            len(response_conditions) != 1 or response_conditions[0] != expected_condition_id
        ):
            raise ResolutionProviderError(
                f"Gamma condition mismatch for {lookup.condition_id}: {response_conditions!r}"
            )
        if not response_conditions:
            normalized_payload["condition_id"] = expected_condition_id

        expected_provider_market_id = _text(lookup.provider_market_id)
        response_provider_markets = _provider_market_candidates(normalized_payload)
        if expected_provider_market_id and response_provider_markets and (
            len(response_provider_markets) != 1
            or response_provider_markets[0] != expected_provider_market_id
        ):
            raise ResolutionProviderError(
                "Gamma provider market mismatch for "
                f"{lookup.condition_id}: {response_provider_markets!r}"
            )
        if expected_provider_market_id and not response_provider_markets:
            normalized_payload["provider_market_id"] = expected_provider_market_id

        observation = normalize_gamma_resolution(normalized_payload)
        if observation.condition_id != expected_condition_id:
            raise ResolutionProviderError(
                f"Gamma condition mismatch for {lookup.condition_id}: {observation.condition_id!r}"
            )
        return observation

    async def fetch_many(
        self,
        lookups: Sequence[ResolutionLookup],
    ) -> list[ResolutionObservation]:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _bounded(lookup: ResolutionLookup) -> ResolutionObservation:
            async with semaphore:
                return await self.fetch_one(lookup)

        return list(await asyncio.gather(*(_bounded(lookup) for lookup in lookups)))


def _validate_observation_for_persistence(observation: ResolutionObservation) -> None:
    if observation.condition_id is None or _condition_id(observation.condition_id) is None:
        raise ValueError("only observations with a valid condition_id can be persisted")
    if not observation.provider or not observation.venue:
        raise ValueError("provider and venue are required")
    if observation.state not in {"open", "closed_pending", "final", "invalid"}:
        raise ValueError(f"unsupported observation state: {observation.state!r}")
    if len(observation.token_ids) != len(observation.outcomes):
        raise ValueError("observation token/outcome arrays are not aligned")
    if len(observation.outcomes) != len(observation.outcome_prices):
        raise ValueError("observation outcome/price arrays are not aligned")
    if observation.evidence_hash != _canonical_hash(observation.evidence_json):
        raise ValueError("observation evidence_hash does not match evidence_json")
    if observation.evidence_json.get("condition_id") != observation.condition_id:
        raise ValueError("observation condition_id does not match evidence_json")
    if observation.evidence_json.get("state") != observation.state:
        raise ValueError("observation state does not match evidence_json")

    evidence_contract = {
        "provider": observation.provider,
        "venue": observation.venue,
        "provider_market_id": observation.provider_market_id,
        "token_ids": list(observation.token_ids),
        "outcomes": list(observation.outcomes),
        "outcome_prices": [_decimal_text(value) for value in observation.outcome_prices],
        "closed": observation.closed,
        "accepting_orders": observation.accepting_orders,
        "resolution_status": observation.resolution_status,
        "winning_token_id": observation.winning_token_id,
        "winning_outcome_index": observation.winning_outcome_index,
        "winning_outcome": observation.winning_outcome,
        "reason": observation.reason,
        "provider_resolved_at": _datetime_text(observation.provider_resolved_at),
        "provider_updated_at": _datetime_text(observation.provider_updated_at),
    }
    for key, typed_value in evidence_contract.items():
        if observation.evidence_json.get(key) != typed_value:
            raise ValueError(f"observation {key} does not match evidence_json")

    if observation.state == "final":
        index = observation.winning_outcome_index
        if index is None or index < 0 or index >= len(observation.token_ids):
            raise ValueError("final observation requires an aligned winner index")
        if observation.token_ids[index] != observation.winning_token_id:
            raise ValueError("final observation winner token is not aligned")
        if observation.outcomes[index] != observation.winning_outcome:
            raise ValueError("final observation winner outcome is not aligned")
    elif any(
        value is not None
        for value in (
            observation.winning_token_id,
            observation.winning_outcome_index,
            observation.winning_outcome,
        )
    ):
        raise ValueError("non-final observations cannot carry an authoritative winner")


def _observation_values(
    observation: ResolutionObservation,
    *,
    observation_id: str,
) -> dict[str, Any]:
    return {
        "id": observation_id,
        "provider": observation.provider,
        "venue": observation.venue,
        "provider_market_id": observation.provider_market_id,
        "condition_id": observation.condition_id,
        "token_ids_json": list(observation.token_ids),
        "outcomes_json": list(observation.outcomes),
        "outcome_prices_json": [_decimal_text(value) for value in observation.outcome_prices],
        "closed": observation.closed,
        "accepting_orders": observation.accepting_orders,
        "resolution_status": observation.resolution_status,
        "state": observation.state,
        "winning_token_id": observation.winning_token_id,
        "winning_outcome_index": observation.winning_outcome_index,
        "winning_outcome": observation.winning_outcome,
        "provider_resolved_at": observation.provider_resolved_at,
        "provider_updated_at": observation.provider_updated_at,
        "evidence_hash": observation.evidence_hash,
        "evidence_json": dict(observation.evidence_json),
        "observed_at": observation.observed_at,
        "created_at": observation.observed_at,
    }


def _resolution_values(
    observation: ResolutionObservation,
    *,
    resolution_id: str,
) -> dict[str, Any]:
    finalized_at = observation.observed_at if observation.state == "final" else None
    return {
        "id": resolution_id,
        "venue": observation.venue,
        "provider": observation.provider,
        "provider_market_id": observation.provider_market_id,
        "condition_id": observation.condition_id,
        "token_ids_json": list(observation.token_ids),
        "outcomes_json": list(observation.outcomes),
        "outcome_prices_json": [_decimal_text(value) for value in observation.outcome_prices],
        "state": observation.state,
        "winning_token_id": observation.winning_token_id,
        "winning_outcome_index": observation.winning_outcome_index,
        "winning_outcome": observation.winning_outcome,
        "provider_resolved_at": observation.provider_resolved_at,
        "first_observed_at": observation.observed_at,
        "last_observed_at": observation.observed_at,
        "finalized_at": finalized_at,
        "fact_version": 1,
        "evidence_hash": observation.evidence_hash,
        "evidence_json": dict(observation.evidence_json),
        "created_at": observation.observed_at,
        "updated_at": observation.observed_at,
    }


def _current_provider_updated_at(current: Any) -> datetime | None:
    evidence = current.evidence_json if isinstance(current.evidence_json, Mapping) else {}
    return _utc_datetime(evidence.get("provider_updated_at"))


def _observation_is_stale(current: Any, observation: ResolutionObservation) -> bool:
    current_clock = _current_provider_updated_at(current) or _utc_datetime(current.last_observed_at)
    observation_clock = observation.provider_updated_at or observation.observed_at
    if current_clock is None:
        return False
    return observation_clock < current_clock


def _identity_conflicts(current: Any, observation: ResolutionObservation) -> bool:
    current_provider_market = _text(current.provider_market_id)
    incoming_provider_market = _text(observation.provider_market_id)
    if (
        current_provider_market is not None
        and incoming_provider_market is not None
        and current_provider_market != incoming_provider_market
    ):
        return True

    current_tokens = tuple(str(value) for value in (current.token_ids_json or []))
    return bool(current_tokens and observation.token_ids and current_tokens != observation.token_ids)


def _final_winner_conflicts(current: Any, observation: ResolutionObservation) -> bool:
    if observation.state != "final":
        return False
    return (
        _text(current.winning_token_id) != observation.winning_token_id
        or current.winning_outcome_index != observation.winning_outcome_index
    )


def _apply_observation_to_current(current: Any, observation: ResolutionObservation) -> None:
    current.provider = observation.provider
    if observation.provider_market_id is not None:
        current.provider_market_id = observation.provider_market_id
    current.token_ids_json = list(observation.token_ids)
    current.outcomes_json = list(observation.outcomes)
    current.outcome_prices_json = [_decimal_text(value) for value in observation.outcome_prices]
    current.state = observation.state
    current.winning_token_id = observation.winning_token_id
    current.winning_outcome_index = observation.winning_outcome_index
    current.winning_outcome = observation.winning_outcome
    current.provider_resolved_at = observation.provider_resolved_at
    current.evidence_hash = observation.evidence_hash
    current.evidence_json = dict(observation.evidence_json)
    if observation.state == "final" and current.finalized_at is None:
        current.finalized_at = observation.observed_at
    current.fact_version = int(current.fact_version or 0) + 1
    current.updated_at = observation.observed_at


async def persist_observation(
    session: AsyncSession,
    observation: ResolutionObservation,
    *,
    commit: bool = False,
) -> PersistObservationResult:
    """Atomically append evidence and advance/freeze its current fact.

    The caller owns the transaction by default.  ``commit=True`` is provided
    only for top-level worker calls that explicitly delegate that ownership;
    failures are rolled back and always propagated.
    """

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from models.database import OnlineMarketResolution, OnlineMarketResolutionObservation

    _validate_observation_for_persistence(observation)
    condition_id = observation.condition_id
    assert condition_id is not None

    observation_id = _stable_id(
        "online-resolution-observation-v1",
        observation.provider,
        condition_id,
        observation.evidence_hash,
    )
    resolution_id = _stable_id(
        "online-market-resolution-v1",
        observation.venue,
        condition_id,
    )

    try:
        observation_insert = (
            pg_insert(OnlineMarketResolutionObservation)
            .values(**_observation_values(observation, observation_id=observation_id))
            .on_conflict_do_nothing(
                index_elements=[
                    OnlineMarketResolutionObservation.provider,
                    OnlineMarketResolutionObservation.condition_id,
                    OnlineMarketResolutionObservation.evidence_hash,
                ]
            )
            .returning(OnlineMarketResolutionObservation.id)
        )
        inserted_observation_id = await session.scalar(observation_insert)
        observation_inserted = inserted_observation_id is not None

        current_insert = (
            pg_insert(OnlineMarketResolution)
            .values(**_resolution_values(observation, resolution_id=resolution_id))
            .on_conflict_do_nothing(
                index_elements=[
                    OnlineMarketResolution.venue,
                    OnlineMarketResolution.condition_id,
                ]
            )
            .returning(OnlineMarketResolution.id)
        )
        inserted_resolution_id = await session.scalar(current_insert)

        current = (
            await session.execute(
                select(OnlineMarketResolution)
                .where(
                    OnlineMarketResolution.venue == observation.venue,
                    OnlineMarketResolution.condition_id == condition_id,
                )
                .with_for_update()
            )
        ).scalar_one()

        previous_state = None if inserted_resolution_id is not None else str(current.state)
        fact_changed = inserted_resolution_id is not None
        conflict_detected = False

        if inserted_resolution_id is None:
            current.last_observed_at = max(
                _utc_datetime(current.last_observed_at) or observation.observed_at,
                observation.observed_at,
            )

            if current.state == "conflicted":
                pass
            elif _identity_conflicts(current, observation):
                current.state = "conflicted"
                current.fact_version = int(current.fact_version or 0) + 1
                current.updated_at = observation.observed_at
                fact_changed = True
                conflict_detected = True
            elif current.state == "final":
                if _final_winner_conflicts(current, observation):
                    current.state = "conflicted"
                    current.fact_version = int(current.fact_version or 0) + 1
                    current.updated_at = observation.observed_at
                    fact_changed = True
                    conflict_detected = True
                # A matching final remains frozen.  New evidence is retained in
                # the append-only table but cannot mutate an already-issued fact.
            elif current.state == "invalid" or observation.state == "final":
                _apply_observation_to_current(current, observation)
                fact_changed = True
            elif current.state == "open" and observation.state == "closed_pending":
                if not _observation_is_stale(current, observation):
                    _apply_observation_to_current(current, observation)
                    fact_changed = True
            elif (
                current.state == observation.state
                and not _observation_is_stale(current, observation)
                and current.evidence_hash != observation.evidence_hash
            ):
                _apply_observation_to_current(current, observation)
                fact_changed = True
            # closed_pending -> open and every other lower-rank transition are
            # intentionally observation-only; current truth never regresses.

        await session.flush()
        result = PersistObservationResult(
            observation_id=observation_id,
            observation_inserted=observation_inserted,
            resolution_id=str(current.id),
            previous_state=previous_state,
            current_state=str(current.state),
            fact_changed=fact_changed,
            conflict_detected=conflict_detected,
            fact_version=int(current.fact_version),
        )
        if commit:
            await session.commit()
        return result
    except BaseException:
        if commit:
            await session.rollback()
        raise


__all__ = [
    "GammaResolutionProvider",
    "PersistObservationResult",
    "ResolutionLookup",
    "ResolutionObservation",
    "ResolutionProviderError",
    "normalize_gamma_resolution",
    "persist_observation",
]
