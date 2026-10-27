import asyncio
from types import SimpleNamespace
from datetime import UTC, datetime

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from prometheus_client import REGISTRY

from qq_bot.config import BotSettings
from qq_bot.plugins import ai_chat as ai_chat_plugin
from qq_bot.runtime import RuntimeStateError
from qq_bot.services import quote_reference
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.sticker_library import Sticker
from pathlib import Path


class FakeEvent:
    group_id = 1001
    user_id = 2001

    def __init__(
        self,
        text: str,
        segments: list[object] | None = None,
        *,
        to_me: bool = False,
        self_id: int = 2880000001,
        user_id: int | None = None,
        message_id: int = 1,
    ):
        self.text = text
        self.segments = segments or []
        self.to_me = to_me
        self.self_id = self_id
        self.message_id = message_id
        if user_id is not None:
            self.user_id = user_id

    def get_message(self) -> "FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(self.segments)

    def is_tome(self) -> bool:
        return self.to_me


class FakeAtSegment:
    type = "at"

    def __init__(self, qq: int):
        self.data = {"qq": str(qq)}


class FakeTextSegment:
    type = "text"

    def __init__(self, text: str):
        self.data = {"text": text}


class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message


class EmptyMemoryStore:
    async def add_message(self, *args, **kwargs) -> int:
        return 123

    async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
        return None

    async def recent_user_turns(self, *, group_id: int, user_id: int, limit: int):
        return []

    async def recent_group_messages(self, *, group_id: int, limit: int):
        return []


class FakeHttpClient:
    """Stand-in for the runtime-owned shared httpx client."""


@pytest.fixture(autouse=True)
def _patch_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime is not started in these tests; stub the client getter."""
    monkeypatch.setattr(ai_chat_plugin, "get_http_client", lambda: FakeHttpClient())


@pytest.fixture(autouse=True)
async def _cancel_leftover_tasks():
    """被 @ 路径会调度真实的冷场检查 task（默认沉睡 180s）；测试结束时取消
    仍未完成的 task，避免事件循环关闭时出现 "Task was destroyed" 告警。"""
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture(autouse=True)
def _fresh_shared_quote_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    """引用接线（S10-HUMAN-P0-06）使用进程级共享 LRU 单例；每个用例前重置，
    避免跨用例的消息登记残留影响引用解析结果。"""
    monkeypatch.setattr(quote_reference, "_SHARED_LRU", None)


def memory_row(
    *,
    message_text: str,
    ai_reply: str = "",
    row_id: int = 1,
    user_id: int = 2001,
) -> ChatMemoryRow:
    return ChatMemoryRow(
        id=row_id,
        group_id=1001,
        user_id=user_id,
        message_text=message_text,
        created_at="2026-05-11T00:00:00+00:00",
        is_ai_prompt=True,
        ai_reply=ai_reply,
    )


# ---- S12-UNIFY-01：addressed 与自主插话同一管线，handler 契约测试 ----


class _RecordingStore:
    """记录写库调用的假记忆库（统一管线测试用）。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def add_message(self, **kwargs) -> int:
        self.rows.append(kwargs)
        return 123


@pytest.mark.asyncio
async def _run_addressed(
    monkeypatch: pytest.MonkeyPatch,
    event: FakeEvent,
    *,
    settings: BotSettings | None = None,
    store: object | None = None,
    reply: str | None = "好了好了",
) -> dict:
    """addressed 用例公共通道：patch run_auto_chat 捕获 kwargs，fake 内调用
    注入的 send（等价真实管线发出），返回捕获的 kwargs。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)
        if reply is not None:
            await kwargs["send"](reply)
        return reply

    base = BotSettings(
        allowed_group_ids="1001",
        ai_api_key="secret",
        named_mention_replacements="@小呱呱=2880000001",
    )
    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: settings or base)
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: store or _RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    return captured


@pytest.mark.asyncio
async def test_addressed_message_routes_through_unified_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被 @ 消息走统一管线：force_reply 必回、触发文本=提问、带表情提示与
    bot 自身 QQ；回复经统一出站（named-mention 替换）发出。"""
    captured = await _run_addressed(
        monkeypatch, FakeEvent("ai 提醒我"), reply="好的，@小呱呱 会收到提醒"
    )

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 提醒我"))  # type: ignore[arg-type]

    assert captured["force_reply"] is True
    assert captured["raw_text"] == "提醒我"
    assert captured["context_limit"] is None
    assert captured["trigger_images"] == ()
    assert captured["bot_user_id"] == 2880000001
    assert "[发脸" in captured["sticker_hint"]  # 表情通道提示随统一管线生效
    message = exc_info.value.message
    assert isinstance(message, Message)
    assert message[0].type == "text"
    assert message[0].data["text"] == "好的，"
    assert message[1].type == "at"
    assert message[1].data["qq"] == "2880000001"
    assert message[2].data["text"] == " 会收到提醒"


@pytest.mark.asyncio
async def test_bare_at_generates_naturally_instead_of_canned_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单独 @ 不带任何内容：合成触发语进统一管线自然接话，不再出现
    「请在 ai 后面输入要问的问题」固定文案；记忆里以同一行文本登记。"""
    store = _RecordingStore()
    captured = await _run_addressed(monkeypatch, FakeEvent("", to_me=True), store=store)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("", to_me=True))  # type: ignore[arg-type]

    assert captured["force_reply"] is True
    assert captured["raw_text"] == "（只发了 @）"
    assert store.rows and store.rows[0]["message_text"] == "（只发了 @）"


@pytest.mark.asyncio
async def test_at_with_image_only_passes_trigger_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@ + 只发图：占位文本作触发、图片源随管线带给生成（封顶注入在服务侧）。"""
    event = FakeEvent(
        "",
        segments=[
            FakeImageSegment("https://multimedia.example/a.jpg"),
        ],
        to_me=True,
    )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    captured = await _run_addressed(monkeypatch, event)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert captured["raw_text"] == "[图片]"
    assert captured["trigger_images"] == ("https://multimedia.example/a.jpg",)


@pytest.mark.asyncio
async def test_memory_reference_limit_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """「参考最近5条」语法 → 统一管线 context_limit=5。"""
    captured = await _run_addressed(monkeypatch, FakeEvent("ai 参考最近5条：聊聊"))
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 参考最近5条：聊聊"))  # type: ignore[arg-type]
    assert captured["context_limit"] == 5


