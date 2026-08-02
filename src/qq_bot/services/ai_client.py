from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

import httpx

from qq_bot.agent.models import NormalizedResponse, ProviderCapabilities, ToolCall
from qq_bot.config import BotSettings, get_settings
from qq_bot.runtime import BREAKER_AI_FALLBACK, BREAKER_AI_PRIMARY, RuntimeStateError, get_runtime
from qq_bot.services.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    PermanentDependencyError,
    TransientDependencyError,
    build_retry_policy,
    classify_exception,
    wrap_http_error,
)

_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


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


def build_chat_payload(
    prompt: str,
    settings: BotSettings,
    *,
    search_context: str = "",
    chat_context: str = "",
    roco_context: str = "",
    current_time: str | None = None,
) -> dict[str, Any]:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise AIReplyError("prompt cannot be empty")

    system_prompt = (
        "你是一个自然的 QQ 群助手，像 QQ 群友聊天。"
        f"当前本地时间：{current_time or _format_current_local_time()}。"
        "回答当前日期、时间、星期时必须以该本地时间为准，不要自行推算或改写。"
        "先直接回答问题，不要总用“好的”“当然”“我来整理”开头。"
        "语气自然，不要像新闻稿或客服；不确定就说不确定。"
        "不要编造事实，不要编造链接，不要编造时间，不要编造价格。"
        "默认 2-4 句，新闻或搜索类问题可以用 3-5 条短点，控制在 600 字以内。"
    )
    cleaned_search_context = search_context.strip()
    cleaned_chat_context = chat_context.strip()
    cleaned_roco_context = roco_context.strip()
    user_sections = [f"当前用户问题：{cleaned_prompt}"]
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
            "回复末尾加“来源：”，最多 3 条，格式为“1. 标题 - URL”。"
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
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
        "max_tokens": 600,
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
        )
    except AIReplyError:
        if not active_settings.has_ai_fallback_config():
            raise
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
        )


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
) -> str:
    """Run one provider under its own retry policy and breaker. Any provider
    failure raises ``AIReplyError``; the caller decides about fallback."""
    payload = build_chat_payload(
        prompt,
        settings.model_copy(update={"ai_model": model}),
        search_context=search_context,
        chat_context=chat_context,
        roco_context=roco_context,
    )
    data = await _post_chat_completion(
        payload,
        settings=settings,
        client=client,
        base_url=base_url,
        api_key=api_key,
        model=model,
        breaker_name=breaker_name,
    )
    normalized = _normalize_response(data)
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
) -> dict[str, Any]:
    """POST one chat completion under the stage-1 retry policy and breaker.
    Returns the parsed JSON body; content extraction is the caller's job."""
    breaker = _breaker_for(breaker_name)
    try:
        await breaker.check()
    except CircuitOpenError:
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
                try:
                    data = await _post_once(
                        payload,
                        settings=settings,
                        client=client,
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                    )
                except AIReplyError:
                    # Invalid response / empty content: permanent, not
                    # retried, not counted against the breaker.
                    raise
                except Exception as exc:
                    await breaker.on_failure(classify_exception(exc))
                    raise
                await breaker.on_success()
                return data
    except (TransientDependencyError, PermanentDependencyError) as exc:
        raise AIReplyError("AI API request failed") from exc
    raise AIReplyError("AI API request failed")


async def _post_once(
    payload: dict[str, Any],
    *,
    settings: BotSettings,
    client: AsyncPostClient,
    base_url: str,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
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
        "max_tokens": 600,
    }
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
    except AIReplyError:
        if provider != "primary" or not active_settings.has_ai_fallback_config():
            raise
        data = await _post_chat_completion(
            payload,
            settings=active_settings,
            client=active_client,
            base_url=active_settings.normalized_ai_fallback_base_url,
            api_key=active_settings.ai_fallback_api_key,
            model=active_settings.ai_fallback_model,
            breaker_name=BREAKER_AI_FALLBACK,
        )
    return _normalize_response(data)


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
