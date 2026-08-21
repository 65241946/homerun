import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from workers import fast_trader_runtime, trader_orchestrator_worker  # noqa: E402


class _ReachedDatabase(RuntimeError):
    pass


class _StopAtDatabase:
    async def __aenter__(self):
        raise _ReachedDatabase

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StaleWalletCache:
    def is_fresh(self):
        return False, "missing_rest_seed"

    def stats_snapshot(self):
        return {"rest_seed_count": 0}


@pytest.mark.asyncio
async def test_shadow_orchestrator_does_not_consult_live_wallet_freshness(monkeypatch):
    monkeypatch.setattr(
        trader_orchestrator_worker,
        "get_wallet_state_cache",
        lambda: (_ for _ in ()).throw(AssertionError("shadow must not read live wallet freshness")),
    )
    monkeypatch.setattr(trader_orchestrator_worker, "AsyncSessionLocal", lambda: _StopAtDatabase())

    with pytest.raises(_ReachedDatabase):
        await trader_orchestrator_worker._run_trader_once(
            {"id": "shadow-trader", "source_configs": [], "risk_limits": {}, "metadata": {}},
            {"mode": "shadow", "settings": {}},
        )


@pytest.mark.asyncio
async def test_live_orchestrator_still_refuses_stale_wallet_state(monkeypatch):
    monkeypatch.setattr(trader_orchestrator_worker, "get_wallet_state_cache", lambda: _StaleWalletCache())
    monkeypatch.setattr(
        trader_orchestrator_worker,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("live stale gate must run before DB work")),
    )

    result = await trader_orchestrator_worker._run_trader_once(
        {"id": "live-trader", "source_configs": [], "risk_limits": {}, "metadata": {}},
        {"mode": "live", "settings": {}},
    )

    assert result == (0, 0, 0)


def _fast_trader(mode: str) -> dict:
    return {
        "id": f"fast-{mode}",
        "mode": mode,
        "source_configs": [
            {
                "source_key": "crypto",
                "strategy_key": "btc_eth_maker_quote",
                "enabled": True,
                "strategy_params": {},
            }
        ],
        "risk_limits": {},
    }


@pytest.mark.asyncio
async def test_shadow_fast_trader_does_not_consult_live_wallet_freshness(monkeypatch):
    runner = fast_trader_runtime._FastTraderTask(_fast_trader("shadow"), asyncio.Event())
    monkeypatch.setattr(
        fast_trader_runtime,
        "get_wallet_state_cache",
        lambda: (_ for _ in ()).throw(AssertionError("shadow must not read live wallet freshness")),
    )
    monkeypatch.setattr(fast_trader_runtime, "FastAsyncSessionLocal", lambda: _StopAtDatabase())

    with pytest.raises(_ReachedDatabase):
        await runner._run_once_inner("fast-shadow", ["crypto"])


@pytest.mark.asyncio
async def test_live_fast_trader_still_refuses_stale_wallet_state(monkeypatch):
    runner = fast_trader_runtime._FastTraderTask(_fast_trader("live"), asyncio.Event())
    monkeypatch.setattr(fast_trader_runtime, "get_wallet_state_cache", lambda: _StaleWalletCache())
    monkeypatch.setattr(
        fast_trader_runtime,
        "FastAsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("live stale gate must run before DB work")),
    )

    await runner._run_once_inner("fast-live", ["crypto"])
