from datetime import UTC, datetime, timedelta
import asyncio
import random
from types import SimpleNamespace
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from nonebot import get_bots, logger, on_message
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException

from qq_bot.agent.evidence import EvidenceStore, render_answer
from qq_bot.agent.models import AgentRequest, AgentScope, SafeFailure
from qq_bot.agent.router import ReasonCode, route_request
from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import LogContext, current_request_id, new_request_id
from qq_bot.observability.tracing import get_tracer
from qq_bot.runtime import (
    RuntimeStateError,
    get_chat_repository,
    get_http_client,
    get_runtime,
)
from qq_bot.services.ai_client import AIReplyError, request_ai_reply
from qq_bot.services.auto_chat import (
    BatchItem,
    TriggerCategory,
    activity_weight,
    classify_trigger,
    run_auto_chat,
    schedule_cold_check,
    shared_state,
    static_prefilter,
    typing_intervals,
)
from qq_bot.services.auto_chat_collector import AutoChatCollector, CollectedBatch
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.persona import load_persona
from qq_bot.services.memory_prompt import (
    extract_at_user_ids,
    extract_at_user_ids_before_separator,
    format_chat_context,
    parse_memory_reference,
)
from qq_bot.services.message_formatting import replace_named_mentions
from qq_bot.services.nicknames import NicknameDirectory
from qq_bot.services.onebot_send import finish_with_send_errors_logged
from qq_bot.services.sticker_library import Sticker, StickerStore
from qq_bot.services.prompt import extract_ai_prompt
from qq_bot.services.quota import quota_scope
from qq_bot.services.reliability import classify_exception
from qq_bot.services.search import (
    SearchError,
    format_search_context,
    prompt_needs_search,
    search_web,
)


ai_chat = on_message(priority=20, block=False)

# Stable clarification replies (S2-AGENT-09): no model call, no draft, and
# never a reason for the user to rephrase with private details. Variant
# pools (S7-AUTO-08) remove the "always the same sentence" machine feel;
# every variant keeps the original meaning.
_AGENT_CLARIFY_MESSAGES: dict[ReasonCode, tuple[str, ...]] = {
    ReasonCode.CLARIFY: (
        "没太明白你的意思，能说得更具体一点吗？",
        "这个我没太看懂，再说细一点？",
        "信息有点少，你想问的是什么呢？",
    ),
    ReasonCode.CAPABILITY_ERROR: (
        "这个功能还没有配置好，先换个问题试试吧。",
        "这个我现在答不了，换个问题试试。",
    ),
    ReasonCode.RULE_FALLBACK: (
        "暂时无法处理这个问题，请稍后再试。",
        "这个问题我现在处理不了，稍后再试试吧。",
    ),
}
_AGENT_DEFAULT_CLARIFY = "没太明白你的意思，能说得更具体一点吗？"
_UNAVAILABLE_MESSAGES = (
    "AI 服务暂时不可用，请稍后再试。",
    "AI 这边临时出问题了，等会儿再问我。",
    "现在答不了，过一会儿再试试。",
)
_QUOTA_RATE_MESSAGE = "提问太频繁了，请稍后再试。"
_QUOTA_COST_MESSAGE = "今日 AI 用量已达预算上限，请明天再试。"


def _pick_variants(pool: tuple[str, ...]) -> str:
    return random.choice(pool)


def _quota_message(reason: str) -> str:
    """Stable user-facing denial text (S4-QUOTA-02/03): never leaks limits,
    costs or identifiers."""
    if reason == "cost":
        return _QUOTA_COST_MESSAGE
    return _QUOTA_RATE_MESSAGE


@ai_chat.handle()
async def handle_ai_chat(event: GroupMessageEvent) -> None:
    # S4-LOG-01: one request_id per processed message, carried through the
    # whole chain via contextvars (logs, spans and metrics share this source).
    request_id = new_request_id()
    with LogContext(request_id=request_id, group_id=event.group_id):
        await _handle_ai_chat(event)


