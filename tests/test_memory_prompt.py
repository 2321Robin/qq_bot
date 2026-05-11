from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.memory_prompt import (
    MemoryReference,
    extract_at_user_ids,
    format_chat_context,
    parse_memory_reference,
)


def test_parse_recent_reference() -> None:
    parsed = parse_memory_reference("参考最近20条：继续总结", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="继续总结", limit=20)


def test_parse_keyword_reference() -> None:
    parsed = parse_memory_reference("参考 洛克王国 的聊天：我们之前说了什么", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="我们之前说了什么", keyword="洛克王国")


def test_parse_mentioned_user_recent_reference() -> None:
    parsed = parse_memory_reference("参考 @小明 的最近20条：总结他的想法", mentioned_user_ids=[2001])

    assert parsed == MemoryReference(question="总结他的想法", user_id=2001, limit=20)


def test_parse_mentioned_user_keyword_reference() -> None:
    parsed = parse_memory_reference("参考 @小明 关于 洛克王国 的聊天：整理重点", mentioned_user_ids=[2001])

    assert parsed == MemoryReference(question="整理重点", user_id=2001, keyword="洛克王国")


def test_parse_non_reference_keeps_prompt_as_question() -> None:
    parsed = parse_memory_reference("讲个笑话", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="讲个笑话")


def test_extract_at_user_ids_from_message_segments() -> None:
    class FakeSegment:
        def __init__(self, segment_type: str, qq: str) -> None:
            self.type = segment_type
            self.data = {"qq": qq}

    assert extract_at_user_ids([FakeSegment("at", "2001"), FakeSegment("text", "ignored")]) == [2001]


def test_format_chat_context_includes_messages_and_ai_replies() -> None:
    rows = [
        ChatMemoryRow(1, 1001, 2001, "ai 你好", "2026-05-11T12:00:00+00:00", True, "你好呀"),
        ChatMemoryRow(2, 1001, 2002, "洛克王国", "2026-05-11T12:01:00+00:00", False, ""),
    ]

    context = format_chat_context(rows)

    assert "用户2001：ai 你好" in context
    assert "机器人：你好呀" in context
    assert "用户2002：洛克王国" in context


def test_format_chat_context_reports_empty_history() -> None:
    assert format_chat_context([]) == "没有找到相关历史聊天记录。"