@pytest.mark.asyncio
async def test_memory_reference_user_scoping_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「参考 @某人 的最近消息」语法 → context_user_id 透传（不把 @ 自己算进去）。"""
    event = FakeEvent("ai 参考最近的消息：他说了啥")
    captured = await _run_addressed(monkeypatch, event)
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]
    assert captured["context_user_id"] is None  # 未带 @ 段时不指定人


@pytest.mark.asyncio
async def test_handler_advances_live_catchup_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """S12-CATCHUP-02：在线消息入库后推进补答水位——重连后补答只看真掉线
    窗口，不再把在线已答过的呼唤重答一遍（2026-10-27 线上事故回归）。"""
    from qq_bot.services import catchup as catchup_service
    from qq_bot.services.catchup import CatchupStateStore, LiveWatermark

    store = CatchupStateStore(tmp_path / "live.json")
    monkeypatch.setattr(catchup_service, "_LIVE_WATERMARK", LiveWatermark(store))
    await _run_addressed(monkeypatch, FakeEvent("ai 在吗"))
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 在吗"))  # type: ignore[arg-type]

    live = catchup_service.shared_live_watermark()
    assert live.get(1001) is not None and live.get(1001) > 0


@pytest.mark.asyncio
async def test_unified_pipeline_failure_sends_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一管线本轮没发出任何东西（生成失败等）→ 回落服务不可用文案。"""

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    await _run_addressed(monkeypatch, FakeEvent("ai 提醒我"), reply=None)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 提醒我"))  # type: ignore[arg-type]

    assert any(m in str(exc_info.value.message) for m in ai_chat_plugin._UNAVAILABLE_MESSAGES)


@pytest.mark.asyncio
async def test_memory_write_failure_does_not_block_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记忆写库失败不拦回复：统一管线照常被调用。"""

    class _BrokenStore:
        async def add_message(self, **kwargs):
            raise RuntimeError("disk full")

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    captured = await _run_addressed(monkeypatch, FakeEvent("ai 提醒我"), store=_BrokenStore())
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 提醒我"))  # type: ignore[arg-type]
    assert captured["force_reply"] is True


@pytest.mark.asyncio
async def test_ai_chat_replies_when_group_message_mentions_bot_without_to_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只写昵称不带 @ 也算 addressed → 统一管线必回。"""
    event = FakeEvent(
        "你好", segments=[FakeAtSegment(2880000001), FakeTextSegment("你好")], to_me=False
    )
    captured = await _run_addressed(monkeypatch, event)
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]
    assert captured["force_reply"] is True
    assert captured["raw_text"] == "你好"


@pytest.mark.asyncio
async def test_ai_chat_ignores_configured_sender_even_when_message_mentions_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """忽略名单（其他机器人）即使在消息里点名机器人也不回。"""

    async def fake_run_auto_chat(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("ignored sender must not reach the pipeline")

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            ai_ignored_user_ids="424242",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: _RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    await ai_chat_plugin.handle_ai_chat(FakeEvent("你好", user_id=424242, to_me=True))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ai_chat_ignores_self_mention_for_explicit_user_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「参考 @机器人 自己的最近消息」：@ 自己不进 context_user_id。"""
    event = FakeEvent(
        "ai 参考最近的消息：他说了啥", to_me=True, segments=[FakeAtSegment(2880000001)]
    )
    captured = await _run_addressed(monkeypatch, event)
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]
    assert captured["context_user_id"] is None


@pytest.mark.asyncio
async def test_ai_chat_uses_actual_at_segment_for_explicit_user_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「参考 @某人 的最近消息」：@ 段里的 QQ 号作 context_user_id。"""
    event = FakeEvent(
        "ai 他最近发送的消息说了啥",
        to_me=True,
        segments=[FakeAtSegment(3000777)],
    )
    captured = await _run_addressed(monkeypatch, event)
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]
    assert captured["context_user_id"] == 3000777


@pytest.mark.asyncio
async def test_ai_chat_records_ordinary_group_message_without_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通（非 addressed）消息：不入 AI 问答链，走插话收集路径。"""
    calls: list[dict] = []

    async def fake_run_auto_chat(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret", auto_chat_enabled=True),
    )
    store = _RecordingStore()
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: store)
    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    await ai_chat_plugin.handle_ai_chat(FakeEvent("今天天气不错"))  # type: ignore[arg-type]
    # 普通消息走收集器（批次路径），直接管线调用不经 handler
    assert calls == []
    assert store.rows[0]["is_ai_prompt"] in (0, False)


@pytest.mark.asyncio
async def test_agent_flag_off_keeps_unified_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_ENABLED=false：走统一管线而非 agent。"""
    captured = await _run_addressed(monkeypatch, FakeEvent("ai 你好"))
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]
    assert captured["force_reply"] is True


