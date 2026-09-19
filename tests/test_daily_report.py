"""Life report service tests (S6-REPORT) — fake clients and canned fixtures,
offline by construction; the deployed 60s fork is never contacted."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from qq_bot.config import BotSettings
from qq_bot.services.daily_report import (
    NewsItem,
    SourceError,
    fetch_section_items,
    truncate_items,
)
from qq_bot.services.reliability import CircuitOpenError


async def _no_sleep(_seconds: float) -> None:
    return None


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeGetClient:
    """Minimal AsyncGetClient stand-in with canned payloads and failure injection."""

    def __init__(
        self,
        payload_by_endpoint: dict[str, Any] | None = None,
        *,
        failures_before_success: int = 0,
        always_raise: Exception | None = None,
    ) -> None:
        self.payload_by_endpoint = payload_by_endpoint or {}
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
        endpoint = url.rsplit("/", 1)[-1]
        return _FakeResponse(self.payload_by_endpoint[endpoint])


def _settings(**overrides: Any) -> BotSettings:
    return BotSettings(report_60s_base_url="http://x", **overrides)


@pytest.mark.parametrize("endpoint", ["news", "hot", "heh"])
async def test_endpoint_maps_to_base_url(endpoint: str) -> None:
    client = _FakeGetClient({endpoint: {"items": [{"title": "T"}]}})
    await fetch_section_items(endpoint, _settings(), client=client, sleep=_no_sleep)
    assert client.urls == [f"http://x/{endpoint}"]


async def test_fetch_normalizes_and_drops_blank_titles() -> None:
    client = _FakeGetClient(
        {"news": {"items": [{"title": "甲"}, {"title": "  "}, {"title": "乙"}]}}
    )
    items = await fetch_section_items("news", _settings(), client=client, sleep=_no_sleep)
    assert items == (NewsItem(title="甲"), NewsItem(title="乙"))


async def test_fetch_retries_transient_then_succeeds() -> None:
    client = _FakeGetClient({"hot": {"items": [{"title": "热搜"}]}}, failures_before_success=1)
    items = await fetch_section_items("hot", _settings(), client=client, sleep=_no_sleep)
    assert items == (NewsItem(title="热搜"),)
    assert len(client.urls) == 2


async def test_fetch_raises_source_error_when_exhausted() -> None:
    client = _FakeGetClient(always_raise=httpx.ConnectError("boom"))
    with pytest.raises(SourceError):
        await fetch_section_items("news", _settings(), client=client, sleep=_no_sleep)


async def test_fetch_short_circuits_on_open_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report

    class _OpenBreaker:
        async def check(self) -> None:
            raise CircuitOpenError("open")

    monkeypatch.setattr(daily_report, "_breaker_for", lambda name, settings: _OpenBreaker())
    client = _FakeGetClient({"news": {"items": [{"title": "不该被请求"}]}})
    with pytest.raises(SourceError, match="circuit"):
        await fetch_section_items("news", _settings(), client=client)


async def test_fetch_short_circuits_when_base_url_missing() -> None:
    with pytest.raises(SourceError, match="base url"):
        await fetch_section_items("news", BotSettings(), client=_FakeGetClient())


async def test_fetch_rejects_unknown_endpoint() -> None:
    with pytest.raises(ValueError, match="unknown report endpoint"):
        await fetch_section_items("weather", _settings(), client=_FakeGetClient())


async def test_fetch_invalid_payload_raises_source_error() -> None:
    client = _FakeGetClient({"heh": {"nope": []}})
    with pytest.raises(SourceError):
        await fetch_section_items("heh", _settings(), client=client, sleep=_no_sleep)


def test_truncate_items_limits_count() -> None:
    items = tuple(NewsItem(title=str(index)) for index in range(5))
    assert len(truncate_items(items, 2)) == 2
    assert truncate_items(items, 0) == ()


# ---- 离线日期板块（S6-REPORT-03）----


def test_date_line_present() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_date_lines

    lines = build_date_lines(date(2026, 9, 15))
    assert lines == ("【早报】9月15日 周二 农历八月初五",)


def test_evening_variant_header() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_date_lines

    assert build_date_lines(date(2026, 9, 15), kind="晚报")[0].startswith("【晚报】")


def test_holiday_marked() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_date_lines

    assert "法定节假日" in build_date_lines(date(2026, 10, 1))[0]


def test_non_holiday_has_no_marker() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_date_lines

    assert "法定节假日" not in build_date_lines(date(2026, 9, 16))[0]


def test_missing_lunar_library_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    from datetime import date

    from qq_bot.services.daily_report import build_date_lines

    real_import = builtins.__import__

    def _blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "cnlunar":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    lines = build_date_lines(date(2026, 9, 15))
    assert lines[0].startswith("【早报】9月15日 周二")


def test_missing_holiday_library_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    from datetime import date

    from qq_bot.services.daily_report import build_date_lines

    real_import = builtins.__import__

    def _blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "chinese_calendar":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    lines = build_date_lines(date(2026, 10, 1))
    assert lines[0].startswith("【早报】10月1日 周四")


# ---- LLM 润色与每日上限（S6-REPORT-04）----


def test_verify_polished_accepts_reorder_and_annotation() -> None:
    from qq_bot.services.daily_report import verify_polished

    titles = ("第一条新闻标题", "第二条新闻标题")
    text = "· 第二条新闻标题（附短评）\n· 第一条新闻标题\n\n【寄语】今天也要加油"
    assert verify_polished(titles, text) is True


def test_verify_polished_rejects_missing_item() -> None:
    from qq_bot.services.daily_report import verify_polished

    titles = ("第一条新闻标题", "第二条新闻标题")
    assert verify_polished(titles, "· 第一条新闻标题\n【寄语】好") is False


def test_verify_polished_ignores_jiyu_section() -> None:
    from qq_bot.services.daily_report import verify_polished

    titles = ("标题甲",)
    text = "· 标题甲\n【寄语】寄语区写什么都不参与比对"
    assert verify_polished(titles, text) is True


def test_verify_polished_rejects_extra_bullets() -> None:
    from qq_bot.services.daily_report import verify_polished

    titles = ("标题甲",)
    text = "· 标题甲\n· 标题乙\n【寄语】好"
    assert verify_polished(titles, text) is False


def test_format_news_template_lists_titles() -> None:
    from qq_bot.services.daily_report import format_news_template

    text = format_news_template((NewsItem(title="甲"), NewsItem(title="乙")))
    assert text == "📰 新闻\n1. 甲\n2. 乙"


async def test_polish_disabled_returns_template() -> None:
    from qq_bot.services.daily_report import polish_news

    outcome = await polish_news((NewsItem(title="甲"), NewsItem(title="乙")), _settings())
    assert outcome.reason == "disabled"
    assert outcome.ok is False
    assert "1. 甲" in outcome.text


async def test_polish_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report

    class _Quota:
        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 1}

        async def record_usage(self, **kwargs: Any) -> None:
            raise AssertionError("capped 路径不应记录用量")

    monkeypatch.setattr(daily_report, "_quota_service", lambda: _Quota())
    outcome = await daily_report.polish_news(
        (NewsItem(title="甲"),),
        _settings(report_ai_enabled=True, report_ai_daily_max=1),
    )
    assert outcome.reason == "capped"
    assert "1. 甲" in outcome.text


async def test_polish_model_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report
    from qq_bot.services.ai_client import AIReplyError

    class _Quota:
        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise AIReplyError("down")

    monkeypatch.setattr(daily_report, "_quota_service", lambda: _Quota())
    monkeypatch.setattr(daily_report, "request_ai_reply", _boom)
    outcome = await daily_report.polish_news(
        (NewsItem(title="甲"),), _settings(report_ai_enabled=True)
    )
    assert outcome.reason == "error"
    assert outcome.ok is False


async def test_polish_check_failed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report

    class _Quota:
        def __init__(self) -> None:
            self.recorded: list[dict[str, Any]] = []

        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            self.recorded.append(kwargs)

    async def _bad_polish(*args: Any, **kwargs: Any) -> str:
        return "· 凭空编造的标题\n【寄语】好"

    quota = _Quota()
    monkeypatch.setattr(daily_report, "_quota_service", lambda: quota)
    monkeypatch.setattr(daily_report, "request_ai_reply", _bad_polish)
    outcome = await daily_report.polish_news(
        (NewsItem(title="甲"),), _settings(report_ai_enabled=True)
    )
    assert outcome.reason == "check_failed"
    assert quota.recorded == []


async def test_polish_success_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report

    class _Quota:
        def __init__(self) -> None:
            self.recorded: list[dict[str, Any]] = []

        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            self.recorded.append(kwargs)

    async def _good_polish(*args: Any, **kwargs: Any) -> str:
        return "· 甲\n\n【寄语】早上好"

    quota = _Quota()
    monkeypatch.setattr(daily_report, "_quota_service", lambda: quota)
    monkeypatch.setattr(daily_report, "request_ai_reply", _good_polish)
    outcome = await daily_report.polish_news(
        (NewsItem(title="甲"),), _settings(report_ai_enabled=True)
    )
    assert outcome.reason == "ok"
    assert outcome.ok is True
    assert len(quota.recorded) == 1
    assert quota.recorded[0]["scope_type"] == "report"


# ---- 早/晚报组装器（S6-REPORT-05）----


def _all_sources_client() -> _FakeGetClient:
    return _FakeGetClient(
        {
            "news": {"items": [{"title": "新闻甲"}, {"title": "新闻乙"}]},
            "hot": {"items": [{"title": f"热搜{i}"} for i in range(1, 6)]},
            "heh": {"items": [{"title": f"热帖{i}"} for i in range(1, 4)]},
        }
    )


async def test_morning_message_assembles_all_sections() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_morning_message

    settings = _settings(countdown_events="六级考试:2030-12-12")
    text = await build_life_morning_message(
        settings, client=_all_sources_client(), today=date(2026, 9, 15)
    )
    assert text.startswith("【早报】9月15日 周二 农历八月初五")
    assert "⏰ 倒计时" in text
    assert "距离六级考试还有" in text
    assert "📰 新闻" in text
    assert "1. 新闻甲" in text
    assert "1. 热搜1" in text
    assert "1. 热帖1" in text


async def test_evening_header_variant() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_evening_message

    text = await build_life_evening_message(
        _settings(), client=_all_sources_client(), today=date(2026, 9, 15)
    )
    assert text.startswith("【晚报】9月15日 周二")


async def test_unavailable_sources_omitted() -> None:
    """2026-09-16 用户裁决：板块失败直接省略，不再渲染 '—' 占位。"""
    from datetime import date

    from qq_bot.services.daily_report import build_life_morning_message

    client = _FakeGetClient(always_raise=httpx.ConnectError("down"))
    text = await build_life_morning_message(_settings(), client=client, today=date(2026, 9, 15))
    assert "📰 新闻" not in text
    assert "🔥 热搜" not in text
    assert "🎮 小黑盒热帖" not in text
    assert text.startswith("【早报】9月15日 周二")


async def test_hotlists_truncated_to_max_items() -> None:
    """2026-09-19 拆分：热搜与热帖各自限条数（热搜默认放宽到 10）。"""
    from datetime import date

    from qq_bot.services.daily_report import build_life_morning_message

    settings = _settings(report_hot_max_items=2, report_heh_max_items=1)
    text = await build_life_morning_message(
        settings, client=_all_sources_client(), today=date(2026, 9, 15)
    )
    assert "1. 热搜1" in text and "2. 热搜2" in text and "3. 热搜3" not in text
    assert "1. 热帖1" in text and "2. 热帖2" not in text


async def test_polish_failure_keeps_template_news(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from qq_bot.services import daily_report as dr
    from qq_bot.services.ai_client import AIReplyError
    from qq_bot.services.daily_report import build_life_morning_message

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise AIReplyError("down")

    monkeypatch.setattr(dr, "_quota_service", lambda: None)
    monkeypatch.setattr(dr, "request_ai_reply", _boom)
    text = await build_life_morning_message(
        _settings(report_ai_enabled=True),
        client=_all_sources_client(),
        today=date(2026, 9, 15),
    )
    assert "1. 新闻甲" in text
    assert "【寄语】" not in text


# ---- 2026-09-16 修订：新闻条数/屏蔽/晚报换源 ----


def _news_client() -> _FakeGetClient:
    return _FakeGetClient(
        {
            "news": {
                "items": [
                    {"title": "新闻甲"},
                    {"title": "新闻乙"},
                    {"title": "新闻丙"},
                    {"title": "新闻丁"},
                ]
            },
            "toutiao": {
                "items": [{"title": f"头条{i}"} for i in range(1, 9)],
            },
        }
    )


async def test_news_truncated_to_max_items() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_morning_message

    settings = _settings(report_news_max_items=2)
    text = await build_life_morning_message(
        settings, client=_news_client(), today=date(2026, 9, 16)
    )
    assert "1. 新闻甲" in text and "2. 新闻乙" in text
    assert "新闻丙" not in text and "新闻丁" not in text


async def test_news_blocklist_filters_titles() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_morning_message

    settings = _settings(report_news_blocklist="乙,丁")
    text = await build_life_morning_message(
        settings, client=_news_client(), today=date(2026, 9, 16)
    )
    assert "1. 新闻甲" in text and "2. 新闻丙" in text
    assert "新闻乙" not in text and "新闻丁" not in text


async def test_evening_news_uses_toutiao_by_default() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_evening_message

    text = await build_life_evening_message(
        _settings(), client=_news_client(), today=date(2026, 9, 16)
    )
    assert "📰 头条热榜" in text
    assert "1. 头条1" in text
    assert "📰 新闻" not in text


async def test_morning_news_keeps_60s_source() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_morning_message

    text = await build_life_morning_message(
        _settings(), client=_news_client(), today=date(2026, 9, 16)
    )
    assert "📰 新闻" in text
    assert "1. 新闻甲" in text


async def test_evening_endpoint_configurable_back_to_news() -> None:
    from datetime import date

    from qq_bot.services.daily_report import build_life_evening_message

    settings = _settings(report_evening_news_endpoint="news")
    text = await build_life_evening_message(
        settings, client=_news_client(), today=date(2026, 9, 16)
    )
    assert "📰 新闻" in text
    assert "1. 新闻甲" in text


async def test_polish_fallback_provider_swaps_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """REPORT_AI_PROVIDER=fallback 时润色应使用备用链路的端点与密钥。"""
    from qq_bot.services import daily_report as dr

    captured: dict = {}

    class _Quota:
        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            return None

    async def _fake_request(prompt, *, settings, client=None, **kwargs):
        captured["base_url"] = settings.ai_base_url
        captured["api_key"] = settings.ai_api_key
        captured["model"] = settings.ai_model
        return "· 标题甲\n\n【寄语】好"

    monkeypatch.setattr(dr, "_quota_service", lambda: _Quota())
    monkeypatch.setattr(dr, "request_ai_reply", _fake_request)
    settings = _settings(
        report_ai_enabled=True,
        report_ai_provider="fallback",
        report_ai_model="glm-4-flash",
        ai_base_url="https://api.deepseek.com",
        ai_api_key="sk-primary",
        ai_fallback_base_url="https://open.bigmodel.cn/api/paas/v4",
        ai_fallback_api_key="sk-glm",
    )
    outcome = await dr.polish_news((NewsItem(title="标题甲"),), settings)
    assert outcome.reason == "ok"
    assert captured["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert captured["api_key"] == "sk-glm"
    assert captured["model"] == "glm-4-flash"


async def test_polish_primary_provider_keeps_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report as dr

    captured: dict = {}

    class _Quota:
        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            return None

    async def _fake_request(prompt, *, settings, client=None, **kwargs):
        captured["base_url"] = settings.ai_base_url
        captured["api_key"] = settings.ai_api_key
        return "· 标题甲\n\n【寄语】好"

    monkeypatch.setattr(dr, "_quota_service", lambda: _Quota())
    monkeypatch.setattr(dr, "request_ai_reply", _fake_request)
    settings = _settings(
        report_ai_enabled=True,
        ai_base_url="https://api.deepseek.com",
        ai_api_key="sk-primary",
    )
    await dr.polish_news((NewsItem(title="标题甲"),), settings)
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["api_key"] == "sk-primary"


async def test_polish_uses_dedicated_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """润色调用应使用 REPORT_AI_TIMEOUT_SECONDS 而非主链路超时（免费档生成慢）。"""
    from qq_bot.services import daily_report as dr

    captured: dict = {}

    class _Quota:
        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            return None

    async def _fake_request(prompt, *, settings, client, **kwargs):
        captured["client"] = client
        return "· 标题甲\n\n【寄语】好"

    monkeypatch.setattr(dr, "_quota_service", lambda: _Quota())
    monkeypatch.setattr(dr, "request_ai_reply", _fake_request)
    settings = _settings(report_ai_enabled=True, report_ai_timeout_seconds=90.0)
    await dr.polish_news((NewsItem(title="标题甲"),), settings, client=object())
    timeout = captured["client"].timeout
    assert timeout.connect == 90.0 and timeout.read == 90.0


def test_repair_appends_truncated_tail_titles() -> None:
    from qq_bot.services.daily_report import _repair_polished

    titles = ("标题甲", "标题乙", "标题丙")
    reply = "- 标题甲【甲短评】\n- 标题乙【乙短评】\n\n【寄语】好"
    repaired = _repair_polished(titles, reply)
    assert repaired is not None
    assert "3. 标题丙" in repaired
    assert "寄语" not in repaired
    assert "标题甲" in repaired


def test_repair_rejects_fabricated_lines() -> None:
    from qq_bot.services.daily_report import _repair_polished

    titles = ("标题甲",)
    reply = "- 标题甲\n- 凭空编造的标题\n【寄语】好"
    assert _repair_polished(titles, reply) is None


# ---- 2026-09-19 修订：去重 / 编号格式 / 热搜资料介绍 ----


def test_dedupe_items_drops_repeats() -> None:
    from qq_bot.services.daily_report import _dedupe_items

    items = (
        NewsItem(title="热搜甲"),
        NewsItem(title="热搜甲 "),  # 仅空白差异
        NewsItem(title="热搜甲！"),  # 标点差异
        NewsItem(title="热搜乙"),
    )
    assert _dedupe_items(items) == (NewsItem(title="热搜甲"), NewsItem(title="热搜乙"))


def test_verify_hot_polished_positional() -> None:
    from qq_bot.services.daily_report import verify_hot_polished

    titles = ("热搜甲", "热搜乙")
    assert (
        verify_hot_polished(
            titles, "1. 热搜甲：某地发生一件事\n2. 热搜乙\n【今日小结】今天热点集中"
        )
        is True
    )
    # 顺序错位：热搜榜排名有意义，判定失败
    assert verify_hot_polished(titles, "1. 热搜乙\n2. 热搜甲") is False
    # 条数不符
    assert verify_hot_polished(titles, "1. 热搜甲") is False
    # 凭空行
    assert verify_hot_polished(titles, "1. 热搜甲\n2. 热搜乙\n3. 凭空编造") is False


def test_repair_hot_renumbers_and_reorders() -> None:
    from qq_bot.services.daily_report import _repair_hot_polished

    titles = ("热搜甲", "热搜乙", "热搜丙")
    reply = "1. 热搜乙：乙的介绍\n2. 热搜甲：甲的介绍"  # 顺序错位且缺尾
    repaired = _repair_hot_polished(titles, reply)
    assert repaired == "1. 热搜甲：甲的介绍\n2. 热搜乙：乙的介绍\n3. 热搜丙"


def test_repair_hot_keeps_title_when_number_strip_breaks_it() -> None:
    from qq_bot.services.daily_report import _repair_hot_polished

    titles = ("2026年高考出分",)
    repaired = _repair_hot_polished(titles, "2026年高考出分：多地公布分数线")
    assert repaired == "1. 2026年高考出分：多地公布分数线"


async def test_hot_section_skips_llm_without_search_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无联网搜索配置时不做无事实基础的介绍，热搜直接用标题模板。"""
    from datetime import date

    from qq_bot.services import daily_report as dr
    from qq_bot.services.daily_report import build_life_morning_message

    calls: list[str] = []

    async def _fake_request(prompt, *, settings, client=None, **kwargs):
        calls.append(prompt)
        return "1. 新闻甲\n2. 新闻乙"

    monkeypatch.setattr(dr, "_quota_service", lambda: None)
    monkeypatch.setattr(dr, "request_ai_reply", _fake_request)
    text = await build_life_morning_message(
        _settings(report_ai_enabled=True),
        client=_all_sources_client(),
        today=date(2026, 9, 19),
    )
    assert len(calls) == 1  # 只有新闻润色调了 LLM
    assert "1. 热搜1" in text and "2. 热搜2" in text


