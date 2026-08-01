"""Runtime lifecycle tests (S1-LIFE-01..05)."""

from __future__ import annotations

import aiosqlite
import httpx
import pytest

from qq_bot.config import BotSettings
from qq_bot.runtime import (
    BREAKER_AI_FALLBACK,
    BREAKER_AI_PRIMARY,
    BREAKER_ONEBOT,
    BREAKER_TAVILY,
    AppRuntime,
    RuntimeState,
    RuntimeStateError,
    get_runtime,
    install_runtime_lifecycle,
    set_runtime_for_testing,
)


@pytest.fixture(autouse=True)
def _clean_runtime_singleton() -> object:
    yield
    set_runtime_for_testing(None)


def _settings(tmp_path) -> BotSettings:
    return BotSettings(chat_memory_path=str(tmp_path / "runtime.sqlite3"))


@pytest.mark.asyncio
async def test_startup_reaches_ready_and_owns_shared_resources(tmp_path) -> None:
    runtime = AppRuntime(settings=_settings(tmp_path))
    await runtime.startup()
    try:
        assert runtime.state is RuntimeState.READY
        assert runtime.is_ready()
        client = runtime.get_http_client()
        assert isinstance(client, httpx.AsyncClient)
        repository = runtime.get_chat_repository()
        assert repository.is_open
        for name in (
            BREAKER_AI_PRIMARY,
            BREAKER_AI_FALLBACK,
            BREAKER_TAVILY,
            BREAKER_ONEBOT,
        ):
            assert runtime.get_breaker(name) is not None
        with pytest.raises(RuntimeStateError):
            runtime.get_breaker("no_such_breaker")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_from_non_new_state_rejected(tmp_path) -> None:
    runtime = AppRuntime(settings=_settings(tmp_path))
    await runtime.startup()
    try:
        with pytest.raises(RuntimeStateError):
            await runtime.startup()
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_closes_resources(tmp_path) -> None:
    runtime = AppRuntime(settings=_settings(tmp_path))
    await runtime.startup()
    repository = runtime.get_chat_repository()
    client = runtime.get_http_client()
    await runtime.shutdown()
    assert runtime.state is RuntimeState.STOPPED
    assert not repository.is_open
    assert client.is_closed
    with pytest.raises(RuntimeStateError):
        runtime.get_http_client()
    with pytest.raises(RuntimeStateError):
        runtime.get_chat_repository()
    with pytest.raises(RuntimeStateError):
        runtime.get_breaker(BREAKER_ONEBOT)
    await runtime.shutdown()  # idempotent
    assert runtime.state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_shutdown_before_startup_is_noop() -> None:
    runtime = AppRuntime()
    await runtime.shutdown()
    assert runtime.state is RuntimeState.NEW  # untouched, getters still fail


@pytest.mark.asyncio
async def test_getters_fail_while_not_ready() -> None:
    runtime = AppRuntime()
    assert runtime.state is RuntimeState.NEW
    with pytest.raises(RuntimeStateError):
        runtime.get_http_client()
    with pytest.raises(RuntimeStateError):
        runtime.get_chat_repository()
    with pytest.raises(RuntimeStateError):
        runtime.get_breaker(BREAKER_AI_PRIMARY)


@pytest.mark.asyncio
async def test_startup_failure_cleans_up_and_never_reaches_ready(tmp_path, monkeypatch) -> None:
    # pre-create a database whose schema version is newer than supported,
    # forcing repository.open() to fail
    path = tmp_path / "newer.sqlite3"
    connection = await aiosqlite.connect(path)
    await connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await connection.execute(
        "INSERT INTO schema_migrations VALUES (999, '2026-08-01T00:00:00+00:00')"
    )
    await connection.commit()
    await connection.close()

    closed_clients: list[httpx.AsyncClient] = []

    async def fake_aclose(self: httpx.AsyncClient) -> None:
        closed_clients.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", fake_aclose)

    runtime = AppRuntime(settings=BotSettings(chat_memory_path=str(path)))
    with pytest.raises(Exception):
        await runtime.startup()
    assert runtime.state is RuntimeState.FAILED
    assert closed_clients, "HTTP client must be closed during startup cleanup"
    with pytest.raises(RuntimeStateError):
        runtime.get_http_client()
    with pytest.raises(RuntimeStateError):
        runtime.get_chat_repository()

    # shutdown from FAILED state must still release and settle cleanly
    await runtime.shutdown()
    assert runtime.state is RuntimeState.STOPPED


class FakeDriver:
    def __init__(self) -> None:
        self.startup_hooks: list = []
        self.shutdown_hooks: list = []

    def on_startup(self, fn):
        self.startup_hooks.append(fn)
        return fn

    def on_shutdown(self, fn):
        self.shutdown_hooks.append(fn)
        return fn


def test_install_runtime_lifecycle_wires_hooks_and_singleton() -> None:
    driver = FakeDriver()
    install_runtime_lifecycle(driver)  # type: ignore[arg-type]
    try:
        assert len(driver.startup_hooks) == 1
        assert len(driver.shutdown_hooks) == 1
        assert get_runtime() is not None
        with pytest.raises(RuntimeStateError, match="already installed"):
            install_runtime_lifecycle(FakeDriver())  # type: ignore[arg-type]
    finally:
        set_runtime_for_testing(None)
    with pytest.raises(RuntimeStateError, match="not been initialized"):
        get_runtime()


def test_set_runtime_for_testing_injects_singleton() -> None:
    runtime = AppRuntime()
    set_runtime_for_testing(runtime)
    try:
        assert get_runtime() is runtime
    finally:
        set_runtime_for_testing(None)
