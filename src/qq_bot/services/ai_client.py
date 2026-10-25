from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import httpx

from qq_bot.agent.models import NormalizedResponse, ProviderCapabilities, ToolCall
from qq_bot.config import BotSettings, get_settings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.cost import Usage, estimate_cost, load_price_table
from qq_bot.observability.logging import current_request_id, get_logger
from qq_bot.observability.tracing import get_tracer
from qq_bot.runtime import BREAKER_AI_FALLBACK, BREAKER_AI_PRIMARY, RuntimeStateError, get_runtime
from qq_bot.services.quota import active_quota_scope
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    PermanentDependencyError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    wrap_http_error,
)

_IMAGE_PROMPT_INSTRUCTION = (
    "用户可能随消息附带图片或表情包：结合图片内容回答，先看清关键细节再说话；"
    "图片看不清或打不开就直接说明，不要编造图片里没有的内容。"
)

_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_price_table: dict[str, object] | None = None

logger = get_logger("qq_bot.ai_client")


async def _account_usage(model: str, usage: dict[str, int] | None) -> None:
    """Record provider-reported tokens and priced cost (S4-METRIC-07,
    S2-TOKEN-03). No usage -> nothing recorded; prices come from the local
    table and are never guessed (unknown/estimated stays honest). The active
    quota scope (bound by ai_chat) receives the same normalized usage/cost
    — no second parse (S4-QUOTA-03/04)."""
    global _price_table
    if not usage:
        return
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    if prompt:
        metrics.TOKENS.labels("prompt", model, "false").inc(prompt)
    if completion:
        metrics.TOKENS.labels("completion", model, "false").inc(completion)
    if _price_table is None:
        _price_table = load_price_table()
    estimate = estimate_cost(
        [
            Usage(
                prompt_tokens=prompt or None,
                completion_tokens=completion or None,
                model_id=model,
            )
        ],
        _price_table,
    )
    if estimate.cost is not None:
        metrics.COST_USD.labels(model, estimate.status).inc(estimate.cost)
    scope = active_quota_scope()
    if scope is None:
        return
    try:
        service = get_runtime().get_quota_service()
        if service is not None:
            await service.record_usage(
                scope_type=scope[0],
                scope_id=scope[1],
                tokens=prompt + completion,
                cost=estimate,
            )
    except Exception:
        # Quota recording must never fail the already-succeeded reply.
        logger.exception("quota usage recording failed")


class AIReplyError(RuntimeError):
    """Raised when the AI provider cannot produce a usable reply."""


class CapabilityError(AIReplyError):
    """The configured provider does not support a capability the request
    needs (S2-AGENT-01). Never emulates tool calls from plain text."""


class AsyncPostClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        raise NotImplementedError


def _thinking_payload_extra(
    settings: BotSettings, *, thinking_enabled: bool | None = None
) -> dict[str, Any]:
    """思考模型兼容（2026-10-24 glm-5.3-flash 主链切换）：思考 token 计入
    max_tokens，会吃光决策门/生成链路的小预算正文；ai_thinking_disabled 时
    携带 thinking.type=disabled（实测可关，DeepSeek 忽略该字段）。

    S11-QA-THINK-01（部署者北极星：回复更像人/更经过考虑）：thinking_enabled
    显式为 True 的调用方（仅 @问答链：build_chat_payload 与 agent 网关）发
    thinking.type=enabled 压过全局 disabled——自主插话保持关（抢答场景快比
    深重要）。"""
    if thinking_enabled is True:
        return {"thinking": {"type": "enabled"}}
    if not settings.ai_thinking_disabled:
        return {}
    return {"thinking": {"type": "disabled"}}


def _user_content_for(text: str, images: Sequence[str]) -> str | list[dict[str, Any]]:
    """OpenAI 风格多模态 content：无图保持纯字符串（与历史版本逐字节一致），
    带图时切换为 text + image_url 块数组（图片走公网 URL 或 base64 data URL）。"""
    if not images:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        blocks.append({"type": "image_url", "image_url": {"url": image}})
    return blocks