async def _handle_ai_chat(event: GroupMessageEvent) -> None:
    # Span tree (S4-TRACE-01): msg.receive covers reception/parsing; the
    # end-to-end msg.total spans entry to send completion (or safe failure).
    tracer = get_tracer()
    trace_id = current_request_id()
    receive_span = tracer.start_span("msg.receive", trace_id=trace_id)
    total_span = tracer.start_span("msg.total", trace_id=trace_id)
    try:
        try:
            settings = get_settings()
            if not settings.group_allowed(event.group_id):
                return
            if event.user_id in settings.ai_ignored_user_id_list:
                return

            memory_store: ChatMemoryRepository | None = None
            http_client = None
            try:
                memory_store = get_chat_repository()
                http_client = get_http_client()
            except Exception:
                logger.exception(
                    "Runtime resources unavailable; continuing without memory or shared client"
                )

            raw_text = event.get_message().extract_plain_text().strip()

            # 群兴趣度计分（S10-HUMAN-P2 H7）：放在 ignored 检查之后、prompt 分支
            # 之前，普通消息与被 @ 问答两条路径共用同一入口，每条消息只记一次。
            # 权重来自纯函数 activity_weight（含"你/你们"/疑问加权），boost 与
            # 半衰期取配置项使部署者可调；state 本身不依赖 settings。显式传
            # half_life 使 note 前先折叠存量（Final Fix：不折叠则活跃消息流中
            # 衰减永不生效，兴趣度退化为纯累加器）。不计消息内容，仅浮点热度。
            shared_state().note_activity(
                event.group_id,
                weight=activity_weight(raw_text, settings=settings),
                boost=settings.auto_chat_interest_boost,
                half_life=settings.auto_chat_interest_half_life,
            )

            # 引用接线（S10-HUMAN-P0-06 H4 + Fix Round 1）：先登记本条消息供
            # 后续引用回查，再解析 reply 段。引用自己 → quoted_self 必回旁路
            # （不渲染标记）；引用他人 → 渲染 u{id} 标记进 stored_text，只有
            # 本地 chat 存储与 auto 管道吃标记文本（渲染层由 memory_prompt
            # 替换为匿名别名，绝不出现在日志/指标/发送文本）。命令/提问提取
            # 继续用未加标记的 raw_text：reply + "ai " 前缀仍是显式命令，被 @
            # 时提示词不携带 u{id}（防止 QQ 号经模型回显进群聊）。
            quoted_self = False
            quoted_ref = None
            stored_text = raw_text
            if settings.auto_chat_quote_reply_enabled:
                from qq_bot.services.quote_reference import (
                    parse_reply_reference,
                    render_quoted_raw_text,
                    shared_quote_lru,
                )

                lru = shared_quote_lru()
                lru.remember(event.message_id, event.user_id, raw_text)
                quoted_ref = parse_reply_reference(
                    event.get_message(), self_id=event.self_id, lru=lru
                )
                if quoted_ref is not None and quoted_ref.ref_user_id == int(event.self_id):
                    quoted_self = True
                    quoted_ref = None  # self 引用必回，但不渲染标记
                if quoted_ref is not None:
                    stored_text = render_quoted_raw_text(raw_text, quoted_ref)

            prompt = extract_ai_prompt(raw_text, prefix=settings.ai_prefix)

            mentioned_self = _mentions_self(event)
            addressed_to_bot = event.is_tome() or mentioned_self
            if prompt is None and addressed_to_bot:
                prompt = raw_text
                if mentioned_self:
                    prompt = _strip_leading_self_mention_text(event).strip()

            is_ai_prompt = prompt is not None or addressed_to_bot
            metrics.MESSAGES.labels("ai_prompt" if is_ai_prompt else "plain").inc()
            if prompt is None:
                if memory_store is not None:
                    try:
                        stored_row_id = await memory_store.add_message(
                            group_id=event.group_id,
                            user_id=event.user_id,
                            message_text=stored_text,
                            is_ai_prompt=is_ai_prompt,
                        )
                    except Exception as exc:
                        record_error("memory", classify_exception(exc).category.value)
                        logger.exception(
                            "Chat memory write failed; continuing without storing message"
                        )
                    else:
                        if settings.auto_chat_enabled:
                            await _maybe_auto_reply(
                                event,
                                raw_text=stored_text,
                                settings=settings,
                                memory_store=memory_store,
                                http_client=http_client,
                                row_id=stored_row_id,
                                quoted_self=quoted_self,
                            )
                return

            if not prompt:
                await finish_with_send_errors_logged(
                    ai_chat,
                    f"请在 {settings.ai_prefix} 后面输入要问的问题。",
                )

            if not settings.has_ai_config():
                await finish_with_send_errors_logged(ai_chat, "AI 功能还没有配置 API Key。")

            mentioned_user_ids = _without_self_mentions(
                extract_at_user_ids_before_separator(event.get_message()),
                event,
            )
            memory_reference = parse_memory_reference(
                prompt,
                mentioned_user_ids=mentioned_user_ids,
            )
            prompt = memory_reference.question

            if not prompt:
                await finish_with_send_errors_logged(ai_chat, "请输入要问的问题")

            # Quota admission (S4-QUOTA-02/03): gate both agent and legacy
            # paths at the entry; explicit commands never route through here.
            quota_service = None
            try:
                quota_service = get_runtime().get_quota_service()
            except RuntimeStateError:
                pass
            if quota_service is not None:
                decision = await quota_service.check_admission(
                    scope_type="group", scope_id=event.group_id
                )
                if not decision.allowed:
                    metrics.QUOTA_DENIED.labels("group", decision.reason).inc()
                    await finish_with_send_errors_logged(ai_chat, _quota_message(decision.reason))
        finally:
            tracer.end_span(receive_span)

        chat_context = ""
        if memory_store is not None and not settings.agent_enabled:
            memory_span = tracer.start_span("memory.retrieve", trace_id=trace_id)
            try:
                limit = min(
                    memory_reference.limit or settings.chat_memory_default_turns,
                    settings.chat_memory_max_results,
                )
                if memory_reference.user_id is not None or memory_reference.keyword:
                    rows = await memory_store.search_messages(
                        group_id=event.group_id,
                        user_id=memory_reference.user_id,
                        keyword=memory_reference.keyword,
                        limit=limit,
                    )
                else:
                    rows = await memory_store.recent_group_messages(
                        group_id=event.group_id, limit=limit
                    )
                chat_context = format_chat_context(rows)
            except Exception:
                tracer.end_span(memory_span, status="error")
                logger.exception("Chat memory read failed; continuing without chat context")
            else:
                tracer.end_span(memory_span)

        memory_message_id: int | None = None
        if memory_store is not None:
            try:
                memory_message_id = await memory_store.add_message(
                    group_id=event.group_id,
                    user_id=event.user_id,
                    message_text=stored_text,
                    is_ai_prompt=is_ai_prompt,
                )
            except Exception as exc:
                record_error("memory", classify_exception(exc).category.value)
                logger.exception("Chat memory write failed; continuing without storing message")

        roco_context = ""
        if settings.roco_enabled:
            knowledge_span = tracer.start_span("knowledge.lookup", trace_id=trace_id)
            try:
                from qq_bot.services.roco_knowledge import build_roco_context

                roco_context = build_roco_context(prompt)
            except Exception as exc:
                category = classify_exception(exc).category.value
                tracer.end_span(knowledge_span, status="error", category=category)
                record_error("knowledge", category)
                logger.exception("Roco knowledge lookup failed; continuing without Roco context")
                roco_context = ""
            else:
                tracer.end_span(knowledge_span)

        search_context = ""
        needs_search = prompt_needs_search(prompt)
        if needs_search and not settings.has_search_config():
            await finish_with_send_errors_logged(
                ai_chat,
                "这个问题需要联网搜索才能可靠回答，但搜索功能还没有配置。",
            )

        if not settings.agent_enabled and needs_search:
            try:
                search_results = await search_web(prompt, settings=settings, client=http_client)
            except SearchError:
                logger.exception("Web search failed for current-event prompt")
                await finish_with_send_errors_logged(
                    ai_chat, "联网搜索失败了，先不乱编；稍后再问我试试。"
                )
            else:
                if search_results:
                    search_context = format_search_context(search_results)
                else:
                    await finish_with_send_errors_logged(
                        ai_chat, "联网搜索没有找到可靠结果，先不乱编。"
                    )

        try:
            if settings.agent_enabled:
                with quota_scope("group", event.group_id):
                    agent_reply = await _handle_agent_chat(event, prompt, settings, memory_store)
                if agent_reply is None:
                    return
                reply = agent_reply
            else:
                with quota_scope("group", event.group_id):
                    reply = await request_ai_reply(
                        prompt,
                        settings=settings,
                        client=http_client,
                        search_context=search_context,
                        chat_context=chat_context,
                        roco_context=roco_context,
                    )
        except AIReplyError:
            await finish_with_send_errors_logged(ai_chat, _pick_variants(_UNAVAILABLE_MESSAGES))

        if memory_message_id is not None:
            try:
                await memory_store.update_ai_reply(memory_message_id, reply)
            except Exception:
                logger.exception("Chat memory reply update failed")

        # 被 @ 问答路径不贴表情也不发表情包（用户在直接对话）：quota/send 用得上
        _quota_check, send, _unused_react, _unused_sticker = _build_auto_chat_hooks(event, settings)
        exit_flag = shared_state().note_reply(event.group_id, settings=settings)
        if exit_flag:
            metrics.AUTO_CHAT.labels("prefilter", "hot_exit_limit").inc()
        if memory_store is not None:
            schedule_cold_check(
                group_id=event.group_id,
                bot_reply_text=reply,
                bot_reply_time=datetime.now(UTC),
                settings=settings,
                memory_store=memory_store,
                send=send,
                quota_check=_quota_check,
                client=http_client,
            )

        await finish_with_send_errors_logged(
            ai_chat, replace_named_mentions(reply, settings.named_mention_replacement_map)
        )
    finally:
        tracer.end_span(total_span)


