from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.market_identity import MarketIdentity, identity_from_order, resolve_market_identity
from services.trader_orchestrator import order_manager
from services.trader_orchestrator_state import build_trader_order_row

CONDITION_A = "0x" + ("a" * 64)
CONDITION_B = "0x" + ("b" * 64)
YES_TOKEN = "11111111111111111111111111111111111111111111111111111111111111111"
NO_TOKEN = "22222222222222222222222222222222222222222222222222222222222222222"
THIRD_TOKEN = "33333333333333333333333333333333333333333333333333333333333333333"


def test_identity_is_immutable_and_keeps_gamma_numeric_id_out_of_token_field() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy",
        payload={
            "condition_id": CONDITION_A,
            "selected_token_id": NO_TOKEN,
            "token_ids": [YES_TOKEN, NO_TOKEN],
            "outcomes": ["Yes", "No"],
        },
    )

    assert identity == MarketIdentity(
        venue="polymarket",
        provider_market_id="3412941",
        condition_id=CONDITION_A,
        token_id=NO_TOKEN,
        outcome_index=1,
        status="complete",
        reason="identity_complete",
    )
    assert identity.token_id != identity.provider_market_id
    with pytest.raises(FrozenInstanceError):
        identity.token_id = YES_TOKEN  # type: ignore[misc]


def test_identity_accepts_existing_live_market_camel_case_aliases() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy_yes",
        payload={
            "live_market": {
                "conditionId": CONDITION_A.upper().replace("0X", "0x"),
                "clobTokenIds": [YES_TOKEN, NO_TOKEN],
                "outcomes": ["Yes", "No"],
                "selected_token_id": YES_TOKEN,
            }
        },
    )

    assert identity.condition_id == CONDITION_A
    assert identity.token_id == YES_TOKEN
    assert identity.outcome_index == 0
    assert identity.status == "complete"


def test_identity_marks_conflicting_condition_ids_ambiguous_instead_of_choosing_one() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy_yes",
        payload={
            "condition_id": CONDITION_A,
            "live_market": {
                "condition_id": CONDITION_B,
                "selected_token_id": YES_TOKEN,
                "token_ids": [YES_TOKEN, NO_TOKEN],
            },
        },
    )

    assert identity.status == "ambiguous"
    assert identity.reason == "conflicting_condition_ids"
    assert identity.condition_id is None


def test_identity_rejects_malformed_explicit_condition_id() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy_yes",
        payload={
            "condition_id": "0x1234",
            "selected_token_id": YES_TOKEN,
            "token_ids": [YES_TOKEN, NO_TOKEN],
        },
    )

    assert identity.status == "invalid"
    assert identity.reason == "invalid_condition_id"


def test_bare_buy_derives_outcome_index_only_from_selected_token_membership() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy",
        payload={
            "condition_id": CONDITION_A,
            "token_id": NO_TOKEN,
            "clob_token_ids": [YES_TOKEN, NO_TOKEN],
            "outcomes": ["Yes", "No"],
        },
    )

    assert identity.status == "complete"
    assert identity.outcome_index == 1


def test_multi_outcome_market_without_selected_token_is_ambiguous() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy",
        payload={
            "condition_id": CONDITION_A,
            "clob_token_ids": [YES_TOKEN, NO_TOKEN, THIRD_TOKEN],
            "outcomes": ["A", "B", "C"],
        },
    )

    assert identity.status == "ambiguous"
    assert identity.reason == "missing_selected_token"
    assert identity.outcome_index is None


def test_numeric_market_id_without_payload_is_provider_id_not_token_guess() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy",
        payload={},
    )

    assert identity.provider_market_id == "3412941"
    assert identity.token_id is None
    assert identity.condition_id is None
    assert identity.status == "ambiguous"
    assert identity.reason == "missing_selected_token"


def test_complete_legacy_payload_is_labeled_legacy_inferred() -> None:
    identity = resolve_market_identity(
        market_id="3412941",
        direction="buy_no",
        payload={
            "market": {
                "conditionId": CONDITION_A,
                "clobTokenIds": [YES_TOKEN, NO_TOKEN],
                "outcomes": ["Yes", "No"],
            },
            "no_token_id": NO_TOKEN,
        },
        legacy=True,
    )

    assert identity.status == "legacy_inferred"
    assert identity.token_id == NO_TOKEN
    assert identity.outcome_index == 1
    assert identity.reason == "legacy_identity_inferred"


def test_order_builder_persists_complete_typed_identity_from_signal_payload() -> None:
    signal = SimpleNamespace(
        id="signal-identity-1",
        source="weather",
        market_id="3412941",
        market_question="Will the test resolve?",
        direction="buy_no",
        entry_price=0.42,
        edge_percent=8.0,
        confidence=0.8,
        payload_json={
            "condition_id": CONDITION_A,
            "selected_token_id": NO_TOKEN,
            "clob_token_ids": [YES_TOKEN, NO_TOKEN],
            "outcomes": ["Yes", "No"],
        },
    )

    row = build_trader_order_row(
        trader_id="trader-1",
        signal=signal,
        decision_id=None,
        strategy_key="weather_distribution",
        strategy_version=1,
        mode="shadow",
        status="open",
        notional_usd=10.0,
        effective_price=0.42,
        reason="test",
        payload={},
    )

    assert row.venue == "polymarket"
    assert row.provider_market_id == "3412941"
    assert row.condition_id == CONDITION_A
    assert row.token_id == NO_TOKEN
    assert row.outcome_index == 1
    assert row.identity_status == "complete"


def test_identity_from_legacy_order_uses_payload_without_guessing_numeric_market_id() -> None:
    order = SimpleNamespace(
        market_id="3412941",
        direction="buy_yes",
        venue=None,
        provider_market_id=None,
        condition_id=None,
        token_id=None,
        outcome_index=None,
        identity_status=None,
        payload_json={
            "live_market": {
                "condition_id": CONDITION_A,
                "selected_token_id": YES_TOKEN,
                "token_ids": [YES_TOKEN, NO_TOKEN],
                "outcomes": ["Yes", "No"],
            }
        },
    )

    identity = identity_from_order(order)

    assert identity.status == "legacy_inferred"
    assert identity.provider_market_id == "3412941"
    assert identity.condition_id == CONDITION_A
    assert identity.token_id == YES_TOKEN
    assert identity.outcome_index == 0


@pytest.mark.asyncio
async def test_execution_boundary_rejects_ambiguous_identity_before_venue_submit(
    monkeypatch,
) -> None:
    execution_mock = AsyncMock(
        side_effect=AssertionError("ambiguous identity must be rejected before venue submit")
    )
    monkeypatch.setattr(order_manager, "execute_live_order", execution_mock)
    signal = SimpleNamespace(
        id="signal-ambiguous-identity",
        market_id="3412941",
        direction="buy",
        entry_price=0.40,
        market_question="Ambiguous outcome",
        payload_json={"selected_token_id": YES_TOKEN},
    )

    result = await order_manager.submit_execution_leg(
        mode="live",
        signal=signal,
        leg={
            "leg_id": "leg-ambiguous-identity",
            "market_id": signal.market_id,
            "market_question": signal.market_question,
            "side": "buy",
            "limit_price": signal.entry_price,
        },
        notional_usd=10.0,
    )

    assert result.status == "failed"
    assert result.payload["submission"] == "rejected"
    assert result.payload["reason"] == "market_identity_missing_outcome_index"
    execution_mock.assert_not_awaited()