@pytest.mark.asyncio
async def test_agent_enabled_skips_unified_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_ENABLED=true：agent 路径接管，统一管线不被调用。"""

    async def fake_run_auto_chat(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("agent path must not call the unified pipeline")

    async def fake_agent_chat(event, prompt, settings, memory_store):
        return "agent 回复"

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret", agent_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: _RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(ai_chat_plugin, "_handle_agent_chat", fake_agent_chat)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage-2 agent path (Task 13, S2-AGENT-09): AGENT_ENABLED routing
# ---------------------------------------------------------------------------


class FakeAgentRuntime:
    """Runtime stand-in exposing the agent-stack getters."""

    def __init__(self, orchestrator: object, gateway: object | None = None):
        self._orchestrator = orchestrator
        self._gateway = gateway

    def get_agent_orchestrator(self):
        return self._orchestrator

    def get_model_gateway(self):
        if self._gateway is None:
            raise RuntimeStateError("not ready")
        return self._gateway

    def get_quota_service(self):
        """Quota disabled in this fake: admission always allowed."""
        return None


class FakeOrchestrator:
    def __init__(self, outcome: object, store: object | None = None):
        self.outcome = outcome
        self.last_store = store
        self.runs: list[object] = []

    async def run(self, request: object) -> object:
        self.runs.append(request)
        return self.outcome


def _agent_settings(**overrides) -> BotSettings:
    values = {"allowed_group_ids": "1001", "ai_api_key": "secret", "agent_enabled": True}
    values.update(overrides)
    return BotSettings(**values)


def _patch_agent_runtime(monkeypatch: pytest.MonkeyPatch, runtime: object) -> None:
    monkeypatch.setattr(ai_chat_plugin, "get_runtime", lambda: runtime)


@pytest.mark.asyncio
async def test_agent_path_routes_renders_and_updates_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT_ENABLED=true: router -> orchestrator -> rendered text; the
    memory write count stays exactly one add_message (S2-AGENT-09)."""
    from qq_bot.agent.evidence import EvidenceStore
    from qq_bot.agent.models import (
        Claim,
        Evidence,
        GroundedAnswer,
        ReasonCode,
        RouteDecision,
        RouteKind,
    )
    from qq_bot.agent.router import RouteTrace

    class Store:
        def __init__(self) -> None:
            self.writes: list[dict] = []

        async def add_message(self, **kwargs) -> int:
            self.writes.append(kwargs)
            return 1

        async def update_ai_reply(self, message_id: int, ai_reply: str) -> None:
            self.writes.append({"reply": ai_reply})

    store = Store()
    answer = GroundedAnswer(
        claims=(Claim(text="暗影格斗是洛克王国精灵", kind="factual", evidence_ids=("L1",)),),
        closing="祝游戏愉快",
    )
    from qq_bot.agent.models import ToolResult

    evidence = EvidenceStore()
    evidence.add(
        ToolResult(
            tool="lookup_pet",
            status="ok",
            evidence=(
                Evidence(
                    id="L1", source_type="local", title="暗影格斗", facts={"name": "暗影格斗"}
                ),
            ),
        )
    )
    orchestrator = FakeOrchestrator(answer, store=evidence)

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.LOCAL_KNOWLEDGE,
            confidence=0.9,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            allowed_tools=("lookup_pet", "search_chat_memory"),
        )
        trace = RouteTrace(
            route=RouteKind.LOCAL_KNOWLEDGE,
            confidence=0.9,
            reason_code=ReasonCode.EXPLICIT_COMMAND,
            latency_ms=1.0,
            is_rule=True,
        )
        return decision, trace

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: store)
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 暗影格斗是谁"))  # type: ignore[arg-type]

    assert len(store.writes) == 2  # one add_message + one update_ai_reply
    assert store.writes[0]["message_text"] == "ai 暗影格斗是谁"
    assert store.writes[1] == {"reply": "暗影格斗是洛克王国精灵；来源：本地图鉴\n祝游戏愉快"}
    assert len(orchestrator.runs) == 1
    request = orchestrator.runs[0]
    assert request.scope.group_id == "1001"
    assert request.scope.user_id == "2001"
    assert request.scope.can_use_chat_memory is True
    assert request.deadline > datetime.now(UTC)
    assert request.route.allowed_tools == ("lookup_pet", "search_chat_memory")
    message = exc_info.value.message
    assert message.extract_plain_text() == "暗影格斗是洛克王国精灵；来源：本地图鉴\n祝游戏愉快"


@pytest.mark.asyncio
async def test_agent_path_clarification_replies_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """needs_clarification: direct stable reply, orchestrator never runs
    (S2-AGENT-09 + Scenario E)."""
    from qq_bot.agent.models import ReasonCode, RouteDecision, RouteKind
    from qq_bot.agent.router import RouteTrace

    class Store:
        async def add_message(self, **kwargs) -> int:
            return 1

    orchestrator = FakeOrchestrator(object())

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.4,
            reason_code=ReasonCode.CLARIFY,
            needs_clarification=True,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.4,
            reason_code=ReasonCode.CLARIFY,
            latency_ms=1.0,
            needs_clarification=True,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: Store())
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 那个什么"))  # type: ignore[arg-type]

    assert orchestrator.runs == []  # no model, no orchestrator
    assert (
        str(exc_info.value.message)
        in (ai_chat_plugin._AGENT_CLARIFY_MESSAGES[ai_chat_plugin.ReasonCode.CLARIFY])
    )


@pytest.mark.asyncio
async def test_agent_path_capability_error_uses_stable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.agent.models import ReasonCode, RouteDecision, RouteKind
    from qq_bot.agent.router import RouteTrace

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.8,
            reason_code=ReasonCode.CAPABILITY_ERROR,
            needs_clarification=True,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.8,
            reason_code=ReasonCode.CAPABILITY_ERROR,
            latency_ms=1.0,
            needs_clarification=True,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    _patch_agent_runtime(
        monkeypatch, FakeAgentRuntime(FakeOrchestrator(object()), gateway=object())
    )
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 删除我的记忆"))  # type: ignore[arg-type]

    assert (
        str(exc_info.value.message)
        in (ai_chat_plugin._AGENT_CLARIFY_MESSAGES[ai_chat_plugin.ReasonCode.CAPABILITY_ERROR])
    )


@pytest.mark.asyncio
async def test_agent_path_safefailure_sends_stable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_bot.agent.models import (
        FailureCode,
        ReasonCode,
        RouteDecision,
        RouteKind,
        SafeFailure,
    )
    from qq_bot.agent.router import RouteTrace

    class Store:
        async def add_message(self, **kwargs) -> int:
            return 1

    async def fake_route_request(prompt, *, settings, gateway, can_use_chat_memory):
        decision = RouteDecision(
            primary_route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            allowed_tools=(),
        )
        return decision, RouteTrace(
            route=RouteKind.DIRECT_CHAT,
            confidence=0.9,
            reason_code=ReasonCode.STRUCTURED_CLASSIFIER,
            latency_ms=1.0,
            is_rule=True,
        )

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    failure = SafeFailure(code=FailureCode.DEADLINE_EXCEEDED, message="处理超时，请稍后重试。")
    orchestrator = FakeOrchestrator(failure)
    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: Store())
    _patch_agent_runtime(monkeypatch, FakeAgentRuntime(orchestrator, gateway=object()))
    monkeypatch.setattr(ai_chat_plugin, "route_request", fake_route_request)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

    assert str(exc_info.value.message) == "处理超时，请稍后重试。"
    assert len(orchestrator.runs) == 1


@pytest.mark.asyncio
async def test_agent_path_runtime_unavailable_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: _agent_settings())
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_runtime",
        lambda: (_ for _ in ()).throw(RuntimeStateError("not ready")),
    )
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

    assert str(exc_info.value.message) in ai_chat_plugin._UNAVAILABLE_MESSAGES