async def _handle_agent_chat(
    event: GroupMessageEvent,
    prompt: str,
    settings: BotSettings,
    memory_store: ChatMemoryRepository | None,
) -> str | None:
    """Stage-2 agent path (S2-AGENT-09): router -> orchestrator -> renderer.
    Returns the reply text, or None when the reply was already sent
    (clarification / SafeFailure / runtime unavailable). No draft, prompt,
    group or user identifiers are ever logged (S2-AGENT-08)."""
    try:
        runtime = get_runtime()
        orchestrator = runtime.get_agent_orchestrator()
        gateway = runtime.get_model_gateway()
    except RuntimeStateError:
        logger.exception("Agent stack unavailable; refusing to fabricate a reply")
        await finish_with_send_errors_logged(ai_chat, _pick_variants(_UNAVAILABLE_MESSAGES))
        return None

    scope = AgentScope(
        group_id=str(event.group_id),
        user_id=str(event.user_id),
        can_use_chat_memory=memory_store is not None,
    )
    tracer = get_tracer()
    route_span = tracer.start_span("route.classify", trace_id=current_request_id())
    try:
        route, _trace = await route_request(
            prompt,
            settings=settings,
            gateway=gateway,
            can_use_chat_memory=memory_store is not None,
        )
    except Exception:
        tracer.end_span(route_span, status="error")
        raise
    tracer.end_span(route_span)
    metrics.ROUTES.labels(route.primary_route.value, route.reason_code.value).inc()
    if route.needs_clarification:
        # S2-AGENT-09 + Scenario E: clarification/capability gaps answer
        # directly; no model call, no tools.
        await finish_with_send_errors_logged(
            ai_chat,
            _pick_variants(
                _AGENT_CLARIFY_MESSAGES.get(route.reason_code, (_AGENT_DEFAULT_CLARIFY,))
            ),
        )
        return None

    request = AgentRequest(
        prompt=prompt,
        scope=scope,
        route=route,
        deadline=datetime.now(UTC) + timedelta(seconds=settings.agent_deadline_seconds),
    )
    outcome = await orchestrator.run(request)
    if isinstance(outcome, SafeFailure):
        await finish_with_send_errors_logged(ai_chat, outcome.message)
        return None
    store = orchestrator.last_store or EvidenceStore()
    return render_answer(outcome, store)


