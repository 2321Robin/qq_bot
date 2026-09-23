"""AI briefing service tests (S8-BRIEF) — canned RSS fixtures and fake
clients, offline by construction; daily.juya.uk is never contacted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest

from qq_bot.config import BotSettings
from qq_bot.services import ai_briefing
from qq_bot.services.ai_briefing import (
    BriefingItem,
    SourceError,
    build_ai_briefing_message,
    compose_briefing,
    fetch_briefing_xml,
    parse_briefing_issue,
    render_briefing_template,
)
from qq_bot.services.daily_report import PolishOutcome
from qq_bot.services.reliability import CircuitOpenError

_BEIJING = timezone(timedelta(hours=8))
_NOW = datetime(2026, 9, 22, 9, 30, tzinfo=_BEIJING)
_FRESH_PUB = "Tue, 22 Sep 2026 01:16:05 GMT"

_ISSUE_HTML = """<div><p><img src="https://assets.example.com/cover.png" alt=""></p>
<h1>AI 早报 2026-09-22</h1>
<h2>概览</h2>
<h3>要闻</h3>
<ul><li>小米发布并开源MiMo-V2.6系列模型 <a href="https://mimo.example.com/v2-6">↗</a> <code>#1</code></li><li>SpaceXAI 推出 Grok 4.7 与 Fast 版本 <a href="https://xai.example.com/grok-4-7">↗</a> <code>#2</code></li></ul>
<h3>开发生态</h3>
<ul><li>硅基流动上线Xing4.0并开放免费调用 <a href="https://sili.example.com/a">↗</a> <code>#3</code></li></ul>
<h2>要闻</h2>
<h3><a href="https://mimo.example.com/v2-6">小米发布并开源MiMo-V2.6系列模型</a> <code>#1</code></h3>
<p>系列核心包括 <code>MiMo-V2.6-Pro</code> 与 Flash，开放权重与技术报告。</p>
<p>API 价格沿用 V2.5。</p>
<h3><a href="https://xai.example.com/grok-4-7">SpaceXAI 推出 Grok 4.7 与 Fast 版本</a> <code>#2</code></h3>
<p>支持 50 万 token 上下文，价格与前代一致。</p>
<h2>开发生态</h2>
<h3><a href="https://sili.example.com/a">硅基流动上线Xing4.0并开放免费调用</a> <code>#3</code></h3>
<p>原生支持 256K 上下文。</p>
</div>"""


def _issue_xml(pub_date: str = _FRESH_PUB, content: str = _ISSUE_HTML) -> str:
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>橘鸦AI早报</title>"
        "<item><title>2026-09-22</title>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<content:encoded>{escape(content)}</content:encoded>"
        "</item></channel></rss>"
    )


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeGetClient:
    """Minimal AsyncGetClient stand-in serving one canned RSS document."""

    def __init__(
        self,
        payload: str = "",
        *,
        failures_before_success: int = 0,
        always_raise: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.failures_before_success = failures_before_success
        self.always_raise = always_raise
        self.urls: list[str] = []
        self._attempts = 0

    async def get(self, url: str, *, timeout: float) -> Any:
        self.urls.append(url)
        self._attempts += 1
        if self.always_raise is not None:
            raise self.always_raise
        if self._attempts <= self.failures_before_success:
            raise httpx.ConnectError("boom")
        return _FakeResponse(self.payload)


async def _no_sleep(_seconds: float) -> None:
    return None


def _settings(**overrides: Any) -> BotSettings:
    overrides.setdefault("ai_briefing_ai_enabled", False)
    return BotSettings(**overrides)


def _items() -> tuple[BriefingItem, ...]:
    return (
        BriefingItem(number=1, category="要闻", headline="甲模型发布", detail="甲的资料"),
        BriefingItem(number=2, category="行业动态", headline="乙公司融资"),
    )


# ---- 解析 ----
async def test_parse_issue_extracts_overview_and_details() -> None:
    items = parse_briefing_issue(_issue_xml(), now=_NOW, max_age_hours=30.0)
    assert [item.headline for item in items] == [
        "小米发布并开源MiMo-V2.6系列模型",
        "SpaceXAI 推出 Grok 4.7 与 Fast 版本",
        "硅基流动上线Xing4.0并开放免费调用",
    ]
    assert [item.number for item in items] == [1, 2, 3]
    assert [item.category for item in items] == ["要闻", "要闻", "开发生态"]
    assert items[0].url == "https://mimo.example.com/v2-6"
    assert "MiMo-V2.6-Pro" in items[0].detail
    assert "API 价格沿用 V2.5" in items[0].detail
    assert "50 万 token" in items[1].detail


def test_parse_issue_prefers_freshest_item() -> None:
    xml = _issue_xml().replace("</channel></rss>", "") + (
        "<item><title>2026-09-21</title>"
        "<pubDate>Mon, 21 Sep 2026 01:16:05 GMT</pubDate>"
        f"<content:encoded>{escape(_ISSUE_HTML)}</content:encoded>"
        "</item></channel></rss>"
    )
    items = parse_briefing_issue(xml, now=_NOW, max_age_hours=30.0)
    assert len(items) == 3


def test_parse_issue_rejects_stale_issue() -> None:
    stale_pub = "Sun, 20 Sep 2026 01:16:05 GMT"
    with pytest.raises(SourceError, match="stale"):
        parse_briefing_issue(_issue_xml(pub_date=stale_pub), now=_NOW, max_age_hours=30.0)


def test_parse_issue_rejects_invalid_xml() -> None:
    with pytest.raises(SourceError, match="xml"):
        parse_briefing_issue("<not-xml", now=_NOW, max_age_hours=30.0)


def test_parse_issue_rejects_issue_without_items() -> None:
    xml = _issue_xml(content="<div><h1>AI 早报</h1></div>")
    with pytest.raises(SourceError, match="no overview items"):
        parse_briefing_issue(xml, now=_NOW, max_age_hours=30.0)


# ---- 拉取 ----
async def test_fetch_uses_configured_feed_url() -> None:
    client = _FakeGetClient(_issue_xml())
    await fetch_briefing_xml(_settings(), client, sleep=_no_sleep)
    assert client.urls == ["https://daily.juya.uk/rss.xml"]


async def test_fetch_retries_transient_then_succeeds() -> None:
    client = _FakeGetClient(_issue_xml(), failures_before_success=1)
    text = await fetch_briefing_xml(_settings(), client, sleep=_no_sleep)
    assert "橘鸦AI早报" in text
    assert len(client.urls) == 2


async def test_fetch_raises_source_error_when_exhausted() -> None:
    client = _FakeGetClient(always_raise=httpx.ConnectError("boom"))
    with pytest.raises(SourceError):
        await fetch_briefing_xml(_settings(), client, sleep=_no_sleep)


async def test_fetch_short_circuits_on_open_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OpenBreaker:
        async def check(self) -> None:
            raise CircuitOpenError("open")

    monkeypatch.setattr(ai_briefing, "_breaker_for", lambda settings: _OpenBreaker())
    client = _FakeGetClient(_issue_xml())
    with pytest.raises(SourceError, match="circuit"):
        await fetch_briefing_xml(_settings(), client, sleep=_no_sleep)
    assert client.urls == []


# ---- LLM 编辑 ----
class _FakeQuota:
    def __init__(self, requests: int) -> None:
        self._requests = requests
        self.recorded = 0

    async def summary(self, *, scope_type: str, scope_id: int) -> dict[str, Any]:
        return {"requests": self._requests}

    async def record_usage(
        self, *, scope_type: str, scope_id: int, tokens: int, cost: float | None
    ) -> None:
        self.recorded += 1


async def test_compose_disabled_returns_template() -> None:
    outcome = await compose_briefing(_items(), _settings(ai_briefing_ai_enabled=False))
    assert outcome == PolishOutcome(
        ok=False, text=render_briefing_template(_items()), reason="disabled"
    )


async def test_compose_capped_returns_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_briefing, "_quota_service", lambda: _FakeQuota(requests=999))
    outcome = await compose_briefing(_items(), _settings(ai_briefing_ai_enabled=True))
    assert outcome.reason == "capped"


async def test_compose_ok_verified_text_and_quota_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def fake_reply(prompt: str, **_kwargs: Any) -> str:
        prompts.append(prompt)
        return "2. 乙公司融资，B轮数亿元\n1. 甲模型发布，权重全部开放"

    quota = _FakeQuota(requests=0)
    monkeypatch.setattr(ai_briefing, "_quota_service", lambda: quota)
    monkeypatch.setattr(ai_briefing, "request_ai_reply", fake_reply)
    outcome = await compose_briefing(_items(), _settings(ai_briefing_ai_enabled=True))
    assert outcome.ok and outcome.reason == "ok"
    assert quota.recorded == 1
    assert "资料：" in prompts[0]
    assert "甲的资料" in prompts[0]


async def test_compose_survives_quota_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenQuota:
        async def summary(self, *, scope_type: str, scope_id: int) -> dict[str, Any]:
            raise RuntimeError("db locked")

        async def record_usage(
            self, *, scope_type: str, scope_id: int, tokens: int, cost: float | None
        ) -> None:
            raise RuntimeError("db locked")

    async def fake_reply(_prompt: str, **_kwargs: Any) -> str:
        return "2. 乙公司融资，B轮数亿元\n1. 甲模型发布，权重全部开放"

    monkeypatch.setattr(ai_briefing, "_quota_service", lambda: _BrokenQuota())
    monkeypatch.setattr(ai_briefing, "request_ai_reply", fake_reply)
    outcome = await compose_briefing(_items(), _settings(ai_briefing_ai_enabled=True))
    assert outcome.ok is True  # 配额炸了不阻塞简报
    assert outcome.reason == "ok"


async def test_compose_falls_back_on_fabrication(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_reply(_prompt: str, **_kwargs: Any) -> str:
        return "1. 甲模型发布，权重全部开放\n2. 完全无关的编造新闻"

    monkeypatch.setattr(ai_briefing, "_quota_service", lambda: None)
    monkeypatch.setattr(ai_briefing, "request_ai_reply", fake_reply)
    outcome = await compose_briefing(_items(), _settings(ai_briefing_ai_enabled=True))
    assert not outcome.ok and outcome.reason == "check_failed"
    assert outcome.text == render_briefing_template(_items())


# ---- 组装 ----
async def test_build_returns_none_when_feed_unconfigured() -> None:
    client = _FakeGetClient(_issue_xml())
    message = await build_ai_briefing_message(
        _settings(ai_briefing_feed_url=" "), client=client, now=_NOW
    )
    assert message is None
    assert client.urls == []


async def test_build_returns_none_when_stale() -> None:
    stale_pub = "Sun, 20 Sep 2026 01:16:05 GMT"
    message = await build_ai_briefing_message(
        _settings(), client=_FakeGetClient(_issue_xml(pub_date=stale_pub)), now=_NOW
    )
    assert message is None


async def test_build_returns_none_when_feed_fails() -> None:
    message = await build_ai_briefing_message(
        _settings(), client=_FakeGetClient(always_raise=httpx.ConnectError("boom")), now=_NOW
    )
    assert message is None


async def test_build_template_message_contains_header_and_credit() -> None:
    message = await build_ai_briefing_message(
        _settings(), client=_FakeGetClient(_issue_xml()), now=_NOW
    )
    assert message is not None
    lines = message.splitlines()
    assert lines[0].startswith("【AI早报】9月22日 周二")
    assert "1. 小米发布并开源MiMo-V2.6系列模型" in lines
    assert lines[-1] == "素材来源：橘鸦AI早报"


async def test_build_uses_llm_summary_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_reply(_prompt: str, **_kwargs: Any) -> str:
        return (
            "1. 小米发布并开源MiMo-V2.6系列模型，权重与技术报告同步开放\n"
            "2. SpaceXAI 推出 Grok 4.7 与 Fast 版本\n"
            "3. 硅基流动上线Xing4.0并开放免费调用"
        )

    monkeypatch.setattr(ai_briefing, "_quota_service", lambda: None)
    monkeypatch.setattr(ai_briefing, "request_ai_reply", fake_reply)
    message = await build_ai_briefing_message(
        _settings(ai_briefing_ai_enabled=True), client=_FakeGetClient(_issue_xml()), now=_NOW
    )
    assert message is not None
    assert "权重与技术报告同步开放" in message
    assert message.endswith("素材来源：橘鸦AI早报")


async def test_build_drops_credit_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_reply(_prompt: str, **_kwargs: Any) -> str:
        return render_briefing_template(
            parse_briefing_issue(_issue_xml(), now=_NOW, max_age_hours=30.0)
        )

    monkeypatch.setattr(ai_briefing, "_quota_service", lambda: None)
    monkeypatch.setattr(ai_briefing, "request_ai_reply", fake_reply)
    message = await build_ai_briefing_message(
        _settings(ai_briefing_ai_enabled=True, ai_briefing_credit=""),
        client=_FakeGetClient(_issue_xml()),
        now=_NOW,
    )
    assert message is not None
    assert "素材来源" not in message


# ---- 配置 ----
def test_scheduled_jobs_accepts_ai_morning() -> None:
    settings = BotSettings(scheduled_jobs="ai_morning@09:30")
    assert settings.scheduled_job_list == [("ai_morning", 9, 30)]
