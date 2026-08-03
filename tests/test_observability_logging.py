"""Observability logging facade tests (S4-LOG-01..07).

Covers hash_id determinism/privacy, LogContext correlation scope, the JSON
whitelist formatter, record_event field filtering, the dual logger hookup
(stdlib qq_bot.* + loguru nonebot in one process) and the memory_commands
privacy fix (S4-LOG-06).
"""

from __future__ import annotations

import json
import logging
import sys

import bot  # noqa: F401  (initializes NoneBot for matcher creation)
import pytest

from qq_bot.observability.logging import (
    JsonFormatter,
    LogContext,
    _ALLOWED_KEYS,
    get_logger,
    hash_id,
    install_logging,
    new_request_id,
    record_event,
)
from qq_bot.plugins import memory_commands


def _make_record(
    message: str = "some message",
    *,
    name: str = "qq_bot.test",
    level: int = logging.INFO,
) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


@pytest.fixture
def restore_logging() -> None:
    """Reset stdlib root handlers and loguru sinks touched by install_logging."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    try:
        from loguru import logger as loguru_logger

        loguru_logger.remove()
        loguru_logger.add(sys.stderr, level=0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# hash_id (S4-LOG-02)
# ---------------------------------------------------------------------------


def test_hash_id_is_deterministic_and_prefixed() -> None:
    first = hash_id(123456789, kind="group")
    second = hash_id(123456789, kind="group")
    assert first == second
    assert first.startswith("group_")
    assert len(first) == len("group_") + 16
    assert hash_id(42, kind="user").startswith("user_")
    assert hash_id("2880000001", kind="user").startswith("user_")


def test_hash_id_output_never_contains_raw_number() -> None:
    raw = 123456789
    for kind in ("group", "user"):
        output = hash_id(raw, kind=kind)
        assert str(raw) not in output
        assert output.split("_", 1)[1].isdigit() is False
        assert all(char in "0123456789abcdef" for char in output.split("_", 1)[1])


def test_hash_id_hashes_differ_between_kinds() -> None:
    assert hash_id(123456789, kind="group") != hash_id(123456789, kind="user")


# ---------------------------------------------------------------------------
# LogContext (S4-LOG-01)
# ---------------------------------------------------------------------------


def test_log_context_carries_request_id_and_group_hash() -> None:
    formatter = JsonFormatter()
    outside = json.loads(formatter.format(_make_record()))
    assert "request_id" not in outside

    request_id = new_request_id()
    with LogContext(request_id=request_id, group_id=123456):
        inside = json.loads(formatter.format(_make_record()))
        assert inside["request_id"] == request_id
        assert inside["group_hash"].startswith("group_")
        assert "123456" not in inside["group_hash"]

    after = json.loads(formatter.format(_make_record()))
    assert "request_id" not in after
    assert "group_hash" not in after


def test_log_context_nested_scopes_merge() -> None:
    formatter = JsonFormatter()
    outer_id = new_request_id()
    with LogContext(request_id=outer_id, group_id=111):
        with LogContext(request_id=new_request_id(), user_id=222):
            payload = json.loads(formatter.format(_make_record()))
            # inner user_hash added; outer group_hash kept
            assert payload["user_hash"].startswith("user_")
            assert payload["group_hash"].startswith("group_")
            assert payload["request_id"] != outer_id
    payload = json.loads(formatter.format(_make_record()))
    assert "user_hash" not in payload


# ---------------------------------------------------------------------------
# JsonFormatter + record_event (S4-LOG-03/04)
# ---------------------------------------------------------------------------


def test_json_formatter_output_is_parseable_and_whitelisted() -> None:
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_make_record("hello world")))
    assert set(payload.keys()) <= _ALLOWED_KEYS
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "qq_bot.test"
    assert "ts" in payload


def test_json_formatter_drops_non_whitelist_extra_keys() -> None:
    formatter = JsonFormatter()
    record = _make_record("event text")
    record.extra_fields = {
        "event": "some_event",
        "attempt": 2,
        "max_attempts": 3,
        "secret_payload": "message body",
        "prompt": "do not leak",
    }
    payload = json.loads(formatter.format(record))
    assert payload["event"] == "some_event"
    assert payload["attempt"] == 2
    assert "secret_payload" not in payload
    assert "prompt" not in payload
    assert "message body" not in json.dumps(payload, ensure_ascii=False)


def test_record_event_carries_whitelisted_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("qq_bot.test_events")
    with caplog.at_level(logging.INFO, logger="qq_bot.test_events"):
        record_event(
            logger,
            logging.INFO,
            "test_event",
            message="something happened",
            attempt=2,
            junk="dropped",
        )
    records = [r for r in caplog.records if r.name == "qq_bot.test_events"]
    assert records
    assert records[0].extra_fields == {
        "event": "test_event",
        "message": "something happened",
        "attempt": 2,
    }


def test_record_event_message_defaults_to_event_name(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("qq_bot.test_events")
    with caplog.at_level(logging.INFO, logger="qq_bot.test_events"):
        record_event(logger, logging.INFO, "plain_event")
    records = [r for r in caplog.records if r.name == "qq_bot.test_events"]
    assert records[0].getMessage() == "plain_event"


# ---------------------------------------------------------------------------
# install_logging dual hookup (S4-LOG-05)
# ---------------------------------------------------------------------------


def test_install_json_logging_covers_stdlib_and_nonebot_loggers(
    capsys: pytest.CaptureFixture[str],
    restore_logging: None,
) -> None:
    install_logging(log_format="json", log_level="INFO")

    from nonebot import logger as nonebot_logger

    request_id = new_request_id()
    with LogContext(request_id=request_id, group_id=987654):
        get_logger("qq_bot.sample").info("stdlib line")
        nonebot_logger.info("nonebot line")

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip().startswith("{")]
    assert len(lines) >= 2

    parsed = [json.loads(line) for line in lines]
    stdlib_lines = [p for p in parsed if p.get("logger") == "qq_bot.sample"]
    nonebot_lines = [p for p in parsed if p.get("message") == "nonebot line"]
    assert stdlib_lines, "stdlib qq_bot logger did not emit a JSON line"
    assert nonebot_lines, "loguru nonebot logger did not emit a JSON line"

    for line in parsed:
        assert set(line.keys()) <= _ALLOWED_KEYS
        assert line["request_id"] == request_id
        assert line["group_hash"].startswith("group_")
        assert "987654" not in line["group_hash"]


def test_install_json_logging_text_mode_keeps_plain_stdlib(
    restore_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    install_logging(log_format="text", log_level="INFO")
    root = logging.getLogger()
    assert not any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers)
    with caplog.at_level(logging.INFO, logger="qq_bot.sample"):
        get_logger("qq_bot.sample").info("plain line")
    assert "plain line" in caplog.text


# ---------------------------------------------------------------------------
# memory_commands privacy fix (S4-LOG-06, scenario B)
# ---------------------------------------------------------------------------


class FinishCalled(Exception):
    def __init__(self, message: object) -> None:
        self.message = message


class _FakeEvent:
    group_id = 1001
    user_id = 2001

    def __init__(self, text: str = "") -> None:
        self.text = text

    def get_message(self) -> "_FakeEvent":
        return self

    def extract_plain_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(())


class _FakeArgs:
    def extract_plain_text(self) -> str:
        return "全部"


async def test_memory_delete_all_log_uses_hashes_only(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeService:
        async def delete_all(self, *, group_id: int, user_id: int) -> list[str]:
            return ["msg-1", "msg-2"]

    monkeypatch.setattr(memory_commands, "_service", lambda: FakeService())

    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    original_finish = memory_commands.memory_delete_command.finish
    memory_commands.memory_delete_command.finish = fake_finish  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.INFO, logger="qq_bot.memory_commands"):
            with pytest.raises(FinishCalled):
                await memory_commands.handle_memory_delete(_FakeEvent(), _FakeArgs())
    finally:
        memory_commands.memory_delete_command.finish = original_finish  # type: ignore[method-assign]

    text = caplog.text
    assert "memory delete_all" in text
    assert "1001" not in text
    assert "2001" not in text
    assert "group_" in text
    assert "user_" in text
    assert "affected_messages=2" in text