@pytest.mark.asyncio
async def test_ai_chat_quota_denial_blocks_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """S4-QUOTA-02: when the quota service denies admission the handler sends
    the stable denial text and never calls the model."""
    from qq_bot.services.chat_memory import ChatMemoryRepository
    from qq_bot.services.quota import QuotaService

    repository = ChatMemoryRepository(tmp_path / "quota.sqlite3", retention_days=30)
    await repository.open()
    try:
        service = QuotaService(
            BotSettings(
                allowed_group_ids="1001",
                ai_api_key="secret",
                quota_rate_limit_per_minute=1,
            ),
            repository,
        )
        # consume the single allowed slot for this scope
        assert (await service.check_admission(scope_type="group", scope_id=1001)).allowed

        calls: list[str] = []

        async def unexpected_run_auto_chat(**kwargs):
            calls.append("pipeline")
            return "不应到达管线"

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(
            ai_chat_plugin,
            "get_settings",
            lambda: BotSettings(
                allowed_group_ids="1001",
                ai_api_key="secret",
                quota_rate_limit_per_minute=1,
            ),
        )
        monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
        _patch_agent_runtime(monkeypatch, _QuotaRuntime(service))
        monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", unexpected_run_auto_chat)
        monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

        with pytest.raises(FinishCalled) as exc_info:
            await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

        assert calls == []
        assert str(exc_info.value.message).strip() == "提问太频繁了，请稍后再试。"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_ai_chat_quota_scope_binds_around_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """S4-QUOTA-03/04: while the model call runs, the quota scope is bound so
    usage accounting can attach the group without re-parsing the event."""
    from qq_bot.services.chat_memory import ChatMemoryRepository
    from qq_bot.services.quota import QuotaService, active_quota_scope

    repository = ChatMemoryRepository(tmp_path / "quota2.sqlite3", retention_days=30)
    await repository.open()
    try:
        service = QuotaService(
            BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
            repository,
        )
        seen: list[tuple[str, int] | None] = []

        async def spy_run_auto_chat(**kwargs) -> str:
            seen.append(active_quota_scope())
            await kwargs["send"]("配额内回复")
            return "配额内回复"

        async def fake_finish(message: object) -> None:
            raise FinishCalled(message)

        monkeypatch.setattr(
            ai_chat_plugin,
            "get_settings",
            lambda: BotSettings(allowed_group_ids="1001", ai_api_key="secret"),
        )
        monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: EmptyMemoryStore())
        _patch_agent_runtime(monkeypatch, _QuotaRuntime(service))
        monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", spy_run_auto_chat)
        monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)
        monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

        with pytest.raises(FinishCalled) as exc_info:
            await ai_chat_plugin.handle_ai_chat(FakeEvent("ai 你好"))  # type: ignore[arg-type]

        assert seen == [("group", 1001)]
        assert str(exc_info.value.message).strip() == "配额内回复"
    finally:
        await repository.close()


class _QuotaRuntime:
    def __init__(self, service: object):
        self._service = service

    def get_quota_service(self):
        return self._service


# ---- 自主群聊插话接线（S7-AUTO-07）----


@pytest.mark.asyncio
async def test_auto_chat_disabled_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"auto": 0}

    async def fake_run_auto_chat(**kwargs):
        calls["auto"] += 1

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 123

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=False),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert calls["auto"] == 0
    assert len(written) == 1
    assert written[0]["message_text"] == "随便聊聊"
    assert written[0]["is_ai_prompt"] is False


@pytest.mark.asyncio
async def test_auto_chat_enabled_routes_plain_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旁路直连路径（S10-HUMAN-P0 H1 后）：collect 关闭时普通消息仍直连管道。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 123

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=False,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())

    await ai_chat_plugin.handle_ai_chat(FakeEvent("小洛在吗"))  # type: ignore[arg-type]

    assert captured["raw_text"] == "小洛在吗"
    assert captured["settings"].auto_chat_enabled is True
    assert callable(captured["send"])
    assert callable(captured["quota_check"])
    assert captured["memory_store"] is not None


@pytest.mark.asyncio
async def test_auto_chat_skipped_when_memory_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"auto": 0}

    async def fake_run_auto_chat(**kwargs):
        calls["auto"] += 1

    def broken_repository():
        raise RuntimeError("no runtime")

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", broken_repository)

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert calls["auto"] == 0


@pytest.mark.asyncio
async def test_auto_chat_quota_and_send_closures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send 闭包走 replace_named_mentions + finish；quota 闭包走 runtime。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 123

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=False,
            named_mention_replacements="@小洛=2880000001",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)

    await ai_chat_plugin.handle_ai_chat(FakeEvent("来玩"))  # type: ignore[arg-type]

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    with pytest.raises(FinishCalled):
        await captured["send"]("你好，@小洛")


# ---- 收集窗口接线（S10-HUMAN-P0-04）----


@pytest.mark.asyncio
async def test_auto_chat_collect_route_stashes_hooks_and_submits_batch_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通消息不再直连 run_auto_chat：暂存 quota/send 钩子，携带
    row_id 的 BatchItem 交给收集器，并刷新 pending gauge。"""
    submitted: list[tuple[int, object]] = []

    class FakeCollector:
        async def submit(self, group_id: int, item: object) -> None:
            submitted.append((group_id, item))

        def pending_count(self) -> int:
            return len(submitted)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 777

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", lambda: FakeCollector())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert len(submitted) == 1
    group_id, item = submitted[0]
    assert group_id == 1001
    assert item.raw_text == "随便聊聊"
    assert item.sender_user_id == 2001
    assert item.row_id == 777
    assert 1001 in ai_chat_plugin._PENDING_AUTO_HOOKS
    quota_check, send, react, send_sticker, send_image, trigger_images, bot_user_id = (
        ai_chat_plugin._PENDING_AUTO_HOOKS[1001]
    )
    assert callable(quota_check)
    assert callable(send)
    assert callable(react)
    assert callable(send_sticker)
    assert callable(send_image)
    assert trigger_images == ()  # 普通文本消息无图，触发图占位为空元组
    assert bot_user_id == 2880000001  # 写回记忆/渲染 bot 行要用机器人自己的 QQ 号


@pytest.mark.asyncio
async def test_auto_chat_collector_delivery_runs_pipeline_with_batch_and_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静默到期后 _deliver 整批交给 run_auto_chat(batch=...)：复用暂存钩子、
    触发文本取批次末条，交付后钩子弹出、pending 归零。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)
        await asyncio.sleep(0)  # 保持 _deliver 可等待的真实异步形态

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 888

    settings = BotSettings(
        allowed_group_ids="1001",
        auto_chat_enabled=True,
        auto_chat_collect_quiet_seconds=0.02,
        auto_chat_collect_max_seconds=0.5,
    )
    monkeypatch.setattr(ai_chat_plugin, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(ai_chat_plugin, "_AUTO_CHAT_COLLECTOR", None)
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    for _ in range(100):
        if captured:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # 等 _run 任务完全收尾，避免悬空 task

    assert captured["raw_text"] == "随便聊聊"
    assert [it.raw_text for it in captured["batch"]] == ["随便聊聊"]
    assert captured["event"].group_id == 1001
    assert captured["settings"].auto_chat_collect_quiet_seconds == 0.02
    assert captured["memory_store"] is not None
    assert callable(captured["send"])
    assert callable(captured["quota_check"])
    assert 1001 not in ai_chat_plugin._PENDING_AUTO_HOOKS
    assert ai_chat_plugin._AUTO_CHAT_COLLECTOR.pending_count() == 0


def _auto_chat_metric(stage: str, result: str) -> float:
    return (
        REGISTRY.get_sample_value("qq_bot_auto_chat_total", {"stage": stage, "result": result})
        or 0.0
    )


@pytest.mark.asyncio
async def test_auto_chat_bypass_direct_call_emits_collect_bypass_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点名旁路直连管道前补 collect/bypass 指标（spec 观测性要求）。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 123

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=True,
            persona_name="小洛",
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    before = _auto_chat_metric("collect", "bypass")
    await ai_chat_plugin.handle_ai_chat(FakeEvent("小洛在吗"))  # type: ignore[arg-type]

    assert captured["raw_text"] == "小洛在吗"  # 旁路直连，未进收集窗口
    assert _auto_chat_metric("collect", "bypass") == before + 1


@pytest.mark.asyncio
async def test_auto_chat_collect_disabled_direct_call_emits_collect_disabled_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """collect 关闭时直连管道前补 collect/disabled 指标（spec 观测性要求）。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 123

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_collect_enabled=False,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    before = _auto_chat_metric("collect", "disabled")
    await ai_chat_plugin.handle_ai_chat(FakeEvent("随便聊聊"))  # type: ignore[arg-type]

    assert captured["raw_text"] == "随便聊聊"  # 直连管道，未进收集窗口
    assert _auto_chat_metric("collect", "disabled") == before + 1


