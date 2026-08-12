import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.online_resolution import (
    GammaResolutionProvider,
    ResolutionLookup,
    ResolutionProviderError,
    normalize_gamma_resolution,
)

CONDITION_ID = "0x04e69db3409542f7c01e2ce24a62c5797f01ae5cea38b63bd8d2e9590525b960"
YES_TOKEN = "58561482285516050791004856867678545196648843282217778362959518771104904906823"
NO_TOKEN = "44141566124781150304063414007185965102308974241356923690196981164402456095848"


def _gamma_payload(**overrides):
    payload = {
        "id": "3486334",
        "conditionId": CONDITION_ID,
        "question": "Ethereum above 1,820 on August 10, 11AM ET?",
        "closed": True,
        "active": True,
        "acceptingOrders": False,
        # Current Gamma responses use this field as the authoritative
        # resolution signal; the legacy ``resolved`` field is commonly null.
        "resolved": None,
        "umaResolutionStatus": "resolved",
        # This aggregate/history field can still contain ``proposed`` even
        # when the market-level status above is resolved.
        "umaResolutionStatuses": '["proposed"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": f'["{YES_TOKEN}", "{NO_TOKEN}"]',
        "updatedAt": "2026-08-10T15:11:58.578991Z",
        "closedTime": "2026-08-10 15:10:58+00",
    }
    payload.update(overrides)
    return payload


def test_normalizes_current_gamma_final_shape_without_guessing_labels():
    observation = normalize_gamma_resolution(_gamma_payload())

    assert observation.provider == "gamma"
    assert observation.venue == "polymarket"
    assert observation.provider_market_id == "3486334"
    assert observation.condition_id == CONDITION_ID
    assert observation.token_ids == (YES_TOKEN, NO_TOKEN)
    assert observation.outcomes == ("Yes", "No")
    assert observation.outcome_prices == (Decimal(1), Decimal(0))
    assert observation.closed is True
    assert observation.accepting_orders is False
    assert observation.resolution_status == "resolved"
    assert observation.state == "final"
    assert observation.winning_outcome_index == 0
    assert observation.winning_token_id == YES_TOKEN
    assert observation.winning_outcome == "Yes"
    assert observation.provider_updated_at == datetime(
        2026, 8, 10, 15, 11, 58, 578991, tzinfo=timezone.utc
    )
    assert observation.provider_resolved_at is None
    assert len(observation.evidence_hash) == 64


def test_explicit_winning_token_is_authoritative_over_conflicting_display_label():
    observation = normalize_gamma_resolution(
        _gamma_payload(
            outcomePrices='["0.55", "0.45"]',
            winningTokenId=NO_TOKEN,
            winningOutcome="Yes",
        )
    )

    assert observation.state == "final"
    assert observation.winning_outcome_index == 1
    assert observation.winning_token_id == NO_TOKEN
    assert observation.winning_outcome == "No"


def test_explicit_winning_token_is_authoritative_over_unknown_display_label():
    observation = normalize_gamma_resolution(
        _gamma_payload(
            outcomePrices='["0.55", "0.45"]',
            winningTokenId=NO_TOKEN,
            winningOutcome="stale-provider-label",
        )
    )

    assert observation.state == "final"
    assert observation.winning_outcome_index == 1
    assert observation.winning_token_id == NO_TOKEN
    assert observation.winning_outcome == "No"


def test_explicit_unique_outcome_label_can_identify_winner_without_price_guess():
    observation = normalize_gamma_resolution(
        _gamma_payload(
            outcomes='["Home Team", "Away Team"]',
            outcomePrices='["0.42", "0.58"]',
            winningOutcome="Away Team",
        )
    )

    assert observation.state == "final"
    assert observation.winning_outcome_index == 1
    assert observation.winning_token_id == NO_TOKEN
    assert observation.winning_outcome == "Away Team"


