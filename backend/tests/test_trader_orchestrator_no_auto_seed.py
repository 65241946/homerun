import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.database import Base, Trader
from services.strategy_sdk import StrategySDK
from services.trader_orchestrator.templates import TRADER_TEMPLATES
from services.trader_orchestrator_state import (
    create_trader,
    create_trader_from_template,
    delete_trader,
    get_orchestrator_overview,
    list_trader_templates,
    list_traders,
    update_trader,
)
from tests.postgres_test_db import build_postgres_session_factory
from workers import trader_orchestrator_worker


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@pytest_asyncio.fixture(scope="function")
async def postgres_session_factory():
    engine, session_factory = await build_postgres_session_factory(Base, "trader_orchestrator_no_auto_seed")
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_database_state(postgres_session_factory):
    table_names = [_quote_ident(table.name) for table in Base.metadata.sorted_tables]
    if not table_names:
        return
    async with postgres_session_factory() as session:
        await session.execute(text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.mark.asyncio
async def test_overview_does_not_seed_default_traders(postgres_session_factory):
    async with postgres_session_factory() as session:
        overview = await get_orchestrator_overview(session)
        traders = await list_traders(session)
        count = int((await session.execute(select(func.count(Trader.id)))).scalar() or 0)

    assert traders == []
    assert int(overview["metrics"]["traders_total"]) == 0
    assert count == 0


@pytest.mark.asyncio
async def test_overview_stays_empty_after_deleting_last_trader(postgres_session_factory):
    async with postgres_session_factory() as session:
        trader = await create_trader(
            session,
            {
                "name": "One-Off Trader",
                "source_configs": [
                    {
                        "source_key": "crypto",
                        "strategy_key": "btc_eth_maker_quote",
                        "strategy_params": {},
                    }
                ],
            },
        )
        deleted = await delete_trader(session, trader["id"])
        overview = await get_orchestrator_overview(session)
        traders = await list_traders(session)
        count = int((await session.execute(select(func.count(Trader.id)))).scalar() or 0)

    assert deleted is True
    assert traders == []
    assert int(overview["metrics"]["traders_total"]) == 0
    assert count == 0


@pytest.mark.asyncio
async def test_worker_loop_does_not_seed_default_traders(postgres_session_factory, monkeypatch):
    monkeypatch.setattr(trader_orchestrator_worker, "AsyncSessionLocal", postgres_session_factory)
    monkeypatch.setattr(trader_orchestrator_worker, "expire_stale_signals", AsyncMock())
    monkeypatch.setattr(trader_orchestrator_worker, "ensure_all_strategies_seeded", AsyncMock())
    monkeypatch.setattr(trader_orchestrator_worker, "refresh_strategy_runtime_if_needed", AsyncMock())
    monkeypatch.setattr(trader_orchestrator_worker, "_build_orchestrator_snapshot_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(
        trader_orchestrator_worker,
        "read_orchestrator_control",
        AsyncMock(
            return_value={
                "is_enabled": False,
                "is_paused": True,
                "run_interval_seconds": 1,
            }
        ),
    )
    monkeypatch.setattr(trader_orchestrator_worker, "write_orchestrator_snapshot", AsyncMock())
    monkeypatch.setattr(
        trader_orchestrator_worker,
        "_ensure_orchestrator_cycle_lock_owner",
        AsyncMock(return_value=True),
    )

    from services.event_bus import event_bus

    monkeypatch.setattr(event_bus, "start", AsyncMock())
    monkeypatch.setattr(event_bus, "subscribe", lambda *a, **kw: None)

    async def _cancel_wait(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(trader_orchestrator_worker, "_wait_for_runtime_trigger", _cancel_wait)

    with pytest.raises(asyncio.CancelledError):
        await trader_orchestrator_worker.run_worker_loop()

    async with postgres_session_factory() as session:
        count = int((await session.execute(select(func.count(Trader.id)))).scalar() or 0)

    assert count == 0


@pytest.mark.asyncio
async def test_create_trader_copies_settings_from_existing_trader(postgres_session_factory):
    async with postgres_session_factory() as session:
        source = await create_trader(
            session,
            {
                "name": "Source Trader",
                "description": "Copy source",
                "mode": "live",
                "source_configs": [
                    {
                        "source_key": "crypto",
                        "strategy_key": "btc_eth_maker_quote",
                        "strategy_params": {"strategy_mode": "maker_quote"},
                    }
                ],
                "interval_seconds": 9,
                "risk_limits": {"max_orders_per_cycle": 3},
                "metadata": {"notes": "copied metadata"},
                "is_enabled": False,
                "is_paused": True,
            },
        )

        copied = await create_trader(
            session,
            {
                "name": "Copied Trader",
                "copy_from_trader_id": source["id"],
            },
        )

    assert copied["id"] != source["id"]
    assert copied["name"] == "Copied Trader"
    assert copied["description"] == source["description"]
    assert copied["mode"] == source["mode"]
    assert copied["source_configs"] == source["source_configs"]
    assert copied["interval_seconds"] == source["interval_seconds"]
    assert copied["risk_limits"] == source["risk_limits"]
    assert copied["metadata"] == source["metadata"]
    assert copied["is_enabled"] == source["is_enabled"]
    assert copied["is_paused"] == source["is_paused"]
    assert copied["requested_run_at"] is None
    assert copied["last_run_at"] is None
    assert copied["next_run_at"] is None


@pytest.mark.asyncio
async def test_create_trader_copy_rejects_unknown_source(postgres_session_factory):
    async with postgres_session_factory() as session:
        with pytest.raises(ValueError, match="Source trader not found"):
            await create_trader(
                session,
                {
                    "name": "Copied Trader",
                    "copy_from_trader_id": "missing-source",
                },
            )


@pytest.mark.asyncio
async def test_create_trader_scopes_by_mode_and_list_filter(postgres_session_factory):
    async with postgres_session_factory() as session:
        shadow = await create_trader(
            session,
            {
                "name": "Shadow Scoped Trader",
                "mode": "shadow",
                "source_configs": [
                    {
                        "source_key": "crypto",
                        "strategy_key": "btc_eth_maker_quote",
                        "strategy_params": {},
                    }
                ],
            },
        )
        live = await create_trader(
            session,
            {
                "name": "Live Scoped Trader",
                "mode": "live",
                "source_configs": [
                    {
                        "source_key": "crypto",
                        "strategy_key": "btc_eth_maker_quote",
                        "strategy_params": {},
                    }
                ],
            },
        )
        shadow_rows = await list_traders(session, mode="shadow")
        live_rows = await list_traders(session, mode="live")

    assert shadow["mode"] == "shadow"
    assert live["mode"] == "live"
    assert {row["id"] for row in shadow_rows} == {shadow["id"]}
    assert {row["id"] for row in live_rows} == {live["id"]}


@pytest.mark.asyncio
async def test_create_trader_rejects_legacy_paper_mode(postgres_session_factory):
    async with postgres_session_factory() as session:
        with pytest.raises(ValueError, match="mode must be 'shadow' or 'live'"):
            await create_trader(
                session,
                {
                    "name": "Legacy Paper Trader",
                    "mode": "paper",
                    "source_configs": [
                        {
                            "source_key": "crypto",
                            "strategy_key": "btc_eth_maker_quote",
                            "strategy_params": {},
                        }
                    ],
                },
            )


@pytest.mark.asyncio
async def test_create_trader_pauses_live_start_without_explicit_risk_caps(postgres_session_factory):
    async with postgres_session_factory() as session:
        trader = await create_trader(
            session,
            {
                "name": "Live Trader Missing Caps",
                "mode": "live",
                "source_configs": [
                    {
                        "source_key": "scanner",
                        "strategy_key": "settlement_lag",
                        "strategy_params": {},
                    }
                ],
                "is_enabled": True,
            },
        )

    assert trader["mode"] == "live"
    assert trader["is_enabled"] is True
    assert trader["is_paused"] is True
    assert float(trader["risk_limits"]["max_trade_notional_usd"]) == 5.0
    assert float(trader["risk_limits"]["max_position_notional_usd"]) == 5.0


@pytest.mark.asyncio
async def test_create_trader_rejects_invalid_mode(postgres_session_factory):
    async with postgres_session_factory() as session:
        with pytest.raises(ValueError, match="mode must be 'shadow' or 'live'"):
            await create_trader(
                session,
                {
                    "name": "Invalid Mode Trader",
                    "mode": "both",
                    "source_configs": [
                        {
                            "source_key": "crypto",
                            "strategy_key": "btc_eth_maker_quote",
                            "strategy_params": {},
                        }
                    ],
                },
            )


@pytest.mark.asyncio
async def test_update_trader_rejects_unknown_strategy_key(postgres_session_factory):
    async with postgres_session_factory() as session:
        trader = await create_trader(
            session,
            {
                "name": "Strict Strategy Trader",
                "source_configs": [
                    {
                        "source_key": "crypto",
                        "strategy_key": "btc_eth_maker_quote",
                        "strategy_params": {},
                    }
                ],
            },
        )

        with pytest.raises(ValueError, match="Unknown strategy_key"):
            await update_trader(
                session,
                trader["id"],
                {
                    "source_configs": [
                        {
                            "source_key": "crypto",
                            "strategy_key": "not_a_real_strategy",
                            "strategy_params": {},
                        }
                    ],
                },
            )


def test_every_trader_template_survives_source_config_normalization():
    """``GET /traders/templates`` normalizes every template to build one response.

    So a single template that fails normalization does not degrade to
    "that preset is missing" — it raises out of the list comprehension and
    takes the whole endpoint down with it.  ``btc_eth_full_stack`` carried
    three ``crypto`` source_configs from the legacy multi-mode design,
    which the one-strategy-per-source rule rejects, so the endpoint
    returned 500 for every caller and the UI's template picker had nothing
    to show.
    """
    templates = list_trader_templates()

    assert len(templates) == len(TRADER_TEMPLATES)
    for template in templates:
        # ``_validate_source_configs`` requires exactly one entry, so a
        # template with two is un-creatable even when its source_keys are
        # distinct — ``scanner_weather`` bundled a scanner and a weather
        # config and cleared normalization only to be rejected at create.
        assert len(template["source_configs"]) == 1, (
            f"{template['id']} bundles {len(template['source_configs'])} source_configs; "
            "a trader runs one strategy on one source"
        )


@pytest.mark.asyncio
async def test_every_trader_template_can_actually_be_created(postgres_session_factory):
    """Listing a preset the API cannot instantiate is a dead button.

    Normalization is not the only thing standing between a template and a
    trader — ``create_trader`` also validates strategy keys against the
    catalog — so this exercises the real create path for every preset
    rather than asserting on the template dicts.
    """
    defaults = StrategySDK.TRADER_RISK_DEFAULTS

    async with postgres_session_factory() as session:
        for template in TRADER_TEMPLATES:
            trader = await create_trader_from_template(session, template["id"])

            assert trader["metadata"]["template_id"] == template["id"]

            # Risk limits come back fully normalized, so a cap the template
            # never mentioned still has a value.  ``max_spread_bps`` is the
            # one that matters most: its gate reads a missing cap as "knob
            # off", so an unnormalized row would silently run unbounded.
            limits = trader["risk_limits"]
            missing = sorted(set(defaults) - set(limits))
            assert not missing, f"{template['id']} created without defaults: {missing}"
            assert limits["max_spread_bps"] == defaults["max_spread_bps"]

            # The template still wins wherever it sets a value.
            for key, value in (template.get("risk_limits") or {}).items():
                assert limits[key] == value, f"{template['id']} lost template value for {key}"
