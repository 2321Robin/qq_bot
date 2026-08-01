"""Application resource container owned by the driver lifecycle (S1-LIFE).

The runtime is created once on driver startup and owns the shared HTTP client,
the chat repository and the per-dependency circuit breakers. Getters fail
explicitly while the runtime is not ready or after shutdown — plugins degrade
per their existing policies instead of creating implicit fallback resources
(S1-LIFE-05).
"""

from __future__ import annotations

import enum
import logging
from typing import TYPE_CHECKING

import httpx

from qq_bot.config import BotSettings, get_settings
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.reliability import CircuitBreaker

if TYPE_CHECKING:
    from nonebot import Driver

logger = logging.getLogger("qq_bot.runtime")

BREAKER_AI_PRIMARY = "ai_primary"
BREAKER_AI_FALLBACK = "ai_fallback"
BREAKER_TAVILY = "tavily"
BREAKER_ONEBOT = "onebot"

_DEFAULT_BREAKER_NAMES = (
    BREAKER_AI_PRIMARY,
    BREAKER_AI_FALLBACK,
    BREAKER_TAVILY,
    BREAKER_ONEBOT,
)


class RuntimeState(enum.Enum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RuntimeStateError(RuntimeError):
    """Raised when runtime resources are requested in an unusable state."""


class AppRuntime:
    def __init__(self, settings: BotSettings | None = None) -> None:
        self._settings = settings
        self._state = RuntimeState.NEW
        self._http_client: httpx.AsyncClient | None = None
        self._repository: ChatMemoryRepository | None = None
        self._breakers: dict[str, CircuitBreaker] = {}

    @property
    def state(self) -> RuntimeState:
        return self._state

    def is_ready(self) -> bool:
        return self._state is RuntimeState.READY

    async def startup(self) -> None:
        """Create resources in order; on failure close what was created and
        never enter ready (S1-LIFE-02)."""
        if self._state is not RuntimeState.NEW:
            raise RuntimeStateError(f"cannot start runtime from state {self._state.value}")
        self._state = RuntimeState.STARTING
        settings = self._settings or get_settings()
        http_client: httpx.AsyncClient | None = None
        repository: ChatMemoryRepository | None = None
        try:
            http_client = httpx.AsyncClient(timeout=httpx.Timeout(settings.ai_timeout_seconds))
            repository = ChatMemoryRepository(
                settings.chat_memory_path,
                retention_days=settings.chat_memory_retention_days,
            )
            await repository.open()
            self._breakers = {
                name: CircuitBreaker(
                    name=name,
                    failure_threshold=settings.breaker_failure_threshold,
                    recovery_seconds=settings.breaker_recovery_seconds,
                )
                for name in _DEFAULT_BREAKER_NAMES
            }
        except Exception:
            if repository is not None:
                try:
                    await repository.close()
                except Exception:
                    logger.exception("failed to close repository during startup cleanup")
            if http_client is not None:
                try:
                    await http_client.aclose()
                except Exception:
                    logger.exception("failed to close HTTP client during startup cleanup")
            self._state = RuntimeState.FAILED
            raise
        self._http_client = http_client
        self._repository = repository
        self._state = RuntimeState.READY
        logger.info("runtime ready (schema version supported)")

    async def shutdown(self) -> None:
        """Release resources; repeated shutdown is idempotent (S1-LIFE-03)."""
        if self._state in (RuntimeState.NEW, RuntimeState.STOPPED):
            return
        self._state = RuntimeState.STOPPING
        repository, self._repository = self._repository, None
        http_client, self._http_client = self._http_client, None
        errors: list[BaseException] = []
        if repository is not None:
            try:
                await repository.close()
            except Exception as exc:
                errors.append(exc)
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception as exc:
                errors.append(exc)
        self._breakers = {}
        self._state = RuntimeState.STOPPED
        if errors:
            raise RuntimeStateError("runtime shutdown failed") from errors[0]

    def get_http_client(self) -> httpx.AsyncClient:
        self._require_ready()
        assert self._http_client is not None
        return self._http_client

    def get_chat_repository(self) -> ChatMemoryRepository:
        self._require_ready()
        assert self._repository is not None
        return self._repository

    def get_breaker(self, name: str) -> CircuitBreaker:
        self._require_ready()
        try:
            return self._breakers[name]
        except KeyError as exc:
            raise RuntimeStateError(f"unknown breaker {name!r}") from exc

    def _require_ready(self) -> None:
        if self._state is not RuntimeState.READY:
            raise RuntimeStateError(f"runtime is not ready (state={self._state.value})")


_runtime: AppRuntime | None = None


def get_runtime() -> AppRuntime:
    if _runtime is None:
        raise RuntimeStateError("runtime has not been initialized")
    return _runtime


def get_chat_repository() -> ChatMemoryRepository:
    return get_runtime().get_chat_repository()


def get_http_client() -> httpx.AsyncClient:
    return get_runtime().get_http_client()


def install_runtime_lifecycle(driver: Driver) -> None:
    """Install startup/shutdown hooks; must be called before loading plugins."""
    global _runtime
    if _runtime is not None:
        raise RuntimeStateError("runtime lifecycle is already installed")
    runtime = AppRuntime()
    _runtime = runtime

    @driver.on_startup
    async def _startup() -> None:
        await runtime.startup()

    @driver.on_shutdown
    async def _shutdown() -> None:
        await runtime.shutdown()


def set_runtime_for_testing(runtime: AppRuntime | None) -> None:
    """Test-only override of the module-level runtime singleton."""
    global _runtime
    _runtime = runtime
