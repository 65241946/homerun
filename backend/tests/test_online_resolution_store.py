from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from models.database import (
    Base,
    OnlineMarketResolution,
    OnlineMarketResolutionObservation,
)
from services.online_resolution import normalize_gamma_resolution, persist_observation
from tests.postgres_test_db import build_postgres_session_factory

CONDITION_ID = "0x04e69db3409542f7c01e2ce24a62c5797f01ae5cea38b63bd8d2e9590525b960"
YES_TOKEN = "58561482285516050791004856867678545196648843282217778362959518771104904906823"
NO_TOKEN = "44141566124781150304063414007185965102308974241356923690196981164402456095848"
BASE_TIME = datetime(2026, 8, 10, 15, 11, 58, tzinfo=timezone.utc)


def _observation(
    *,
    prices: str = '["1", "0"]',
    closed: bool = True,
    accepting_orders: bool = False,
    resolution_status: str | None = "resolved",
    updated_at: datetime = BASE_TIME,
    observed_at: datetime | None = None,
):
    return normalize_gamma_resolution(
        {
            "id": "3486334",
            "conditionId": CONDITION_ID,
            "closed": closed,
            "acceptingOrders": accepting_orders,
            "umaResolutionStatus": resolution_status,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": prices,
            "clobTokenIds": f'["{YES_TOKEN}", "{NO_TOKEN}"]',
            "updatedAt": updated_at.isoformat(),
        },
        observed_at=observed_at or updated_at,
    )