# ---- 固定文案池化（S7-AUTO-08）----


def test_clarify_and_unavailable_messages_are_pools() -> None:
    assert len(ai_chat_plugin._AGENT_CLARIFY_MESSAGES[ai_chat_plugin.ReasonCode.CLARIFY]) >= 3
    assert len(ai_chat_plugin._UNAVAILABLE_MESSAGES) >= 3
    for pool in ai_chat_plugin._AGENT_CLARIFY_MESSAGES.values():
        assert all(isinstance(m, str) and m for m in pool)


def test_pick_variants_stays_within_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ("a", "b", "c")
    monkeypatch.setattr(ai_chat_plugin.random, "choice", lambda seq: seq[0])
    assert ai_chat_plugin._pick_variants(pool) == "a"


# ---- 二期：被 @ 路径登记与冷场调度（S7-AUTO-P2-05）----


# ---- 二期收尾：send 闭包吞掉 finish 控制流异常（S7-AUTO-P2-07）----


@pytest.mark.asyncio
async def test_auto_chat_send_hook_swallows_finished_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matcher.finish 正常完成抛 FinishedException；send 闭包必须吞掉它，
    否则调用方的后续逻辑（如 run_auto_chat 的 send/ok 指标）永远不会执行。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, send, _react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(event, settings)

    async def fake_finish(matcher, message):
        raise FinishedException()

    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    await send("x")  # 不抛出 FinishedException 即通过


# ---- 机器人自己的发言写回记忆（S12-CHATFIX-01）----


class _RecordingMemory:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def add_message(self, **kwargs):
        self.rows.append(kwargs)
        return len(self.rows)


class _FakeSendBot:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_group_msg(self, *, group_id: int, message: object) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_auto_chat_send_hook_records_self_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插话成功出站后逐段写回聊天记忆（user_id=bot 自己），后续轮次上下文
    才有「这话是我说的」——否则 @问答不认账（2026-10-26 线上实测）。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    memory = _RecordingMemory()
    _, send, _react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(
        event, settings, memory_store=memory
    )
    bot = _FakeSendBot()
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: bot)
    monkeypatch.setattr(ai_chat_plugin.random, "uniform", lambda a, b: 0.0)

    async def fake_finish(matcher, message):
        bot.sent.append(message)  # 末段走 finish 链路发出
        raise FinishedException()

    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)

    await send(["第一句", "第二句"])
    assert [str(m) for m in bot.sent] == ["第一句", "第二句"]
    assert [(r["user_id"], r["message_text"]) for r in memory.rows] == [
        (2880000001, "第一句"),
        (2880000001, "第二句"),
    ]


@pytest.mark.asyncio
async def test_auto_chat_send_hook_strips_placeholder_and_skips_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """出站保险闸：占位标记剥除；剥空段丢弃、全空整轮不发也不记账。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    memory = _RecordingMemory()
    _, send, _react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(
        event, settings, memory_store=memory
    )
    bot = _FakeSendBot()
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: bot)
    monkeypatch.setattr(ai_chat_plugin.random, "uniform", lambda a, b: 0.0)

    async def fake_finish(matcher, message):
        bot.sent.append(message)
        raise FinishedException()

    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)

    await send(["看这个[图片]", "[图片]"])
    assert [str(m) for m in bot.sent] == ["看这个"]  # 占位剥除；纯占位段丢弃
    # 记忆按原始段登记（含 [图片] 占位，与收消息存储惯例一致）；纯占位段没发出不记账
    assert [(r["user_id"], r["message_text"]) for r in memory.rows] == [
        (2880000001, "看这个[图片]")
    ]

    await send(["[图片]"])
    assert [str(m) for m in bot.sent] == ["看这个"]  # 全空整轮不发
    assert len(memory.rows) == 1


@pytest.mark.asyncio
async def test_send_image_hook_records_placeholder_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plusone 复读图成功后以 [图片] 占位行写回记忆；失败不记账。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    memory = _RecordingMemory()
    _, _send, _react, _sticker, send_image = ai_chat_plugin._build_auto_chat_hooks(
        event, settings, memory_store=memory
    )
    bot = _FakeSendBot()
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: bot)

    ok = await send_image("https://multimedia.example/a.jpg")
    assert ok is True
    assert [(r["user_id"], r["message_text"]) for r in memory.rows] == [(2880000001, "[图片]")]

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)
    assert await send_image("https://multimedia.example/b.jpg") is False
    assert len(memory.rows) == 1  # 失败不记账


# ---- 贴表情 react 闭包（S10-HUMAN-P2 H9，Fix Round 1 / Minor 5）----


@pytest.mark.asyncio
async def test_auto_chat_react_hook_returns_false_without_connected_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无已连接 OneBot v11 bot：闭包必须返回 False（由 auto_chat 服务层
    的连续失败降级逻辑处置），绝不抛异常。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, _send, react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    assert await react(42, "128") is False


@pytest.mark.asyncio
async def test_auto_chat_react_hook_calls_set_msg_emoji_like_with_protocol_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议契约钉死：成功路径调用 `call_api("set_msg_emoji_like",
    message_id=<int>, emoji_id=<str>)` 并返回 True（NapCat 参数名/类型）。"""
    recorded: list[tuple[str, dict]] = []

    class FakeOneBot:
        async def call_api(self, action: str, **params):
            recorded.append((action, params))
            return None  # 协议成功时返回值无关紧要

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, _send, react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: FakeOneBot())

    assert await react(42, "128") is True
    assert recorded == [("set_msg_emoji_like", {"message_id": 42, "emoji_id": "128"})]