def _connected_onebot_bot() -> OneBotV11Bot | None:
    return next(
        (bot for bot in get_bots().values() if isinstance(bot, OneBotV11Bot)),
        None,
    )


def _build_auto_chat_hooks(
    event: GroupMessageEvent, settings: BotSettings
) -> tuple[
    Callable[[], Awaitable[bool]],
    Callable[[str], Awaitable[None]],
    Callable[[int, str], Awaitable[bool]],
    Callable[[Sticker], Awaitable[bool]],
]:
    """quota 准入、发送、贴表情与发表情包闭包（自主插话与被 @ 路径共用）。
    react（S10-HUMAN-P2 H9）：对触发消息贴 OneBot v11 `set_msg_emoji_like`，
    emoji_id 由决策门白名单过滤后传入；任何失败返回 False（由 auto_chat 的
    连续失败降级逻辑处置），成功 True。
    send_sticker（S10-HUMAN-P2 H10）：把库中条目对应的本地图片作为 image 段
    发到本群；无 bot 连接/发送失败一律 False（降级与限额记账在服务层）。"""

    async def quota_check() -> bool:
        try:
            quota_service = get_runtime().get_quota_service()
        except RuntimeStateError:
            return True
        decision = await quota_service.check_admission(scope_type="group", scope_id=event.group_id)
        if not decision.allowed:
            metrics.QUOTA_DENIED.labels("group", decision.reason).inc()
        return decision.allowed

    async def send(text: str | Sequence[str]) -> None:
        """多段发送（S7-AUTO-P2-10）：前面的碎片走裸 API 连发（带打字节奏
        的间隔，失败静默——碎片是装饰性的），最后一段走 finish 的完整
        重试/熔断链路。"""
        parts = [text] if isinstance(text, str) else [p for p in (p.strip() for p in text) if p]
        if not parts:
            return
        try:
            bot = _connected_onebot_bot()
            if bot is None:
                await finish_with_send_errors_logged(
                    ai_chat,
                    replace_named_mentions(
                        "，".join(parts), settings.named_mention_replacement_map
                    ),
                )
                return
            if settings.auto_chat_typing_enabled:
                intervals = typing_intervals(
                    parts,
                    base=settings.auto_chat_typing_base_seconds,
                    per_char=settings.auto_chat_typing_per_char,
                    max_seconds=settings.auto_chat_typing_max_seconds,
                    rng=random.random,
                )
            else:
                intervals = [random.uniform(0.6, 1.4) for _ in parts[:-1]]
            for index, part in enumerate(parts[:-1]):
                try:
                    await bot.send_group_msg(
                        group_id=event.group_id,
                        message=replace_named_mentions(
                            part, settings.named_mention_replacement_map
                        ),
                    )
                except Exception:
                    pass  # 插话碎片丢失可接受；也不把消息内容写进日志（隐私白名单）
                await asyncio.sleep(intervals[index])
            await finish_with_send_errors_logged(
                ai_chat,
                replace_named_mentions(parts[-1], settings.named_mention_replacement_map),
            )
        except FinishedException:
            pass  # matcher.finish 正常完成的控制流异常；吞掉让 ok 指标可执行

    async def react(message_id: int, emoji_id: str) -> bool:
        """贴表情反应（S10-HUMAN-P2 H9）：NapCat `set_msg_emoji_like`
        （message_id int + emoji_id str）。协议不支持/超时/无连接 bot 一律
        False——降级与限额记账在 auto_chat 服务层。"""
        try:
            bot = _connected_onebot_bot()
            if bot is None:
                return False
            await bot.call_api(
                "set_msg_emoji_like",
                message_id=message_id,
                emoji_id=emoji_id,
            )
            return True
        except Exception:
            return False

    async def send_sticker(sticker: Sticker) -> bool:
        """发表情包（S10-HUMAN-P2 H10）：把本地图片作为 OneBot v11 image 段
        发到本群（与 roco 宠物卡同一 `file:///` 绝对路径惯例）。无连接 bot、
        协议错误、文件不可读一律 False（连续失败降级在服务层）。"""
        try:
            bot = _connected_onebot_bot()
            if bot is None:
                return False
            await bot.send_group_msg(
                group_id=event.group_id,
                message=MessageSegment.image(f"file:///{sticker.path.resolve().as_posix()}"),
            )
            return True
        except Exception:
            return False

    return quota_check, send, react, send_sticker


