"""Typed, network-free market identity normalization.

``TraderOrder.market_id`` is a provider market identifier in the orchestrator
and must not be guessed to be a condition or outcome token.  This module only
uses explicit payload evidence and returns a status/reason instead of filling
missing fields heuristically.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

IdentityStatus = Literal["complete", "legacy_inferred", "ambiguous", "invalid"]

_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_UINT_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class MarketIdentity:
    venue: str
    provider_market_id: str | None
    condition_id: str | None
    token_id: str | None
    outcome_index: int | None
    status: IdentityStatus
    reason: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
            if isinstance(parsed, list):
                return parsed
    return []


def _normalized_condition_id(value: Any) -> str | None:
    raw = _text(value)
    if not raw or not _CONDITION_ID_RE.fullmatch(raw):
        return None
    return raw.lower()


def _normalized_uint(value: Any) -> str | None:
    raw = _text(value)
    if not raw or not _UINT_RE.fullmatch(raw):
        return None
    return raw


def _payload_sources(
    payload: Mapping[str, Any],
    signal_payload: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []

    def _append_tree(root: Mapping[str, Any]) -> None:
        if not root:
            return
        sources.append(root)
        for key in ("live_market", "market"):
            nested = _mapping(root.get(key))
            if nested:
                sources.append(nested)
        markets = _sequence(root.get("markets"))
        if markets:
            first = _mapping(markets[0])
            if first:
                sources.append(first)

    _append_tree(payload)
    _append_tree(signal_payload)
    return sources


def _distinct(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _read_token_lists(sources: list[Mapping[str, Any]]) -> tuple[list[str], bool, bool]:
    lists: list[tuple[str, ...]] = []
    invalid = False
    for source in sources:
        for key in ("token_ids", "tokenIds", "clob_token_ids", "clobTokenIds"):
            if key not in source or source.get(key) in (None, ""):
                continue
            values = _sequence(source.get(key))
            if not values:
                invalid = True
                continue
            normalized: list[str] = []
            for value in values:
                token = _normalized_uint(value)
                if token is None:
                    invalid = True
                    normalized = []
                    break
                normalized.append(token)
            if normalized:
                lists.append(tuple(normalized))

        yes_token = _normalized_uint(source.get("yes_token_id") or source.get("yesTokenId"))
        no_token = _normalized_uint(source.get("no_token_id") or source.get("noTokenId"))
        if yes_token and no_token:
            lists.append((yes_token, no_token))

    distinct_lists = list(dict.fromkeys(lists))
    if len(distinct_lists) > 1:
        return [], True, invalid
    return list(distinct_lists[0]) if distinct_lists else [], False, invalid


def _direction_index(direction: Any) -> int | None:
    key = _text(direction).lower().replace("-", "_").replace(" ", "_")
    if key in {"yes", "buy_yes", "sell_yes"}:
        return 0
    if key in {"no", "buy_no", "sell_no"}:
        return 1
    return None


def resolve_market_identity(
    *,
    market_id: Any,
    direction: Any,
    payload: Mapping[str, Any] | None,
    signal_payload: Mapping[str, Any] | None = None,
    venue: str = "polymarket",
    legacy: bool = False,
) -> MarketIdentity:
    """Return a typed identity using explicit payload evidence only."""

    payload_map = _mapping(payload)
    signal_map = _mapping(signal_payload)
    sources = _payload_sources(payload_map, signal_map)
    normalized_venue = _text(venue).lower() or "polymarket"

    condition_candidates: list[str] = []
    invalid_condition = False
    provider_market_candidates: list[str] = []

    root_market_id = _text(market_id)
    if root_market_id:
        root_condition = _normalized_condition_id(root_market_id)
        root_provider = _normalized_uint(root_market_id)
        if root_condition:
            condition_candidates.append(root_condition)
        elif root_provider:
            provider_market_candidates.append(root_provider)
        else:
            # Provider market identifiers are opaque at this boundary. Gamma
            # uses numeric ids today, while synthetic/test and future provider
            # adapters may use strings. The important rule is that this value
            # is never reinterpreted as an outcome token.
            provider_market_candidates.append(root_market_id)

    for source in sources:
        for key in ("condition_id", "conditionId"):
            if key not in source or source.get(key) in (None, ""):
                continue
            condition = _normalized_condition_id(source.get(key))
            if condition is None:
                invalid_condition = True
            else:
                condition_candidates.append(condition)

        for key in ("provider_market_id", "providerMarketId"):
            if key not in source or source.get(key) in (None, ""):
                continue
            provider_market = _text(source.get(key))
            if provider_market:
                provider_market_candidates.append(provider_market)

        for key in ("id", "market_id", "marketId"):
            if key not in source or source.get(key) in (None, ""):
                continue
            value = source.get(key)
            condition = _normalized_condition_id(value)
            provider_market = _normalized_uint(value)
            if condition:
                condition_candidates.append(condition)
            elif provider_market:
                provider_market_candidates.append(provider_market)
            else:
                opaque_provider_market = _text(value)
                if opaque_provider_market:
                    provider_market_candidates.append(opaque_provider_market)

    condition_ids = _distinct(condition_candidates)
    provider_market_ids = _distinct(provider_market_candidates)
    provider_market_id = provider_market_ids[0] if len(provider_market_ids) == 1 else None

    if invalid_condition:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            None,
            None,
            None,
            "invalid",
            "invalid_condition_id",
        )
    if len(condition_ids) > 1:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            None,
            None,
            None,
            "ambiguous",
            "conflicting_condition_ids",
        )
    if len(provider_market_ids) > 1:
        return MarketIdentity(
            normalized_venue,
            None,
            condition_ids[0] if condition_ids else None,
            None,
            None,
            "ambiguous",
            "conflicting_provider_market_ids",
        )

    condition_id = condition_ids[0] if condition_ids else None

    token_ids, conflicting_token_lists, invalid_token_list = _read_token_lists(sources)
    if invalid_token_list:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            None,
            "invalid",
            "invalid_token_id",
        )
    if conflicting_token_lists:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            None,
            "ambiguous",
            "conflicting_token_lists",
        )

    outcome_lists: list[tuple[str, ...]] = []
    for source in sources:
        for key in ("outcomes", "outcome_labels", "outcomeLabels"):
            if key not in source or source.get(key) in (None, ""):
                continue
            values = tuple(_text(value) for value in _sequence(source.get(key)))
            if values:
                outcome_lists.append(values)
    distinct_outcomes = list(dict.fromkeys(outcome_lists))
    if len(distinct_outcomes) > 1:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            None,
            "ambiguous",
            "conflicting_outcome_lists",
        )
    if distinct_outcomes and token_ids and len(distinct_outcomes[0]) != len(token_ids):
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            None,
            "invalid",
            "token_outcome_length_mismatch",
        )

    selected_candidates: list[str] = []
    invalid_selected_token = False
    for source in sources:
        token_sources = [source]
        leg = _mapping(source.get("leg"))
        if leg:
            token_sources.append(leg)
        for token_source in token_sources:
            for key in ("token_id", "tokenId", "selected_token_id", "selectedTokenId", "asset"):
                if key not in token_source or token_source.get(key) in (None, ""):
                    continue
                token = _normalized_uint(token_source.get(key))
                if token is None:
                    invalid_selected_token = True
                else:
                    selected_candidates.append(token)

    direction_index = _direction_index(direction)
    if not selected_candidates and direction_index is not None:
        direction_aliases = (
            ("yes_token_id", "yesTokenId")
            if direction_index == 0
            else ("no_token_id", "noTokenId")
        )
        for source in sources:
            for key in direction_aliases:
                token = _normalized_uint(source.get(key))
                if token:
                    selected_candidates.append(token)

    selected_tokens = _distinct(selected_candidates)
    if invalid_selected_token:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            None,
            "invalid",
            "invalid_token_id",
        )
    if len(selected_tokens) > 1:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            None,
            "ambiguous",
            "conflicting_selected_tokens",
        )

    selected_token = selected_tokens[0] if selected_tokens else None

    explicit_indexes: list[int] = []
    invalid_outcome_index = False
    for source in sources:
        for key in ("outcome_index", "outcomeIndex", "selected_outcome_index", "selectedOutcomeIndex"):
            if key not in source or source.get(key) in (None, ""):
                continue
            try:
                index = int(source.get(key))
            except (TypeError, ValueError):
                invalid_outcome_index = True
                continue
            if index < 0:
                invalid_outcome_index = True
            else:
                explicit_indexes.append(index)
    if invalid_outcome_index:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            selected_token,
            None,
            "invalid",
            "invalid_outcome_index",
        )

    index_candidates = list(dict.fromkeys(explicit_indexes))
    if selected_token and token_ids:
        if selected_token not in token_ids:
            return MarketIdentity(
                normalized_venue,
                provider_market_id,
                condition_id,
                selected_token,
                None,
                "invalid",
                "selected_token_not_in_market",
            )
        index_candidates.append(token_ids.index(selected_token))
    if direction_index is not None:
        if token_ids and direction_index >= len(token_ids):
            return MarketIdentity(
                normalized_venue,
                provider_market_id,
                condition_id,
                selected_token,
                None,
                "invalid",
                "direction_outcome_out_of_range",
            )
        index_candidates.append(direction_index)
        if selected_token is None and token_ids:
            selected_token = token_ids[direction_index]

    unique_indexes = list(dict.fromkeys(index_candidates))
    if len(unique_indexes) > 1:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            selected_token,
            None,
            "ambiguous",
            "conflicting_outcome_indexes",
        )
    outcome_index = unique_indexes[0] if unique_indexes else None
    if token_ids and outcome_index is not None and outcome_index >= len(token_ids):
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            selected_token,
            None,
            "invalid",
            "outcome_index_out_of_range",
        )

    if condition_id is None and provider_market_id is None:
        return MarketIdentity(
            normalized_venue,
            None,
            None,
            selected_token,
            outcome_index,
            "ambiguous",
            "missing_market_identifier",
        )
    if selected_token is None:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            None,
            outcome_index,
            "ambiguous",
            "missing_selected_token",
        )
    if outcome_index is None:
        return MarketIdentity(
            normalized_venue,
            provider_market_id,
            condition_id,
            selected_token,
            None,
            "ambiguous",
            "missing_outcome_index",
        )

    return MarketIdentity(
        normalized_venue,
        provider_market_id,
        condition_id,
        selected_token,
        outcome_index,
        "legacy_inferred" if legacy else "complete",
        "legacy_identity_inferred" if legacy else "identity_complete",
    )


def identity_from_order(order: Any) -> MarketIdentity:
    """Read persisted typed identity, falling back to legacy payload evidence.

    A stored complete/legacy identity is authoritative. Rows created before the
    typed columns existed are normalized from their payload and are explicitly
    labeled ``legacy_inferred`` so callers cannot mistake inference for a new
    order-time guarantee.
    """

    stored_status = _text(getattr(order, "identity_status", None)).lower()
    stored_venue = _text(getattr(order, "venue", None)).lower() or "polymarket"
    stored_provider_market_id = _text(getattr(order, "provider_market_id", None)) or None
    stored_condition_id = _normalized_condition_id(getattr(order, "condition_id", None))
    stored_token_id = _normalized_uint(getattr(order, "token_id", None))
    raw_outcome_index = getattr(order, "outcome_index", None)
    try:
        stored_outcome_index = (
            int(raw_outcome_index) if raw_outcome_index is not None else None
        )
    except (TypeError, ValueError):
        stored_outcome_index = None

    if stored_status in {"complete", "legacy_inferred"}:
        if (
            stored_condition_id is None
            or stored_token_id is None
            or stored_outcome_index is None
            or stored_outcome_index < 0
        ):
            return MarketIdentity(
                stored_venue,
                stored_provider_market_id,
                stored_condition_id,
                stored_token_id,
                stored_outcome_index,
                "invalid",
                "invalid_stored_identity",
            )
        return MarketIdentity(
            stored_venue,
            stored_provider_market_id,
            stored_condition_id,
            stored_token_id,
            stored_outcome_index,
            stored_status,  # type: ignore[arg-type]
            "identity_complete"
            if stored_status == "complete"
            else "legacy_identity_inferred",
        )

    if stored_status in {"ambiguous", "invalid"}:
        return MarketIdentity(
            stored_venue,
            stored_provider_market_id,
            stored_condition_id,
            stored_token_id,
            stored_outcome_index,
            stored_status,  # type: ignore[arg-type]
            f"stored_identity_{stored_status}",
        )

    return resolve_market_identity(
        market_id=getattr(order, "market_id", None),
        direction=getattr(order, "direction", None),
        payload=_mapping(getattr(order, "payload_json", None)),
        venue=stored_venue,
        legacy=True,
    )