def build_chat_payload(
    prompt: str,
    settings: BotSettings,
    *,
    search_context: str = "",
    chat_context: str = "",
    roco_context: str = "",
    current_time: str | None = None,
    images: Sequence[str] = (),
    extra_system_prompt: str = "",
) -> dict[str, Any]:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise AIReplyError("prompt cannot be empty")

    # 人设注入（S7-AUTO-09）：人设来自 settings，随 _call_provider 的
    # model_copy 透传，无需改函数签名；grounding 约束全部保留。
    # S11-QA-VOICE-01（部署者北极星：回复更像人）：身份从"群助手"改为真实
    # 群友；去掉"2-4 句/3-5 条短点"助手模板与编点列表；客服收尾/自称 AI
    # 显式禁止；搜索出处从"来源："正式列表改为随口带出处+按需贴链接。
    persona_name = settings.persona_name.strip()
    identity = (
        f"你在QQ群里像真实群友一样聊天，群友都叫你“{persona_name}”。"
        if persona_name
        else "你在QQ群里像真实群友一样聊天。"
    )
    system_prompt = (
        identity
        + f"你的说话风格：{settings.effective_persona_prompt}。"
        + f"当前本地时间：{current_time or _format_current_local_time()}。"
        "回答当前日期、时间、星期时必须以该本地时间为准，不要自行推算或改写。"
        "先直接回答问题，不要总用“好的”“当然”“我来整理”开头。"
        "像群友打字那样说话：多数时候一两句话就答完，事情复杂就连贯多说几句，"
        "禁止用 1. 2. 3. 这种编点列表，禁止分条排版；单条上限 1200 字，"
        "但绝大多数回答根本用不到，别凑字数。"
        "语气自然，不要像新闻稿、客服或 wiki；不确定就说不确定，不知道就说不知道。"
        "不要说“希望这能帮到你”“祝你”“很高兴为你”这类客服收尾；"
        "除非被直接问到，不要主动说自己是机器人或 AI。"
        "不要编造事实，不要编造链接，不要编造时间，不要编造价格。"
    )
    cleaned_search_context = search_context.strip()
    cleaned_chat_context = chat_context.strip()
    cleaned_roco_context = roco_context.strip()
    user_sections = [f"当前用户问题：{cleaned_prompt}"]
    if images:
        system_prompt += f" {_IMAGE_PROMPT_INSTRUCTION}"
    extra = extra_system_prompt.strip()
    if extra:
        system_prompt += f" {extra}"
    if cleaned_chat_context:
        system_prompt += (
            " 如果提供了历史聊天记录，只把它作为理解前文和用户意图的参考。"
            "不要编造不存在的历史聊天记录；历史不足时要直接说明。"
        )
        user_sections.append(cleaned_chat_context)

    if cleaned_roco_context:
        system_prompt += (
            " 如果提供了本地洛克王国资料，它是可信的本地数据，优先级高于联网搜索和模型记忆；"
            "回答洛克王国精灵、技能、进化问题时优先依据这些本地资料。"
            "不要猜本地资料外的洛克王国数据；资料没有记录或字段为空时，"
            "要直接说本地数据没有记录。"
        )
        user_sections.append(f"本地洛克王国资料：\n{cleaned_roco_context}")

    if cleaned_search_context:
        system_prompt += (
            " 如果提供了联网搜索资料，请优先依据资料回答；"
            "不要编造资料外的信息，不要编造链接，不要编造时间，不要编造价格。"
            "如果搜索资料不足或互相冲突，就说没有可靠来源或信息不一致。"
            "是搜来的事实可以随口带一句从哪看到的，需要时把最有用的一条链接直接贴出来，"
            "但别用“来源：1. 2. 3.”这种正式列表收尾。"
        )
        user_sections.append(f"联网搜索资料：\n{cleaned_search_context}")

    user_content = (
        "\n\n".join(user_sections)
        if cleaned_search_context or cleaned_chat_context or cleaned_roco_context
        else cleaned_prompt
    )

    return {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _user_content_for(user_content, images)},
        ],
        "temperature": 0.7,
        # 思考模型（glm-5.3 系）的思考 token 计入 max_tokens：带图请求的
        # 推理明显变长，1200 会被思考吃光导致正文为空（2026-10-25 线上实测：
        # 7 次带图请求 4 次空正文 → 判空失败落备用链、丢图回答）。上限是界
        # 不是预算（S11-BUDGET-01），带图时放宽到 2400，纯文本请求不变。
        "max_tokens": 2400 if images else 1200,
        **_thinking_payload_extra(settings, thinking_enabled=settings.ai_qa_thinking_enabled),
    }