async def _maybe_auto_reply(
    event: GroupMessageEvent,
    *,
    raw_text: str,
    settings: BotSettings,
    memory_store: ChatMemoryRepository,
    http_client: object | None,
    row_id: int | None = None,
    quoted_self: bool = False,
) -> None:
    """Bridge from the plugin into the auto-chat pipeline (S7-AUTO-07).
    Send/quota are injected so the service stays matcher- and runtime-free.

    入口分流（S10-HUMAN-P0 H1）：点名/引用/"你们"旁路直达管道（等不了话尾）；
    其余普通消息在 `auto_chat_collect_enabled` 时先进收集窗口，静默到期后
    整批交付，防抢答。`quoted_self` 来自 handler 的 reply 段解析
    （S10-HUMAN-P0-06）：引用机器人自己的消息必回，走 QUOTED 旁路。"""
    load_persona(settings)  # 前置校验：人设配置可解析（坏配置早失败）

    quota_check, send, react, send_sticker = _build_auto_chat_hooks(event, settings)

    category = classify_trigger(raw_text, settings=settings, quoted_self=quoted_self)
    bypass = category in (
        TriggerCategory.NICKNAMED,
        TriggerCategory.QUOTED,
        TriggerCategory.YOU_PLURAL,
    )
    if not settings.auto_chat_collect_enabled:
        # spec 观测性要求 collect 阶段 result 覆盖 bypass/disabled：直连前补指标
        metrics.AUTO_CHAT.labels("collect", "disabled").inc()
        await run_auto_chat(
            event=event,
            raw_text=raw_text,
            settings=settings,
            memory_store=memory_store,
            send=send,
            quota_check=quota_check,
            client=http_client,
            quoted_self=quoted_self,
            react=react,
            trigger_message_id=event.message_id,
            sticker_library=_sticker_store().library
            if settings.auto_chat_sticker_enabled
            else None,
            send_sticker=send_sticker if settings.auto_chat_sticker_enabled else None,
            nickname_resolver=_nickname_resolver(event.group_id, settings),
        )
        return
    if bypass:
        metrics.AUTO_CHAT.labels("collect", "bypass").inc()
        await run_auto_chat(
            event=event,
            raw_text=raw_text,
            settings=settings,
            memory_store=memory_store,
            send=send,
            quota_check=quota_check,
            client=http_client,
            quoted_self=quoted_self,
            react=react,
            trigger_message_id=event.message_id,
            sticker_library=_sticker_store().library
            if settings.auto_chat_sticker_enabled
            else None,
            send_sticker=send_sticker if settings.auto_chat_sticker_enabled else None,
            nickname_resolver=_nickname_resolver(event.group_id, settings),
        )
        return
    collector = _get_auto_chat_collector()
    _PENDING_AUTO_HOOKS.setdefault(event.group_id, (quota_check, send, react, send_sticker))
    await collector.submit(
        event.group_id,
        BatchItem(
            raw_text=raw_text,
            sender_user_id=event.user_id,
            created_at=datetime.now(UTC),
            row_id=row_id,
        ),
    )
    metrics.AUTO_CHAT_PENDING.set(collector.pending_count())