@pytest.mark.asyncio
async def test_store_rejects_typed_fields_that_do_not_match_hashed_evidence():
    tampered = replace(_observation(), provider_market_id="9999999")

    with pytest.raises(ValueError, match="provider_market_id does not match evidence_json"):
        await persist_observation(None, tampered)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_store_deduplicates_observations_and_advances_pending_to_final():
    engine, session_factory = await build_postgres_session_factory(Base, "online_resolution_progression")
    try:
        pending = _observation(prices='["0.98", "0.02"]')
        final = _observation(updated_at=BASE_TIME + timedelta(minutes=1))

        async with session_factory() as session:
            pending_result = await persist_observation(session, pending)
            assert pending_result.observation_inserted is True
            assert pending_result.current_state == "closed_pending"
            assert pending_result.fact_version == 1
            await session.commit()

        async with session_factory() as session:
            final_result = await persist_observation(session, final)
            assert final_result.observation_inserted is True
            assert final_result.previous_state == "closed_pending"
            assert final_result.current_state == "final"
            assert final_result.fact_changed is True
            assert final_result.fact_version == 2
            await session.commit()

        async with session_factory() as session:
            replay_result = await persist_observation(session, final)
            assert replay_result.observation_inserted is False
            assert replay_result.current_state == "final"
            assert replay_result.fact_changed is False
            assert replay_result.fact_version == 2
            await session.commit()

        async with session_factory() as session:
            observations = await session.scalar(
                select(func.count()).select_from(OnlineMarketResolutionObservation)
            )
            facts = await session.scalar(select(func.count()).select_from(OnlineMarketResolution))
            current = (
                await session.execute(
                    select(OnlineMarketResolution).where(
                        OnlineMarketResolution.condition_id == CONDITION_ID
                    )
                )
            ).scalar_one()

        assert observations == 2
        assert facts == 1
        assert current.state == "final"
        assert current.winning_token_id == YES_TOKEN
        assert current.fact_version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_freezes_final_winner_and_records_conflicting_observation():
    engine, session_factory = await build_postgres_session_factory(Base, "online_resolution_conflict")
    try:
        first_final = _observation()
        same_winner_new_evidence = _observation(updated_at=BASE_TIME + timedelta(minutes=1))
        conflicting_final = _observation(
            prices='["0", "1"]',
            updated_at=BASE_TIME + timedelta(minutes=2),
        )

        async with session_factory() as session:
            await persist_observation(session, first_final)
            await session.commit()

        async with session_factory() as session:
            same_winner = await persist_observation(session, same_winner_new_evidence)
            assert same_winner.observation_inserted is True
            assert same_winner.current_state == "final"
            assert same_winner.fact_changed is False
            assert same_winner.fact_version == 1
            await session.commit()

        async with session_factory() as session:
            conflict = await persist_observation(session, conflicting_final)
            assert conflict.observation_inserted is True
            assert conflict.previous_state == "final"
            assert conflict.current_state == "conflicted"
            assert conflict.conflict_detected is True
            assert conflict.fact_version == 2
            await session.commit()

        async with session_factory() as session:
            current = (
                await session.execute(select(OnlineMarketResolution))
            ).scalar_one()
            observation_rows = (
                await session.execute(
                    select(OnlineMarketResolutionObservation).order_by(
                        OnlineMarketResolutionObservation.observed_at
                    )
                )
            ).scalars().all()

        assert current.state == "conflicted"
        assert current.winning_token_id == YES_TOKEN
        assert current.winning_outcome_index == 0
        assert current.evidence_hash == first_final.evidence_hash
        assert len(observation_rows) == 3
        assert observation_rows[-1].winning_token_id == NO_TOKEN
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_does_not_regress_current_fact_from_stale_observation():
    engine, session_factory = await build_postgres_session_factory(Base, "online_resolution_stale")
    try:
        pending = _observation(
            prices='["0.98", "0.02"]',
            updated_at=BASE_TIME + timedelta(minutes=2),
        )
        stale_open = _observation(
            prices='["0.55", "0.45"]',
            closed=False,
            accepting_orders=True,
            resolution_status=None,
            updated_at=BASE_TIME,
            observed_at=BASE_TIME + timedelta(minutes=3),
        )

        async with session_factory() as session:
            await persist_observation(session, pending)
            await session.commit()

        async with session_factory() as session:
            result = await persist_observation(session, stale_open)
            assert result.observation_inserted is True
            assert result.previous_state == "closed_pending"
            assert result.current_state == "closed_pending"
            assert result.fact_changed is False
            assert result.fact_version == 1
            await session.commit()

        async with session_factory() as session:
            current = (await session.execute(select(OnlineMarketResolution))).scalar_one()
            observation_count = await session.scalar(
                select(func.count()).select_from(OnlineMarketResolutionObservation)
            )

        assert current.state == "closed_pending"
        assert current.evidence_hash == pending.evidence_hash
        assert observation_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_default_does_not_commit_and_caller_owns_transaction():
    engine, session_factory = await build_postgres_session_factory(Base, "online_resolution_commit_owner")
    try:
        session = session_factory()
        await persist_observation(session, _observation())
        await session.close()

        async with session_factory() as probe:
            observations = await probe.scalar(
                select(func.count()).select_from(OnlineMarketResolutionObservation)
            )
            facts = await probe.scalar(select(func.count()).select_from(OnlineMarketResolution))

        assert observations == 0
        assert facts == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_rolls_back_observation_and_fact_together_on_database_failure():
    engine, session_factory = await build_postgres_session_factory(Base, "online_resolution_atomic_failure")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE FUNCTION fail_online_resolution_fact() RETURNS trigger AS $$
                    BEGIN
                        RAISE EXCEPTION 'injected current fact failure';
                    END;
                    $$ LANGUAGE plpgsql
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TRIGGER fail_online_resolution_fact_insert
                    BEFORE INSERT ON online_market_resolutions
                    FOR EACH ROW EXECUTE FUNCTION fail_online_resolution_fact()
                    """
                )
            )

        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="injected current fact failure"):
                await persist_observation(session, _observation(), commit=True)

        async with session_factory() as probe:
            observations = await probe.scalar(
                select(func.count()).select_from(OnlineMarketResolutionObservation)
            )
            facts = await probe.scalar(select(func.count()).select_from(OnlineMarketResolution))

        assert observations == 0
        assert facts == 0
    finally:
        await engine.dispose()
