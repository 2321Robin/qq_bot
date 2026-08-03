from datetime import UTC, datetime, timedelta

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

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
from qq_bot.services.chat_memory import ChatMemoryRepository
from qq_bot.services.memory_prompt import (
    extract_at_user_ids,
    extract_at_user_ids_before_separator,
    format_chat_context,
    parse_memory_reference,
)
from qq_bot.services.message_formatting import replace_named_mentions
from qq_bot.services.onebot_send import finish_with_send_errors_logged
from qq_bot.services.prompt import extract_ai_prompt
from qq_bot.services.quota import quota_scope
from qq_bot.services.reliability import classify_exception
from qq_bot.services.roco_knowledge import build_roco_context
from qq_bot.services.search import (
    SearchError,
    format_search_context,
    prompt_needs_search,
    search_web,
)


ai_chat = on_message(priority=20, block=False)

# Stable clarification replies (S2-AGENT-09): no model call, no draft, and
# never a reason for the user to rephrase with private details.
_AGENT_CLARIFY_MESSAGES: dict[ReasonCode, str] = {
    ReasonCode.CLARIFY: "没太明白你的意思，能说得更具体一点吗？",
    ReasonCode.CAPABILITY_ERROR: "这个功能还没有配置好，先换个问题试试吧。",
    ReasonCode.RULE_FALLBACK: "暂时无法处理这个问题，请稍后再试。",
}
_AGENT_DEFAULT_CLARIFY = "没太明白你的意思，能说得更具体一点吗？"
_AGENT_UNAVAILABLE = "AI 服务暂时不可用，请稍后再试。"
_QUOTA_RATE_MESSAGE = "提问太频繁了，请稍后再试。"
_QUOTA_COST_MESSAGE = "今日 AI 用量已达预算上限，请明天再试。"


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
                        await memory_store.add_message(
                            group_id=event.group_id,
                            user_id=event.user_id,
                            message_text=raw_text,
                            is_ai_prompt=is_ai_prompt,
                        )
                    except Exception as exc:
                        record_error("memory", classify_exception(exc).category.value)
                        logger.exception(
                            "Chat memory write failed; continuing without storing message"
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
                    message_text=raw_text,
                    is_ai_prompt=is_ai_prompt,
                )
            except Exception as exc:
                record_error("memory", classify_exception(exc).category.value)
                logger.exception("Chat memory write failed; continuing without storing message")

        knowledge_span = tracer.start_span("knowledge.lookup", trace_id=trace_id)
        try:
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
            await finish_with_send_errors_logged(ai_chat, _AGENT_UNAVAILABLE)

        if memory_message_id is not None:
            try:
                await memory_store.update_ai_reply(memory_message_id, reply)
            except Exception:
                logger.exception("Chat memory reply update failed")

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
        await finish_with_send_errors_logged(ai_chat, _AGENT_UNAVAILABLE)
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
            _AGENT_CLARIFY_MESSAGES.get(route.reason_code, _AGENT_DEFAULT_CLARIFY),
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