_STICKER_STORE: StickerStore | None = None
_NICKNAME_DIR: NicknameDirectory | None = None


def _nickname_directory() -> NicknameDirectory:
    """进程级群昵称目录单例（S11-AUTO-04）：OneBot `get_group_member_list`
    拉全量成员，群名片优先、QQ 昵称兜底；只在遇到未知 id 时拉取并落盘
    `data/group_nicknames.json`（部署者裁决：不做周期刷新），防抖 5 分钟。"""
    global _NICKNAME_DIR
    if _NICKNAME_DIR is None:

        async def _fetch_members(group_id: int) -> object:
            bot = _connected_onebot_bot()
            if bot is None:
                raise RuntimeError("no connected OneBot bot")
            return await bot.call_api("get_group_member_list", group_id=group_id)

        _NICKNAME_DIR = NicknameDirectory(_fetch_members)
    return _NICKNAME_DIR


def _nickname_resolver(
    group_id: int, settings: BotSettings
) -> Callable[[frozenset[int]], Awaitable[dict[int, str] | None]] | None:
    """开关关闭时返回 None（prompt/tune log 全字母代称）；返回的回调内部
    吞异常——昵称是装饰性增强，任何失败回落字母。"""
    if not settings.auto_chat_nicknames_enabled:
        return None

    async def _resolve(user_ids: frozenset[int]) -> dict[int, str] | None:
        try:
            return await _nickname_directory().get(group_id, user_ids)
        except Exception:
            return None

    return _resolve


