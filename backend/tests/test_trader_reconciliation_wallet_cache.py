import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import wallet_state_cache  # noqa: E402
from workers import trader_reconciliation_worker  # noqa: E402


class _UnseededCache:
    def wallet_address(self):
        return None


@pytest.mark.asyncio
async def test_wallet_reseeder_does_not_duplicate_live_initializer_when_credentials_missing(monkeypatch):
    ensure_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(trader_reconciliation_worker.live_execution_service, "is_ready", lambda: False)
    monkeypatch.setattr(
        trader_reconciliation_worker.live_execution_service,
        "get_last_init_error",
        lambda: "missing_polymarket_credentials",
    )
    monkeypatch.setattr(trader_reconciliation_worker.live_execution_service, "ensure_initialized", ensure_mock)
    monkeypatch.setattr(wallet_state_cache, "get_wallet_state_cache", lambda: _UnseededCache())

    await trader_reconciliation_worker._reseed_wallet_state_cache_from_rest()

    ensure_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wallet_monitor_sync_does_not_duplicate_live_initializer(monkeypatch):
    ensure_mock = AsyncMock(return_value=False)
    set_wallets_mock = Mock()
    monkeypatch.setattr(trader_reconciliation_worker.live_execution_service, "is_ready", lambda: False)
    monkeypatch.setattr(trader_reconciliation_worker.live_execution_service, "ensure_initialized", ensure_mock)
    monkeypatch.setattr(
        trader_reconciliation_worker.wallet_ws_monitor,
        "set_wallets_for_source",
        set_wallets_mock,
    )

    wallet = await trader_reconciliation_worker._sync_live_wallet_monitor_source("existing-wallet")

    assert wallet == ""
    ensure_mock.assert_not_awaited()
