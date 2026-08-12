from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.news import shared_state


@pytest.mark.asyncio
async def test_partial_news_heartbeat_merges_previous_cycle_stats(monkeypatch):
    row = SimpleNamespace(
        stats_json={
            "findings": 4,
            "intents": 1,
            "articles_considered": 17,
            "pending_intents": 1,
        }
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: row)
        )
    )
    monkeypatch.setattr(shared_state, "_commit_with_retry", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(shared_state.event_bus, "publish", publish)

    await shared_state.write_news_snapshot(
        session,
        status={
            "running": True,
            "enabled": True,
            "interval_seconds": 120,
            "current_activity": "Idle",
        },
        stats={"pending_intents": 2},
        merge_stats=True,
    )

    assert row.stats_json == {
        "findings": 4,
        "intents": 1,
        "articles_considered": 17,
        "pending_intents": 2,
    }
    update_payload = publish.await_args_list[1].args[1]
    assert update_payload["findings"] == 4
    assert update_payload["intents"] == 1
    assert update_payload["pending_intents"] == 2