@pytest.mark.asyncio
async def test_auto_chat_react_hook_swallows_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议不支持/超时/断连：call_api 抛异常时闭包返回 False，不向上抛。"""

    class ExplodingOneBot:
        async def call_api(self, action: str, **params):
            raise RuntimeError("protocol unsupported")

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _, _send, react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(event, settings)
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: ExplodingOneBot())

    assert await react(42, "128") is False


# ---- 二期修复：多段连发（S7-AUTO-P2-10）----


@pytest.mark.asyncio
async def test_send_multi_parts_bursts_then_finishes_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOneBot:
        def __init__(self):
            self.sent: list[tuple[int, str]] = []

        async def send_group_msg(self, *, group_id, message):
            self.sent.append((group_id, str(message)))

    fake_bot = FakeOneBot()

    async def fake_finish(matcher, message):
        raise FinishCalled(message)

    event = FakeEvent("随便聊聊")
    settings = BotSettings(
        allowed_group_ids="1001",
        ai_api_key="secret",
        named_mention_replacements="@小洛=2880000001",
    )
    _, send, _react, _sticker, _image = ai_chat_plugin._build_auto_chat_hooks(event, settings)

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: fake_bot)
    monkeypatch.setattr(ai_chat_plugin, "finish_with_send_errors_logged", fake_finish)
    monkeypatch.setattr(ai_chat_plugin.ai_chat, "finish", fake_finish)
    monkeypatch.setattr(ai_chat_plugin.random, "uniform", lambda a, b: 0.0)

    with pytest.raises(FinishCalled) as exc_info:
        await send(["哈哈", "玩啊", "你好，@小洛"])

    # 前面的碎片走裸 API 连发；最后一段走 finish
    assert fake_bot.sent == [(1001, "哈哈"), (1001, "玩啊")]
    assert "你好，" in str(exc_info.value.message)


# ---- 引用接线（S10-HUMAN-P0-06）：事件层渲染 + quoted 必回 ----


class FakeReplySegment:
    type = "reply"

    def __init__(self, data: dict):
        self.data = data


@pytest.mark.asyncio
async def test_quoted_self_message_bypasses_collector_and_replies_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用机器人自己的消息（H4 quoted 必回）：设 quoted_self 旁路直连管道，
    不进收集窗口，也不渲染引用标记（存储与管道输入都不带 u{id}）。"""
    captured: dict = {}

    async def fake_run_auto_chat(**kwargs):
        captured.update(kwargs)

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 321

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())

    def _no_collector():
        raise AssertionError("quoted-self message must bypass the collector")

    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", _no_collector)
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    before = _auto_chat_metric("collect", "bypass")
    event = FakeEvent(
        "随便聊聊",
        segments=[FakeReplySegment({"id": 7, "qq": 2880000001, "text": "机器人之前的话"})],
    )
    await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert captured["raw_text"] == "随便聊聊"  # self 引用不渲染标记
    assert _auto_chat_metric("collect", "bypass") == before + 1
    assert written[0]["message_text"] == "随便聊聊"  # 存储同样无标记


@pytest.mark.asyncio
async def test_quoted_other_user_renders_marker_into_pipeline_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用他人（H4 事件层渲染）：reply 段扩展字段解析后渲染 u{id} 标记进
    原始文本（只进本地存储/管道输入），且不触发 quoted 旁路。"""
    submitted: list[object] = []

    class FakeCollector:
        async def submit(self, group_id: int, item: object) -> None:
            submitted.append(item)

        def pending_count(self) -> int:
            return len(submitted)

    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 654

    auto_calls: list = []

    async def fake_run_auto_chat(**kwargs):
        auto_calls.append(kwargs)

    monkeypatch.setattr(ai_chat_plugin, "run_auto_chat", fake_run_auto_chat)
    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(allowed_group_ids="1001", auto_chat_enabled=True),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", lambda: FakeCollector())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    event = FakeEvent("冲", segments=[FakeReplySegment({"id": 8, "qq": 12345, "text": "上分"})])
    await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert len(submitted) == 1
    assert submitted[0].raw_text == "回复 [引用 u12345:上分]：冲"
    assert auto_calls == []  # 普通引用不走旁路
    # Fix Round 1：存储侧同样带标记（非 self 方向的入库文本可回查引用上下文）
    assert written[0]["message_text"] == "回复 [引用 u12345:上分]：冲"


@pytest.mark.asyncio
async def test_quote_reply_disabled_ignores_reply_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭：reply 段被完全忽略，引用自己也不旁路（接线前行为）。"""
    submitted: list[object] = []

    class FakeCollector:
        async def submit(self, group_id: int, item: object) -> None:
            submitted.append(item)

        def pending_count(self) -> int:
            return len(submitted)

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            return 999

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            auto_chat_enabled=True,
            auto_chat_quote_reply_enabled=False,
        ),
    )
    monkeypatch.setattr(ai_chat_plugin, "get_chat_repository", lambda: RecordingStore())
    monkeypatch.setattr(ai_chat_plugin, "_get_auto_chat_collector", lambda: FakeCollector())
    monkeypatch.setattr(ai_chat_plugin, "_PENDING_AUTO_HOOKS", {})

    event = FakeEvent(
        "随便聊聊",
        segments=[FakeReplySegment({"id": 9, "qq": 2880000001, "text": "机器人之前的话"})],
    )
    await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert len(submitted) == 1
    assert submitted[0].raw_text == "随便聊聊"  # 未渲染、未旁路


# ---- Fix Round 1（S10-HUMAN-P0-06）：标记只进存储/auto 管道，命令/提问
# ---- 提取继续用未加标记的 raw_text ----