def _breaker_for(name: str) -> CircuitBreaker:
    """Resolve a provider breaker from the runtime, falling back to a fresh
    default when no runtime is installed (unit tests)."""
    try:
        return get_runtime().get_breaker(name)
    except RuntimeStateError:
        settings = get_settings()
        return CircuitBreaker(
            name=name,
            failure_threshold=settings.breaker_failure_threshold,
            recovery_seconds=settings.breaker_recovery_seconds,
        )


def _resolve_client(client: AsyncPostClient | None) -> AsyncPostClient:
    if client is not None:
        return client
    try:
        return get_runtime().get_http_client()
    except RuntimeStateError as exc:
        raise AIReplyError("AI client is not available (runtime not ready)") from exc


async def request_ai_reply(
    prompt: str,
    *,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    search_context: str = "",
    chat_context: str = "",
    roco_context: str = "",
    images: Sequence[str] = (),
    extra_system_prompt: str = "",
) -> str:
    active_settings = settings or get_settings()
    if not active_settings.has_ai_config():
        raise AIReplyError("AI_API_KEY is not configured")
    if not prompt.strip():
        raise AIReplyError("prompt cannot be empty")

    active_client = _resolve_client(client)

    try:
        return await _call_provider(
            prompt,
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_base_url,
            api_key=active_settings.ai_api_key,
            model=active_settings.ai_model,
            breaker_name=BREAKER_AI_PRIMARY,
            search_context=search_context,
            chat_context=chat_context,
            roco_context=roco_context,
            images=images,
            extra_system_prompt=extra_system_prompt,
        )
    except AIReplyError:
        if not active_settings.has_ai_fallback_config():
            raise
        metrics.FALLBACKS.labels("ai").inc()
        # 静默切换会让"实际在用哪个模型"变得不可见（部署者以为在用主模型），
        # 这里必须留痕；只记模型名，不记任何密钥。
        logger.warning(
            "AI primary failed, serving with fallback: requested_model=%s fallback_model=%s",
            active_settings.ai_model,
            active_settings.ai_fallback_model,
        )
        if images:
            # 备用模型不承诺视觉（当前 fallback=glm-4-flash 无视觉）：带图重试
            # 注定 4xx，还会把本可回答的问题拖成"服务不可用"。落备用时一律
            # 纯文本回答（模型至少能围绕文字回应），并留指标可查。
            metrics.AI_VISION.labels("fallback_text").inc()
        return await _call_provider(
            prompt,
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_fallback_base_url,
            api_key=active_settings.ai_fallback_api_key,
            model=active_settings.ai_fallback_model,
            breaker_name=BREAKER_AI_FALLBACK,
            search_context=search_context,
            chat_context=chat_context,
            roco_context=roco_context,
            extra_system_prompt=extra_system_prompt,
        )


