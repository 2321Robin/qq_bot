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