async def test_hot_section_polishes_with_search_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    import qq_bot.services.search as search_service
    from qq_bot.services import daily_report as dr
    from qq_bot.services.daily_report import build_life_morning_message
    from qq_bot.services.search import SearchResult

    prompts: list[str] = []

    async def _fake_search(query: str, *, settings, client=None):
        return [SearchResult(title=f"{query}的报道", url="http://n", content="事件的具体经过")]

    async def _fake_request(prompt, *, settings, client=None, **kwargs):
        prompts.append(prompt)
        return (
            "1. 热搜1：关于热搜1的具体介绍\n"
            "2. 热搜2\n"
            "3. 热搜3\n"
            "4. 热搜4\n"
            "5. 热搜5\n"
            "【今日小结】今天事件类热点居多"
        )

    monkeypatch.setattr(search_service, "search_web", _fake_search)
    monkeypatch.setattr(dr, "_quota_service", lambda: None)
    monkeypatch.setattr(dr, "request_ai_reply", _fake_request)
    settings = _settings(
        report_ai_enabled=True, search_enabled=True, tavily_api_key="tvly-", report_hot_max_items=5
    )
    text = await build_life_morning_message(
        settings, client=_all_sources_client(), today=date(2026, 9, 19)
    )
    # 热搜润色 prompt 带逐条资料；成品保留板块标题、介绍与小结
    hot_prompts = [p for p in prompts if "热搜编辑" in p]
    assert len(hot_prompts) == 1
    assert "资料：" in hot_prompts[0]
    assert "🔥 热搜" in text
    assert "1. 热搜1：关于热搜1的具体介绍" in text
    assert "【今日小结】今天事件类热点居多" in text
    # 新闻成品不带小结/寄语
    assert "1. 新闻甲" in text