async def request_completion(
    *,
    system_prompt: str,
    user_prompt: str,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    model: str = "",
    max_tokens: int = 512,
    temperature: float = 0.7,
    json_mode: bool = False,
    images: Sequence[str] = (),
) -> str:
    """Single-turn completion with primary->fallback switching (S7-AUTO).

    Used by the auto-chat gate (json_mode + router model) and the casual
    persona replies. Separated from ``request_ai_reply`` because these calls
    carry no search/memory/roco context blocks.

    ``images``（S11-VISION-01）：自主插话生成链的触发图（决策门永不带图）；
    落备用时与 request_ai_reply 同规则一律去掉图片。"""
    active_settings = settings or get_settings()
    if not active_settings.has_ai_config():
        raise AIReplyError("AI_API_KEY is not configured")
    active_client = _resolve_client(client)
    chosen_model = model.strip() or active_settings.ai_model

    def _payload(for_model: str, for_images: Sequence[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": for_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _user_content_for(user_prompt, for_images)},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        payload.update(_thinking_payload_extra(active_settings))
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    try:
        return await _call_small_provider(
            _payload(chosen_model, images),
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_base_url,
            api_key=active_settings.ai_api_key,
            model=chosen_model,
            breaker_name=BREAKER_AI_PRIMARY,
            timeout_seconds=(active_settings.ai_qa_vision_timeout_seconds if images else None),
        )
    except AIReplyError:
        if not active_settings.has_ai_fallback_config():
            raise
        metrics.FALLBACKS.labels("ai").inc()
        effective = active_settings.ai_fallback_model
        logger.warning(
            "AI primary failed, serving with fallback: requested_model=%s fallback_model=%s",
            chosen_model,
            effective,
        )
        # 备用侧永远用 ai_fallback_model（与 _call_provider 一致）：
        # 显式指定的主侧模型（如 router 便宜模型）在备用商可能不存在。
        if images:
            metrics.AI_VISION.labels("fallback_text").inc()
        return await _call_small_provider(
            _payload(effective, ()),
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_fallback_base_url,
            api_key=active_settings.ai_fallback_api_key,
            model=effective,
            breaker_name=BREAKER_AI_FALLBACK,
        )


async def _call_small_provider(
    payload: dict[str, Any],
    *,
    settings: BotSettings,
    client: AsyncPostClient,
    base_url: str,
    api_key: str,
    model: str,
    breaker_name: str,
    timeout_seconds: float | None = None,
) -> str:
    data = await _post_chat_completion(
        payload,
        settings=settings,
        client=client,
        base_url=base_url,
        api_key=api_key,
        model=model,
        breaker_name=breaker_name,
        timeout_seconds=timeout_seconds,
    )
    normalized = _normalize_response(data)
    await _account_usage(model, normalized.usage)
    if normalized.text is None:
        raise AIReplyError("AI API returned an empty response")
    return normalized.text


async def _call_provider(
    prompt: str,
    *,
    settings: BotSettings,
    client: AsyncPostClient,
    base_url: str,
    api_key: str,
    model: str,
    breaker_name: str,
    search_context: str,
    chat_context: str,
    roco_context: str,
    images: Sequence[str] = (),
    extra_system_prompt: str = "",
) -> str:
    """Run one provider under its own retry policy and breaker. Any provider
    failure raises ``AIReplyError``; the caller decides about fallback.

    视觉降级（S11-VISION-01）：带图请求被主模型**永久性拒绝**（4xx——模型
    无视觉/图片源拉取失败/图片超限）时，同链路去掉图片重试一次纯文本，
    问答主流程不中断（fail-open）；瞬时失败保持带图走既有重试与主备切换。"""
    if images:
        metrics.AI_VISION.labels("request").inc()
    payload = build_chat_payload(
        prompt,
        settings.model_copy(update={"ai_model": model}),
        search_context=search_context,
        chat_context=chat_context,
        roco_context=roco_context,
        images=images,
        extra_system_prompt=extra_system_prompt,
    )
    timeout_seconds = settings.ai_qa_vision_timeout_seconds if images else None
    try:
        data = await _post_chat_completion(
            payload,
            settings=settings,
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            breaker_name=breaker_name,
            timeout_seconds=timeout_seconds,
        )
    except AIReplyError as exc:
        if not images or not isinstance(exc.__cause__, PermanentDependencyError):
            raise
        metrics.AI_VISION.labels("degraded").inc()
        logger.warning(
            "vision request permanently rejected; retrying same provider without images: model=%s",
            model,
        )
        data = await _post_chat_completion(
            build_chat_payload(
                prompt,
                settings.model_copy(update={"ai_model": model}),
                search_context=search_context,
                chat_context=chat_context,
                roco_context=roco_context,
                extra_system_prompt=extra_system_prompt,
            ),
            settings=settings,
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            breaker_name=breaker_name,
        )
    normalized = _normalize_response(data)
    await _account_usage(model, normalized.usage)
    if normalized.text is None:
        raise AIReplyError("AI API returned an empty response")
    return normalized.text


async def _post_chat_completion(
    payload: dict[str, Any],
    *,
    settings: BotSettings,
    client: AsyncPostClient,
    base_url: str,
    api_key: str,
    model: str,
    breaker_name: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """POST one chat completion under the stage-1 retry policy and breaker.
    Returns the parsed JSON body; content extraction is the caller's job.
    ``timeout_seconds`` 覆盖共享客户端的读超时（视觉请求单独放宽用）。"""
    breaker = _breaker_for(breaker_name)
    provider = "primary" if breaker_name == BREAKER_AI_PRIMARY else "fallback"
    tracer = get_tracer()
    span = tracer.start_span("model.call", trace_id=current_request_id(), provider=provider)
    started_at = time.perf_counter()
    try:
        await breaker.check()
    except CircuitOpenError:
        metrics.AI_REQUESTS.labels(provider, "circuit_open").inc()
        tracer.end_span(span, status="error")
        raise AIReplyError("AI provider is temporarily unavailable") from None

    policy = build_retry_policy(
        max_attempts=settings.ai_max_attempts,
        base_delay_seconds=settings.ai_retry_base_delay_seconds,
        max_delay_seconds=settings.ai_retry_max_delay_seconds,
        jitter_ratio=settings.retry_jitter_ratio,
    )
    try:
        async for attempt in policy:
            with attempt:
                if attempt.retry_state.attempt_number >= 2:
                    metrics.RETRIES.labels("ai").inc()
                try:
                    data = await _post_once(
                        payload,
                        settings=settings,
                        client=client,
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        timeout_seconds=timeout_seconds,
                    )
                except AIReplyError:
                    # Invalid response / empty content: permanent, not
                    # retried, not counted against the breaker.
                    raise
                except Exception as exc:
                    await breaker.on_failure(classify_exception(exc))
                    raise
                await breaker.on_success()
                metrics.AI_DURATION.labels(provider).observe(time.perf_counter() - started_at)
                result = "retried" if attempt.retry_state.attempt_number >= 2 else "ok"
                metrics.AI_REQUESTS.labels(provider, result).inc()
                tracer.end_span(span)
                return data
    except (TransientDependencyError, PermanentDependencyError) as exc:
        record_error("ai", classify_exception(exc).category.value)
        metrics.AI_REQUESTS.labels(provider, "error").inc()
        tracer.end_span(span, status="error", category=classify_exception(exc).category.value)
        raise AIReplyError("AI API request failed") from exc
    tracer.end_span(span, status="error")
    raise AIReplyError("AI API request failed")


async def _post_once(
    payload: dict[str, Any],
    *,
    settings: BotSettings,
    client: AsyncPostClient,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    try:
        kwargs: dict[str, Any] = {}
        if timeout_seconds is not None:
            # 显式覆盖读超时；注意 httpx 传 timeout=None 会变成"无限等待"，
            # 只在确有覆盖值时才携带该参数。
            kwargs["timeout"] = timeout_seconds
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={**payload, "model": model},
            **kwargs,
        )
        response.raise_for_status()
        data = response.json()
    except (TransientDependencyError, PermanentDependencyError):
        raise
    except httpx.HTTPStatusError as exc:
        raise wrap_http_error(exc) from exc
    except httpx.HTTPError as exc:
        raise wrap_http_error(exc) from exc
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise AIReplyError("AI API returned an invalid response") from exc
    return data


def provider_capabilities(settings: BotSettings) -> ProviderCapabilities:
    """Capabilities the configured providers expose (S2-AGENT-01).
    Fallback providers share the same capability switches; a provider that
    cannot satisfy a request raises :class:`CapabilityError` up front."""
    return ProviderCapabilities(
        tools=settings.ai_provider_tools_enabled,
        structured_output=settings.ai_provider_structured_output_enabled,
        usage=True,
    )


def _normalize_usage(raw_usage: Any) -> dict[str, int] | None:
    if not isinstance(raw_usage, dict):
        return None
    usage: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw_usage.get(key)
        if isinstance(value, int):
            usage[key] = value
    return usage or None


def _normalize_response(data: dict[str, Any]) -> NormalizedResponse:
    """Normalize one provider response: text + validated tool calls +
    finish reason + optional usage (S2-AGENT-02). Malformed tool calls are
    an explicit error, never a silent text reply."""
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIReplyError("AI API returned an invalid response") from exc

    raw_content = message.get("content")
    text: str | None = None
    if raw_content is not None:
        if not isinstance(raw_content, str):
            raise AIReplyError("AI API returned an invalid response")
        text = raw_content.strip() or None

    tool_calls: list[ToolCall] = []
    raw_tool_calls = message.get("tool_calls")
    if raw_tool_calls:
        if not isinstance(raw_tool_calls, list):
            raise AIReplyError("AI API returned invalid tool calls")
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, dict):
                raise AIReplyError("AI API returned invalid tool calls")
            call_id = raw_call.get("id", "")
            function = raw_call.get("function")
            if not isinstance(function, dict):
                raise AIReplyError("AI API returned invalid tool calls")
            name = function.get("name", "")
            raw_arguments = function.get("arguments", "{}")
            try:
                arguments = json.loads(raw_arguments) if raw_arguments else {}
            except (TypeError, ValueError) as exc:
                raise AIReplyError("AI API returned unparseable tool arguments") from exc
            if not isinstance(arguments, dict):
                raise AIReplyError("AI API returned unparseable tool arguments")
            try:
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
            except Exception as exc:
                raise AIReplyError("AI API returned invalid tool calls") from exc

    finish_reason = choice.get("finish_reason", "")
    if not isinstance(finish_reason, str):
        finish_reason = ""

    if text is None and not tool_calls:
        raise AIReplyError("AI API returned an empty response")
    return NormalizedResponse(
        text=text,
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        usage=_normalize_usage(data.get("usage")),
    )


async def request_model_turn(
    *,
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    response_format: dict[str, Any] | None = None,
    settings: BotSettings | None = None,
    client: AsyncPostClient | None = None,
    provider: str = "primary",
) -> NormalizedResponse:
    """Structured model gateway turn (S2-AGENT-01/02/07). Uses the shared
    client, stage-1 retry policy and per-provider breakers; the primary
    failure falls back to the fallback provider under the existing contract.

    Raises :class:`CapabilityError` when the provider cannot satisfy the
    requested capabilities — before any network call.
    """
    active_settings = settings or get_settings()
    if not active_settings.has_ai_config():
        raise AIReplyError("AI_API_KEY is not configured")
    if not messages or not messages[-1].get("content", "").strip():
        raise AIReplyError("prompt cannot be empty")

    capabilities = provider_capabilities(active_settings)
    if tools and not capabilities.tools:
        raise CapabilityError("provider tools capability is disabled")
    if response_format and not capabilities.structured_output:
        raise CapabilityError("provider structured output capability is disabled")

    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": 0.2 if response_format else 0.7,
        "max_tokens": 1200,
    }
    payload.update(
        _thinking_payload_extra(
            active_settings, thinking_enabled=active_settings.ai_qa_thinking_enabled
        )
    )
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if response_format:
        payload["response_format"] = response_format

    active_client = _resolve_client(client)
    try:
        data = await _post_chat_completion(
            payload,
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_base_url,
            api_key=active_settings.ai_api_key,
            model=active_settings.ai_model,
            breaker_name=BREAKER_AI_PRIMARY,
        )
        model = active_settings.ai_model
    except AIReplyError:
        if provider != "primary" or not active_settings.has_ai_fallback_config():
            raise
        metrics.FALLBACKS.labels("ai").inc()
        logger.warning(
            "AI primary failed, serving with fallback: requested_model=%s fallback_model=%s",
            active_settings.ai_model,
            active_settings.ai_fallback_model,
        )
        data = await _post_chat_completion(
            payload,
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_fallback_base_url,
            api_key=active_settings.ai_fallback_api_key,
            model=active_settings.ai_fallback_model,
            breaker_name=BREAKER_AI_FALLBACK,
        )
        model = active_settings.ai_fallback_model
    normalized = _normalize_response(data)
    await _account_usage(model, normalized.usage)
    return normalized


class AiModelGateway:
    """Object adapter over :func:`request_model_turn` for the agent stack
    (router, orchestrator, verifier, summarizer all speak the same
    ``ModelGateway`` protocol). Settings and shared client are bound at
    construction; callers may override per call."""

    def __init__(
        self,
        settings: BotSettings | None = None,
        *,
        client: AsyncPostClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def request_model_turn(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        settings: BotSettings | None = None,
        client: AsyncPostClient | None = None,
        provider: str = "primary",
    ) -> NormalizedResponse:
        return await request_model_turn(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            settings=settings or self._settings,
            client=client or self._client,
            provider=provider,
        )


def _format_current_local_time() -> str:
    local_now = datetime.now()
    return f"{local_now:%Y-%m-%d %H:%M}，{_WEEKDAY_NAMES[local_now.weekday()]}"