@pytest.mark.parametrize(
    ("overrides", "expected_state", "expected_reason"),
    [
        (
            {"outcomePrices": '["0.98", "0.02"]'},
            "closed_pending",
            "winner_not_final",
        ),
        (
            {"umaResolutionStatus": "proposed"},
            "closed_pending",
            "provider_not_resolved",
        ),
        (
            {"closed": False, "acceptingOrders": True, "umaResolutionStatus": None},
            "open",
            "market_open",
        ),
        (
            {"clobTokenIds": f'["{YES_TOKEN}"]'},
            "invalid",
            "market_arrays_misaligned",
        ),
        (
            {"clobTokenIds": None},
            "invalid",
            "missing_token_ids",
        ),
        (
            {"outcomePrices": '["1", "1"]'},
            "invalid",
            "multiple_price_winners",
        ),
        (
            {"conditionId": "3486334"},
            "invalid",
            "invalid_condition_id",
        ),
        (
            {
                "outcomes": '["One", "Two", "Three"]',
                "clobTokenIds": f'["{YES_TOKEN}", "{NO_TOKEN}", "3"]',
                "outcomePrices": '["1", "0", "0"]',
            },
            "closed_pending",
            "winner_not_final",
        ),
        (
            {
                "condition_id": "0x" + "1" * 64,
            },
            "invalid",
            "conflicting_condition_ids",
        ),
    ],
)
def test_strict_finality_rejects_incomplete_or_ambiguous_evidence(
    overrides,
    expected_state,
    expected_reason,
):
    observation = normalize_gamma_resolution(_gamma_payload(**overrides))

    assert observation.state == expected_state
    assert observation.reason == expected_reason
    assert observation.winning_token_id is None
    assert observation.winning_outcome_index is None


def test_canonical_hash_ignores_mapping_order_and_observation_time():
    first_payload = _gamma_payload()
    reordered_payload = dict(reversed(list(first_payload.items())))

    first = normalize_gamma_resolution(
        first_payload,
        observed_at=datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc),
    )
    second = normalize_gamma_resolution(
        reordered_payload,
        observed_at=datetime(2026, 8, 10, 16, 5, tzinfo=timezone.utc),
    )

    assert first.evidence_json == second.evidence_json
    assert first.evidence_hash == second.evidence_hash


def test_canonical_hash_changes_when_economic_evidence_changes():
    first = normalize_gamma_resolution(_gamma_payload())
    second = normalize_gamma_resolution(_gamma_payload(outcomePrices='["0", "1"]'))

    assert first.evidence_hash != second.evidence_hash
    assert first.winning_token_id != second.winning_token_id


@pytest.mark.asyncio
async def test_provider_is_injected_bounded_and_has_no_database_dependency():
    active = 0
    peak = 0
    seen: list[str] = []

    async def fetch_market(lookup: ResolutionLookup):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append(lookup.condition_id)
        await asyncio.sleep(0.01)
        active -= 1
        suffix = int(lookup.condition_id[-1], 16)
        return _gamma_payload(
            id=str(3_486_334 + suffix),
            conditionId=lookup.condition_id,
        )

    lookups = [
        ResolutionLookup(condition_id=f"0x{'0' * 63}{value:x}")
        for value in range(1, 7)
    ]
    provider = GammaResolutionProvider(
        fetch_market=fetch_market,
        request_timeout_seconds=0.5,
        max_concurrency=2,
    )

    observations = await provider.fetch_many(lookups)

    assert len(observations) == len(lookups)
    assert set(seen) == {lookup.condition_id for lookup in lookups}
    assert peak == 2
    assert all(item.state == "final" for item in observations)


@pytest.mark.asyncio
async def test_provider_enforces_per_request_timeout():
    async def blocked_fetch(_lookup: ResolutionLookup):
        await asyncio.sleep(1)
        return _gamma_payload()

    provider = GammaResolutionProvider(
        fetch_market=blocked_fetch,
        request_timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError):
        await provider.fetch_one(ResolutionLookup(condition_id=CONDITION_ID))


@pytest.mark.asyncio
async def test_provider_rejects_response_for_a_different_condition():
    other_condition = "0x" + "2" * 64

    async def mismatched_fetch(_lookup: ResolutionLookup):
        return _gamma_payload(conditionId=other_condition)

    provider = GammaResolutionProvider(fetch_market=mismatched_fetch)

    with pytest.raises(ResolutionProviderError, match="condition mismatch"):
        await provider.fetch_one(ResolutionLookup(condition_id=CONDITION_ID))


@pytest.mark.asyncio
async def test_provider_rejects_response_for_a_different_provider_market():
    async def mismatched_fetch(_lookup: ResolutionLookup):
        return _gamma_payload(id="9999999")

    provider = GammaResolutionProvider(fetch_market=mismatched_fetch)

    with pytest.raises(ResolutionProviderError, match="provider market mismatch"):
        await provider.fetch_one(
            ResolutionLookup(
                condition_id=CONDITION_ID,
                provider_market_id="3486334",
            )
        )
