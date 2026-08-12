import sys
import asyncio
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import models.database as database_module
from workers import host


def test_detection_plane_loads_sports_market_refresh_strategies():
    source_keys = host._PLANE_CONFIGS["detection"]["strategy_source_keys"]

    assert "scanner" in source_keys
    assert "sports" in source_keys


def test_should_suppress_asyncio_exception_for_asyncpg_backend_pid_noise():
    exc = AttributeError("'NoneType' object has no attribute 'backend_pid'")

    assert host._should_suppress_asyncio_exception("Future exception was never retrieved", exc) is True


def test_should_suppress_asyncio_exception_for_transport_noise():
    assert host._should_suppress_asyncio_exception("_ProactorBasePipeTransport._loop_reading", None) is True


def test_should_suppress_asyncio_exception_for_protocol_invalid_state_noise():
    exc = asyncio.InvalidStateError("invalid state")

    assert host._should_suppress_asyncio_exception("Fatal error: protocol.data_received() call failed.", exc) is True


def test_should_suppress_asyncpg_internal_client_error_post_cancel():
    """asyncpg's protocol-state error from drain task is benign noise."""
    asyncpg_exc = type(
        "InternalClientError", (Exception,), {"__module__": "asyncpg.exceptions._base"}
    )("got result for unknown protocol state 3")

    assert host._should_suppress_asyncio_exception("Task exception was never retrieved", asyncpg_exc) is True


def test_should_suppress_sqlalchemy_resource_closed_error():
    """SQLAlchemy ResourceClosedError from drain task is benign noise."""
    sa_exc = type(
        "ResourceClosedError", (Exception,), {"__module__": "sqlalchemy.exc"}
    )("This transaction is closed")

    assert host._should_suppress_asyncio_exception("Task exception was never retrieved", sa_exc) is True


def test_should_suppress_sqlalchemy_interface_error_rvf5():
    """SQLAlchemy InterfaceError linked to rvf5 (closed connection) is benign."""
    sa_exc = type(
        "InterfaceError", (Exception,), {"__module__": "sqlalchemy.exc"}
    )("(Background on this error at: https://sqlalche.me/e/20/rvf5)")

    assert host._should_suppress_asyncio_exception("Task exception was never retrieved", sa_exc) is True


def test_should_not_suppress_arbitrary_runtime_error():
    """A non-cleanup error must still surface at ERROR level."""
    assert host._should_suppress_asyncio_exception("Task exception was never retrieved", ValueError("bad")) is False


@pytest.mark.asyncio
async def test_initialize_services_schedules_live_execution_in_background(monkeypatch):
    worker_host = host.WorkerHost("all")
    worker_host._plane_config = {key: False for key in worker_host._plane_config}
    worker_host._plane_config["initialize_live_execution"] = True

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_live_initialize():
        started.set()
        await release.wait()
        return True

    async def fake_watchdog():
        await release.wait()

    async def fake_memory_loop():
        await release.wait()

    monkeypatch.setattr(host.live_execution_service, "initialize", fake_live_initialize)
    monkeypatch.setattr(database_module, "start_pool_watchdog", lambda: asyncio.create_task(fake_watchdog()))
    monkeypatch.setattr("utils.memory_diagnostic.memory_diagnostic_loop", fake_memory_loop)

    await worker_host._initialize_services()

    live_init_tasks = [
        task for task in worker_host._background_tasks if task.get_name() == "all-live-execution-init"
    ]
    assert len(live_init_tasks) == 1
    assert not live_init_tasks[0].done()
    await asyncio.wait_for(started.wait(), timeout=0.2)

    release.set()
    await asyncio.gather(*worker_host._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_initialize_services_schedules_runtime_bootstrap_in_background(monkeypatch):
    worker_host = host.WorkerHost("all")
    worker_host._plane_config = {key: False for key in worker_host._plane_config}
    worker_host._plane_config["start_intent_runtime"] = True
    worker_host._plane_config["start_feed_manager"] = True
    worker_host._plane_config["start_market_runtime"] = True

    release = asyncio.Event()

    class _FakeIntentRuntime:
        def __init__(self) -> None:
            self._started = False
            self.entered = asyncio.Event()

        @property
        def started(self) -> bool:
            return self._started

        async def start(self) -> None:
            self.entered.set()
            await release.wait()
            self._started = True

    class _FakeStartable:
        def __init__(self) -> None:
            self._started = False
            self.entered = asyncio.Event()

        async def start(self) -> None:
            self.entered.set()
            await release.wait()
            self._started = True

    async def fake_watchdog():
        await release.wait()

    async def fake_memory_loop():
        await release.wait()

    fake_intent_runtime = _FakeIntentRuntime()
    fake_feed_manager = _FakeStartable()
    fake_market_runtime = _FakeStartable()

    monkeypatch.setattr(host, "get_intent_runtime", lambda: fake_intent_runtime)
    monkeypatch.setattr(host, "get_feed_manager", lambda: fake_feed_manager)
    monkeypatch.setattr(host, "get_market_runtime", lambda: fake_market_runtime)
    monkeypatch.setattr(database_module, "start_pool_watchdog", lambda: asyncio.create_task(fake_watchdog()))
    monkeypatch.setattr("utils.memory_diagnostic.memory_diagnostic_loop", fake_memory_loop)

    await worker_host._initialize_services()

    bootstrap_tasks = {
        task.get_name(): task
        for task in worker_host._background_tasks
        if task.get_name() in {
            "all-intent-runtime-init",
            "all-feed-manager-init",
            "all-market-runtime-init",
        }
    }
    assert set(bootstrap_tasks) == {
        "all-intent-runtime-init",
        "all-feed-manager-init",
        "all-market-runtime-init",
    }
    assert all(not task.done() for task in bootstrap_tasks.values())

    await asyncio.wait_for(fake_intent_runtime.entered.wait(), timeout=0.2)
    await asyncio.wait_for(fake_feed_manager.entered.wait(), timeout=0.2)
    await asyncio.wait_for(fake_market_runtime.entered.wait(), timeout=0.2)

    release.set()
    await asyncio.gather(*worker_host._background_tasks, return_exceptions=True)