def _sticker_store() -> StickerStore:
    """进程级表情包库单例：目录在首建时从当前 settings 固化；清单内容按
    mtime 热重载（store.library 每次读都检查）。"""
    global _STICKER_STORE
    if _STICKER_STORE is None:
        _STICKER_STORE = StickerStore(Path(get_settings().auto_chat_sticker_dir))
    return _STICKER_STORE


def _deliver_sticker_library():
    """交付路径的表情包库快照：开关关时返回 None（prompt 不出现候选块）。"""
    settings = get_settings()
    if not settings.auto_chat_sticker_enabled:
        return None
    return _sticker_store().library


_AUTO_CHAT_COLLECTOR: AutoChatCollector | None = None
_PENDING_AUTO_HOOKS: dict[int, tuple] = {}


def _get_auto_chat_collector() -> AutoChatCollector:
    """进程级收集器单例：quiet/max 与 prefilter 在首建时从当前 settings
    固化（settings 视为每进程静态，不做热重载）。"""
    global _AUTO_CHAT_COLLECTOR
    if _AUTO_CHAT_COLLECTOR is None:
        settings = get_settings()

        async def _deliver(group_id: int, batch: CollectedBatch) -> None:
            hooks = _PENDING_AUTO_HOOKS.pop(group_id, None)
            quota_check, send, react, send_sticker = hooks if hooks else (None, None, None, None)
            try:
                memory_store = get_chat_repository()
                http_client = get_http_client()
            except Exception:
                memory_store = None
                http_client = None
            if memory_store is None or send is None:
                return
            await run_auto_chat(
                event=SimpleNamespace(group_id=group_id),
                raw_text=batch.items[-1].raw_text,
                batch=batch.items,
                settings=get_settings(),
                memory_store=memory_store,
                send=send,
                quota_check=quota_check,
                client=http_client,
                # 收集器批次交付路径不启用 react：一批消息没有单一触发
                # message_id 语义，保持 react=None 让 react 分支自然短路。
                react=None,
                trigger_message_id=None,
                # 表情包（H10）与 react 不同：挑一张群情绪图没有"回复某条
                # 消息"的语义要求，批次路径同样可用——库非空且开关开即注入候选。
                sticker_library=_deliver_sticker_library(),
                send_sticker=send_sticker,
                nickname_resolver=_nickname_resolver(group_id, get_settings()),
            )
            # 交付后刷新 pending gauge（批次已出队，恢复到当前全局挂起数）
            metrics.AUTO_CHAT_PENDING.set(
                0 if _AUTO_CHAT_COLLECTOR is None else _AUTO_CHAT_COLLECTOR.pending_count()
            )

        _AUTO_CHAT_COLLECTOR = AutoChatCollector(
            quiet_seconds=settings.auto_chat_collect_quiet_seconds,
            max_seconds=settings.auto_chat_collect_max_seconds,
            prefilter=lambda text: static_prefilter(text, settings=settings),
            metric=lambda stage, result: metrics.AUTO_CHAT.labels(stage, result).inc(),
            deliver=_deliver,
        )
    return _AUTO_CHAT_COLLECTOR


def _mentions_self(event: GroupMessageEvent) -> bool:
    try:
        self_id = int(event.self_id)
    except (TypeError, ValueError):
        return False
    return self_id in extract_at_user_ids(event.get_message())


def _strip_leading_self_mention_text(event: GroupMessageEvent) -> str:
    segments = iter(event.get_message())
    try:
        first_segment = next(segments)
    except StopIteration:
        return event.get_message().extract_plain_text()

    if first_segment.type != "at" or str(first_segment.data.get("qq", "")) != str(event.self_id):
        return event.get_message().extract_plain_text()

    return "".join(
        str(segment.data.get("text", "")) for segment in segments if segment.type == "text"
    )


def _without_self_mentions(user_ids: list[int], event: GroupMessageEvent) -> list[int]:
    try:
        self_id = int(event.self_id)
    except (TypeError, ValueError):
        return user_ids
    return [user_id for user_id in user_ids if user_id != self_id]