@pytest.mark.asyncio
async def test_quoted_reply_with_ai_prefix_still_explicit_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix Round 1 回归 A：reply + "ai xxx" 前缀仍识别为显式 AI 命令；
    提示词不含 u{id} 令牌与 [引用 标记（标记只进存储）。"""
    captured: dict = {}
    written: list[dict] = []

    class RecordingStore(EmptyMemoryStore):
        async def add_message(self, **kwargs) -> int:
            written.append(kwargs)
            return 555

    monkeypatch.setattr(
        ai_chat_plugin,
        "get_settings",
        lambda: BotSettings(
            allowed_group_ids="1001",
            ai_api_key="secret",
            auto_chat_enabled=False,
            auto_chat_quote_reply_enabled=True,
        ),
    )
    captured = await _run_addressed(
        monkeypatch, FakeEvent("ai 帮我看看这个"), store=RecordingStore()
    )
    event = FakeEvent(
        "ai 帮我看看这个",
        segments=[FakeReplySegment({"id": 11, "qq": 12345, "text": "上分"})],
    )
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    prompt = captured["raw_text"]
    # S11-QUOTE-02：引用内容按昵称别名渲染进统一管线触发文本（模型不再盲答
    # 引用），u{id} 令牌与 QQ 号依旧绝不出现——隐私红线不变，只是换了渲染层。
    assert prompt == "回复 [引用 用户A:上分]：帮我看看这个"
    assert "u12345" not in prompt
    assert "12345" not in prompt
    # 存储侧仍带标记：引用上下文保留给本地存储与后续渲染
    assert written[0]["message_text"] == "回复 [引用 u12345:上分]：ai 帮我看看这个"


@pytest.mark.asyncio
async def test_quoted_reply_addressed_to_bot_prompt_has_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix Round 1 回归 B（S11-QUOTE-02 修订）：reply + @bot（to_me）走显式
    应答路径；提示词现在携带昵称渲染的引用内容，但 u{id} 令牌与原始 QQ 号
    绝不出现（QQ 号经模型回显进群聊即隐私边界破防）。"""
    captured = await _run_addressed(monkeypatch, FakeEvent("帮我看看这个", to_me=True))

    event = FakeEvent(
        "帮我看看这个",
        segments=[FakeReplySegment({"id": 12, "qq": 12345, "text": "上分"})],
        to_me=True,
    )
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    prompt = captured["raw_text"]
    assert prompt == "回复 [引用 用户A:上分]：帮我看看这个"
    assert "u12345" not in prompt
    assert "12345" not in prompt


# ---------------------------------------------------------------------------
# 表情包发送钩子（S10-HUMAN-P2 H10）
# ---------------------------------------------------------------------------


def _sticker() -> Sticker:
    return Sticker(id="cat_laugh", path=Path("/tmp/cat_laugh.png"), description="猫大笑")


@pytest.mark.asyncio
async def test_send_sticker_hook_returns_false_without_connected_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无已连接 bot：闭包返回 False（服务层连续失败降级逻辑处置）。"""
    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _quota, _send, _react, send_sticker, _image = ai_chat_plugin._build_auto_chat_hooks(
        event, settings
    )
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)

    assert await send_sticker(_sticker()) is False


@pytest.mark.asyncio
async def test_send_sticker_hook_sends_image_segment_to_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功路径：库条目的本地绝对路径作为 image 段发到本群（与宠物卡同一
    `file:///` 惯例），返回 True。"""
    recorded: dict = {}

    class FakeOneBot:
        async def send_group_msg(self, *, group_id: int, message):
            recorded["group_id"] = group_id
            recorded["message"] = message
            return None

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _quota, _send, _react, send_sticker, _image = ai_chat_plugin._build_auto_chat_hooks(
        event, settings
    )
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: FakeOneBot())

    assert await send_sticker(_sticker()) is True
    assert recorded["group_id"] == 1001
    segment = recorded["message"]
    assert segment.type == "image"
    assert segment.data["file"].startswith("file:///")


@pytest.mark.asyncio
async def test_send_sticker_hook_swallows_send_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议拒绝/文件不可读：send_group_msg 抛异常时闭包返回 False，不向上抛。"""

    class ExplodingOneBot:
        async def send_group_msg(self, *, group_id: int, message):
            raise RuntimeError("protocol unsupported")

    event = FakeEvent("随便聊聊")
    settings = BotSettings(allowed_group_ids="1001", ai_api_key="secret")
    _quota, _send, _react, send_sticker, _image = ai_chat_plugin._build_auto_chat_hooks(
        event, settings
    )
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: ExplodingOneBot())

    assert await send_sticker(_sticker()) is False


# ---- 视觉（S11-VISION-01）：@问答读图 / 回复发图发表情 ----


class FakeImageSegment:
    type = "image"

    def __init__(self, url: str):
        self.data = {"url": url}


def _sticker_library(tmp_path: Path):
    from qq_bot.services.sticker_library import StickerLibrary

    sticker_path = tmp_path / "dog.png"
    sticker_path.write_bytes(b"png")
    return StickerLibrary(stickers=(Sticker(id="dog", path=sticker_path, description="一只狗"),))


@pytest.mark.asyncio
async def test_ai_chat_passes_images_and_attachment_placeholder_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@ + 图片：占位文本作触发，图片源随 trigger_images 进统一管线
    （随请求发给模型的封顶与归属说明在服务层注入）。"""
    captured = await _run_addressed(monkeypatch, FakeEvent("", to_me=True))

    event = FakeEvent(
        "",
        segments=[
            FakeImageSegment("https://multimedia.example/a.jpg"),
            FakeImageSegment("https://multimedia.example/b.jpg"),
        ],
        to_me=True,
    )
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert captured["raw_text"] == "[图片]"
    assert captured["trigger_images"] == (
        "https://multimedia.example/a.jpg",
        "https://multimedia.example/b.jpg",
    )


@pytest.mark.asyncio
async def test_ai_chat_face_segment_becomes_tag_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QQ 经典表情渲染成 [表情:名字] 占位随触发文本进统一管线。"""
    captured = await _run_addressed(monkeypatch, FakeEvent("", to_me=True))

    face_segment = SimpleNamespace(type="face", data={"id": "21", "text": "白眼"})
    event = FakeEvent("", [FakeTextSegment(""), face_segment], to_me=True)
    with pytest.raises(FinishCalled):
        await ai_chat_plugin.handle_ai_chat(event)  # type: ignore[arg-type]

    assert "[表情:白眼]" in captured["raw_text"]
    assert captured["trigger_images"] == ()


def test_build_outgoing_reply_plain_text_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = BotSettings()
    monkeypatch.setattr(ai_chat_plugin, "_deliver_sticker_library", lambda: None)
    message = ai_chat_plugin._build_outgoing_reply("就一句话", settings=settings)
    assert isinstance(message, Message)
    assert message.extract_plain_text() == "就一句话"


def test_build_outgoing_reply_sticker_marker_becomes_image_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("哈哈[发图:dog]", settings=settings)

    assert isinstance(message, Message)
    assert [seg.type for seg in message] == ["text", "image"]
    assert message[0].data["text"] == "哈哈"
    assert message[1].data["file"].startswith("file:///")
    assert message[1].data["file"].endswith("dog.png")


def test_build_outgoing_reply_face_marker_becomes_face_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("[发脸:呲牙]没了", settings=settings)

    assert isinstance(message, Message)
    assert [seg.type for seg in message] == ["face", "text"]
    assert message[0].data["id"] == "13"
    assert message[1].data["text"] == "没了"


def test_build_outgoing_reply_unknown_markers_are_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """库缺条目/白名单外表情名：标记整段丢弃，绝不出现在群消息里。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=True)

    message = ai_chat_plugin._build_outgoing_reply("哈哈[发图:cat][发脸:菜刀]", settings=settings)

    assert message.extract_plain_text() == "哈哈"
    assert all(seg.type != "image" and seg.type != "face" for seg in message)