async def test_news_section_polished_keeps_section_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """润色输出只有条目行：板块标题行由组装器补上，截断条目由 repair 兜底。"""
    from datetime import date

    from qq_bot.services import daily_report as dr
    from qq_bot.services.daily_report import build_life_morning_message

    async def _partial_polish(prompt, *, settings, client=None, **kwargs):
        return "2. 新闻乙\n1. 新闻甲（甲短评）"

    monkeypatch.setattr(dr, "_quota_service", lambda: None)
    monkeypatch.setattr(dr, "request_ai_reply", _partial_polish)
    text = await build_life_morning_message(
        _settings(report_ai_enabled=True),
        client=_news_client(),
        today=date(2026, 9, 19),
    )
    assert "📰 新闻" in text
    assert "2. 新闻乙" in text
    assert "1. 新闻甲（甲短评）" in text
    assert "3. 新闻丙" in text and "4. 新闻丁" in text


async def test_hot_section_search_failure_degrades_to_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    import qq_bot.services.search as search_service
    from qq_bot.services import daily_report as dr
    from qq_bot.services.ai_client import AIReplyError
    from qq_bot.services.daily_report import build_life_morning_message

    async def _boom_search(query: str, *, settings, client=None):
        raise search_service.SearchError("down")

    async def _boom_request(*args: Any, **kwargs: Any) -> str:
        raise AIReplyError("down")

    monkeypatch.setattr(search_service, "search_web", _boom_search)
    monkeypatch.setattr(dr, "_quota_service", lambda: None)
    monkeypatch.setattr(dr, "request_ai_reply", _boom_request)
    settings = _settings(report_ai_enabled=True, search_enabled=True, tavily_api_key="tvly-")
    text = await build_life_morning_message(
        settings, client=_all_sources_client(), today=date(2026, 9, 19)
    )
    assert "1. 热搜1" in text and "2. 热搜2" in text
    assert "【今日小结】" not in text


