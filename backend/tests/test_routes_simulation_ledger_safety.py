from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api import routes_simulation


@pytest.mark.asyncio
async def test_delete_v2_account_returns_conflict_instead_of_internal_error(monkeypatch):
    delete_mock = AsyncMock(
        side_effect=ValueError(
            "v2 ledger account account-v2 cannot be deleted; "
            "its append-only cash history must be retained"
        )
    )
    monkeypatch.setattr(routes_simulation.simulation_service, "delete_account", delete_mock)

    with pytest.raises(HTTPException) as excinfo:
        await routes_simulation.delete_simulation_account("account-v2")

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {
        "code": "append_only_ledger_account",
        "message": (
            "v2 ledger account account-v2 cannot be deleted; "
            "its append-only cash history must be retained"
        ),
    }
    delete_mock.assert_awaited_once_with("account-v2")