def test_build_outgoing_reply_disabled_channel_still_strips_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """开关关掉后模型漏出的标记也不许原文进群（剥掉，只发文本）。"""
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    settings = BotSettings(ai_qa_sticker_enabled=False)

    message = ai_chat_plugin._build_outgoing_reply("哈哈[发图:dog]", settings=settings)

    assert message.extract_plain_text() == "哈哈"
    assert all(seg.type != "image" for seg in message)


def test_qa_sticker_hint_face_channel_survives_empty_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QQ 小表情不依赖表情包库：库为空时 [发脸] 提示必须在，表情包候选块缺席
    （2026-10-25 线上教训：服务器没建库，整块提示被吞，模型发不了表情）。"""
    monkeypatch.setattr(ai_chat_plugin, "_deliver_sticker_library", lambda: None)
    hint = ai_chat_plugin._qa_sticker_hint(BotSettings(ai_qa_sticker_enabled=True))
    assert "[发脸:名字]" in hint
    assert "呲牙" in hint
    assert "表情包候选" not in hint
    assert "[发图:ID]" not in hint


def test_qa_sticker_hint_lists_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ai_chat_plugin, "_deliver_sticker_library", lambda: _sticker_library(tmp_path)
    )
    hint = ai_chat_plugin._qa_sticker_hint(BotSettings(ai_qa_sticker_enabled=True))
    assert "[发图:dog]" in hint or "发图" in hint
    assert "一只狗" in hint
    assert "呲牙" in hint


# ---- 回捞图归属（S11-VISION-01：模型看得到图但不知道是谁发的）----


# ---- @问答引用前缀 + get_msg 回查（S11-QUOTE-01/02）----


def test_qa_quote_prefix_renders_nickname_no_qq_digits() -> None:
    """引用内容按上下文同一套昵称渲染进 QA prompt，QQ 号绝不出现。"""
    ref = quote_reference.QuoteReference(ref_user_id=2002, ref_text="上一把太离谱了")
    row = ChatMemoryRow(
        id=1,
        group_id=1001,
        user_id=2002,
        message_text="上一把太离谱了",
        created_at="",
        is_ai_prompt=False,
        ai_reply="",
    )
    prefix = ai_chat_plugin._qa_quote_prefix(ref, [row], {2002: "大肥鱼"})
    assert prefix == "回复 [引用 大肥鱼:上一把太离谱了]："
    assert "2002" not in prefix


def test_qa_quote_prefix_letter_fallback_aligned_with_context() -> None:
    """无昵称回落字母：被引用者排在上下文行之后，字母序与上下文渲染一致不串位。"""
    ref = quote_reference.QuoteReference(ref_user_id=3000, ref_text="原文")
    row = ChatMemoryRow(
        id=1,
        group_id=1001,
        user_id=2001,
        message_text="占位",
        created_at="",
        is_ai_prompt=False,
        ai_reply="",
    )
    prefix = ai_chat_plugin._qa_quote_prefix(ref, [row], None)
    assert prefix.startswith("回复 [引用 用户B:原文]")


def test_qa_quote_prefix_empty_text_degrades_to_note() -> None:
    """引用纯图/取不到文字：降级为说明句，模型不编引用内容。"""
    ref = quote_reference.QuoteReference(ref_user_id=2002, ref_text="")
    prefix = ai_chat_plugin._qa_quote_prefix(ref, [], {2002: "大肥鱼"})
    assert "大肥鱼" in prefix
    assert "不要编造引用内容" in prefix


def test_qa_quote_prefix_none_ref_is_empty() -> None:
    assert ai_chat_plugin._qa_quote_prefix(None, [], None) == ""


def test_plain_text_from_onebot_message_segment_array() -> None:
    raw = [
        {"type": "text", "data": {"text": "上半句"}},
        {"type": "face", "data": {"id": "1"}},
        {"type": "text", "data": {"text": "下半句"}},
    ]
    assert ai_chat_plugin._plain_text_from_onebot_message(raw) == "上半句下半句"


def test_plain_text_from_onebot_message_cq_string() -> None:
    assert ai_chat_plugin._plain_text_from_onebot_message("[CQ:image,file=x.jpg]看这个") == "看这个"


async def test_quote_fetcher_calls_get_msg(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBot:
        async def call_api(self, api: str, **kwargs):
            assert api == "get_msg"
            return {
                "message_id": kwargs["message_id"],
                "sender": {"user_id": 2002},
                "message": [{"type": "text", "data": {"text": "被引用的原话"}}],
            }

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: FakeBot())
    fetch = ai_chat_plugin._quote_message_fetcher()
    assert fetch is not None
    assert await fetch(77) == (2002, "被引用的原话")


async def test_quote_fetcher_none_without_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: None)
    assert ai_chat_plugin._quote_message_fetcher() is None


async def test_quote_fetcher_swallows_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBot:
        async def call_api(self, api: str, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(ai_chat_plugin, "_connected_onebot_bot", lambda: FakeBot())
    fetch = ai_chat_plugin._quote_message_fetcher()
    assert fetch is not None
    assert await fetch(77) is None


def test_split_qa_reply_newlines_to_bubbles() -> None:
    assert ai_chat_plugin._split_qa_reply("烧啥烧\n额度就是留着这时候用的，不烧白不烧") == [
        "烧啥烧",
        "额度就是留着这时候用的，不烧白不烧",
    ]


def test_split_qa_reply_single_line_unchanged() -> None:
    assert ai_chat_plugin._split_qa_reply("就一句话") == ["就一句话"]


def test_split_qa_reply_caps_at_three_parts() -> None:
    reply = "一\n二\n三\n四\n五"
    parts = ai_chat_plugin._split_qa_reply(reply)
    assert len(parts) == 3
    assert parts[0] == "一"
    assert "\n" in parts[-1]  # 溢出并回最后一段


def test_split_qa_reply_drops_blank_lines() -> None:
    assert ai_chat_plugin._split_qa_reply("\n 甲 \n\n 乙 \n") == ["甲", "乙"]


def sent_fragments_guard() -> list:
    return []
