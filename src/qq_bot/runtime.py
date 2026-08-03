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
from typing import TYPE_CHECKING, Callable
import httpx

from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics, set_trace_enabled
from qq_bot.observability.logging import get_logger, install_logging, record_event
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.reliability import CircuitBreaker, CircuitState

if TYPE_CHECKING:
    from qq_bot.services.quota import QuotaService

if TYPE_CHECKING:
    from nonebot import Driver

    from qq_bot.agent.orchestrator import AgentOrchestrator
    from qq_bot.agent.registry import ToolRegistry
    from qq_bot.services.ai_client import AiModelGateway
    from qq_bot.services.layered_memory import LayeredMemoryService

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


def _breaker_state_callback(
    name: str,
) -> Callable[[CircuitState, CircuitState], None]:
    """Breaker transition callback, registered once per breaker at startup
    (S4-METRIC-03): maintains the state gauge, counts transitions and emits a
    structured event. Never registered on a hot path."""
    runtime_logger = get_logger("qq_bot.runtime")

    def _on_state_change(old_state: CircuitState, new_state: CircuitState) -> None:
        metrics.CIRCUIT_INFO.labels(name, old_state.value).set(0)
        metrics.CIRCUIT_INFO.labels(name, new_state.value).set(1)
        metrics.CIRCUIT_TRANSITIONS.labels(name, new_state.value).inc()
        record_event(
            runtime_logger,
            logging.INFO,
            "circuit_state_changed",
            message=f"circuit {name}: {old_state.value} -> {new_state.value}",
            circuit_state=new_state.value,
        )

    return _on_state_change


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
        self._registry: ToolRegistry | None = None
        self._gateway: AiModelGateway | None = None
        self._orchestrator: AgentOrchestrator | None = None
        self._memory: LayeredMemoryService | None = None
        self._quota: QuotaService | None = None

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
        # Production hookup of the observability facade (S4-LOG-05,
        # S4-METRIC-12): applied once at startup, before any message flows.
        install_logging(log_format=settings.log_format, log_level=settings.log_level)
        metrics.set_metrics_enabled(settings.metrics_enabled)
        set_trace_enabled(settings.trace_enabled)
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
                    on_state_change=_breaker_state_callback(name),
                )
                for name in _DEFAULT_BREAKER_NAMES
            }
            for name, breaker in self._breakers.items():
                metrics.CIRCUIT_INFO.labels(name, breaker.state.value).set(1)
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
        self._registry, self._gateway, self._memory, self._orchestrator = self._build_agent_stack(
            settings, repository, http_client
        )
        if settings.quota_enabled:
            from qq_bot.services.quota import QuotaService

            self._quota = QuotaService(settings, repository)
        else:
            self._quota = None
        self._state = RuntimeState.READY
        logger.info("runtime ready (schema version supported)")

    def _build_agent_stack(
        self,
        settings: BotSettings,
        repository: ChatMemoryRepository,
        http_client: httpx.AsyncClient,
    ) -> tuple[ToolRegistry, AiModelGateway, LayeredMemoryService, AgentOrchestrator]:
        """Build the stage-2 agent resources (S2-CONFIG-01): five tools, the
        model gateway, the budget, the layered memory service and the
        orchestrator. Pure construction — no network, no model calls.

        Imports are lazy: the agent stack depends on service modules that
        import ``qq_bot.runtime`` at module level (breakers, shared client),
        so loading them here avoids an import cycle at module scope."""
        from qq_bot.agent.evidence import ModelSemanticVerifier
        from qq_bot.agent.orchestrator import AgentOrchestrator
        from qq_bot.agent.registry import ToolRegistry
        from qq_bot.agent.token_budget import BudgetManager
        from qq_bot.agent.tools.memory import register_memory_tools
        from qq_bot.agent.tools.roco import register_roco_tools
        from qq_bot.agent.tools.web import register_web_tool
        from qq_bot.services.ai_client import AiModelGateway
        from qq_bot.services.layered_memory import LayeredMemoryService

        registry = ToolRegistry()
        register_roco_tools(registry)
        register_web_tool(registry, settings=settings, client=http_client)
        register_memory_tools(registry, repository)
        registry.validate()
        gateway = AiModelGateway(settings, client=http_client)
        budget = BudgetManager(settings)
        verifier = (
            ModelSemanticVerifier(gateway, settings)
            if settings.ai_semantic_verifier_enabled
            else None
        )
        memory = LayeredMemoryService(repository, settings, gateway=gateway, budget=budget)
        orchestrator = AgentOrchestrator(
            registry=registry,
            gateway=gateway,
            settings=settings,
            verifier=verifier,
            budget=budget,
            memory=memory,
        )
        return registry, gateway, memory, orchestrator

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

    def get_tool_registry(self) -> ToolRegistry:
        self._require_ready()
        assert self._registry is not None
        return self._registry

    def get_model_gateway(self) -> AiModelGateway:
        self._require_ready()
        assert self._gateway is not None
        return self._gateway

    def get_layered_memory(self) -> LayeredMemoryService:
        self._require_ready()
        assert self._memory is not None
        return self._memory

    def get_agent_orchestrator(self) -> AgentOrchestrator:
        self._require_ready()
        assert self._orchestrator is not None
        return self._orchestrator

    def get_quota_service(self) -> QuotaService | None:
        """The quota service, or None when quota is disabled (S4-QUOTA-01).
        Same readiness semantics as the other getters."""
        self._require_ready()
        return self._quota

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