async def test_polish_hot_success_keeps_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report as dr

    class _Quota:
        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            return None

    async def _fake_request(prompt, *, settings, client=None, **kwargs):
        assert "资料：现场情况" in prompt
        return "1. 热搜甲：现场情况说明\n2. 热搜乙\n\n【寄语】加油"

    monkeypatch.setattr(dr, "_quota_service", lambda: _Quota())
    monkeypatch.setattr(dr, "request_ai_reply", _fake_request)
    settings = _settings(report_ai_enabled=True)
    outcome = await dr.polish_hot(
        (NewsItem(title="热搜甲"), NewsItem(title="热搜乙")),
        settings,
        material=("现场情况", ""),
    )
    assert outcome.ok is True
    assert "1. 热搜甲：现场情况说明" in outcome.text
    assert "寄语" not in outcome.text


async def test_polish_hot_check_failed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from qq_bot.services import daily_report as dr

    class _Quota:
        def __init__(self) -> None:
            self.recorded: list[dict[str, Any]] = []

        async def summary(self, *, scope_type: str, scope_id: int):
            return {"requests": 0}

        async def record_usage(self, **kwargs: Any) -> None:
            self.recorded.append(kwargs)

    async def _fabricated(prompt, *, settings, client=None, **kwargs):
        return "1. 热搜甲：正常\n2. 凭空编造的热搜"

    quota = _Quota()
    monkeypatch.setattr(dr, "_quota_service", lambda: quota)
    monkeypatch.setattr(dr, "request_ai_reply", _fabricated)
    settings = _settings(report_ai_enabled=True)
    outcome = await dr.polish_hot(
        (NewsItem(title="热搜甲"), NewsItem(title="热搜乙")), settings, material=("", "")
    )
    assert outcome.reason == "check_failed"
    assert outcome.text == "🔥 热搜\n1. 热搜甲\n2. 热搜乙"
    assert quota.recorded == []
