import contextlib
import asyncio
from unittest.mock import AsyncMock

import pytest

from workers import tracked_traders_worker


@pytest.mark.asyncio
async def test_apply_market_tradability_persists_closed_confluence_signals(monkeypatch):
    rows = [
        {"market_id": "0xclosed", "is_active": True, "is_tradeable": True},
        {"market_id": "0xopen", "is_active": True},
        {"market_id": "0xunknown", "is_active": True},
    ]
    tradability_loader = AsyncMock(
        return_value={
            "0xclosed": False,
            "0xopen": True,
        }
    )
    deactivate = AsyncMock(return_value=1)
    monkeypatch.setattr(
        tracked_traders_worker,
        "get_market_tradability_map",
        tradability_loader,
    )
    monkeypatch.setattr(
        tracked_traders_worker.wallet_intelligence.confluence,
        "deactivate_market_signals",
        deactivate,
        raising=False,
    )

    result = await tracked_traders_worker._apply_market_tradability_to_firehose(rows)

    tradability_loader.assert_awaited_once_with(["0xclosed", "0xopen", "0xunknown"])
    deactivate.assert_awaited_once_with(["0xclosed"])
    assert result == {"0xclosed": False, "0xopen": True}
    assert rows[0]["is_active"] is False
    assert rows[0]["is_tradeable"] is False
    assert rows[1]["is_active"] is True
    assert rows[1]["is_tradeable"] is True
    assert rows[2]["is_active"] is True
    assert "is_tradeable" not in rows[2]


@pytest.mark.asyncio
async def test_attach_cached_market_execution_metadata_adds_tokens_without_http(monkeypatch):
    rows = [
        {
            "market_id": "0xactive",
            "is_active": True,
            "is_tradeable": True,
        },
        {
            "market_id": "0xinactive",
            "is_active": False,
            "is_tradeable": False,
        },
    ]
    cache_lookup = AsyncMock(
        return_value={
            "token_ids": ["yes-token", "no-token"],
            "outcomes": ["Yes", "No"],
        }
    )
    monkeypatch.setattr(
        tracked_traders_worker.market_cache_service,
        "get_market",
        cache_lookup,
    )

    attached = await tracked_traders_worker._attach_cached_market_execution_metadata(rows)

    cache_lookup.assert_awaited_once_with("0xactive")
    assert attached == 1
    assert rows[0]["market_token_ids"] == ["yes-token", "no-token"]
    assert rows[0]["yes_token_id"] == "yes-token"
    assert rows[0]["no_token_id"] == "no-token"
    assert rows[0]["outcome_labels"] == ["Yes", "No"]
    assert "yes_token_id" not in rows[1]


@pytest.mark.asyncio
async def test_graceful_timeout_closes_rejected_coroutine_when_prior_task_still_running():
    gate = asyncio.Event()

    async def _long_running():
        try:
            await gate.wait()
        finally:
            gate.set()

    async def _rejected():
        return {"flagged_insiders": 0}

    tracked_traders_worker._inflight_timed_tasks.clear()
    tracked_traders_worker._abandoned_tasks.clear()
    existing = asyncio.create_task(_long_running(), name="tracked-traders-test-existing")
    tracked_traders_worker._inflight_timed_tasks["insider_rescore"] = existing
    rejected_coro = _rejected()
    try:
        with pytest.raises(tracked_traders_worker._TimedTaskStillRunningError):
            await tracked_traders_worker._graceful_timeout(
                rejected_coro,
                timeout=0.01,
                label="insider_rescore",
            )
        assert rejected_coro.cr_frame is None
    finally:
        existing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing
        tracked_traders_worker._inflight_timed_tasks.clear()
        tracked_traders_worker._abandoned_tasks.clear()
