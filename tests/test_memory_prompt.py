from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.memory_prompt import (
    MemoryReference,
    extract_at_user_ids,
    format_chat_context,
    parse_memory_reference,
    render_rows_with_aliases,
    substitute_reference_tokens,
)


def test_parse_recent_reference() -> None:
    parsed = parse_memory_reference("参考最近20条：继续总结", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="继续总结", limit=20)


def test_parse_keyword_reference() -> None:
    parsed = parse_memory_reference("参考 洛克王国 的聊天：我们之前说了什么", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="我们之前说了什么", keyword="洛克王国")


def test_parse_mentioned_user_recent_reference() -> None:
    parsed = parse_memory_reference(
        "参考 @小明 的最近20条：总结他的想法", mentioned_user_ids=[2001]
    )

    assert parsed == MemoryReference(question="总结他的想法", user_id=2001, limit=20)


def test_parse_mentioned_user_keyword_reference() -> None:
    parsed = parse_memory_reference(
        "参考 @小明 关于 洛克王国 的聊天：整理重点", mentioned_user_ids=[2001]
    )

    assert parsed == MemoryReference(question="整理重点", user_id=2001, keyword="洛克王国")


def test_parse_mentioned_user_only_reference() -> None:
    parsed = parse_memory_reference("参考 @小明：总结他的想法", mentioned_user_ids=[2001])

    assert parsed == MemoryReference(question="总结他的想法", user_id=2001)


def test_parse_non_reference_keeps_prompt_as_question() -> None:
    parsed = parse_memory_reference("讲个笑话", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="讲个笑话")


def test_parse_reference_without_separator_keeps_full_prompt_as_question() -> None:
    parsed = parse_memory_reference("参考最近20条继续总结", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="参考最近20条继续总结")


def test_parse_reference_url_colon_keeps_full_prompt_as_question() -> None:
    parsed = parse_memory_reference("参考 https://example.com 怎么看", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="参考 https://example.com 怎么看")


def test_parse_reference_prose_colon_keeps_full_prompt_as_question() -> None:
    parsed = parse_memory_reference("参考 这个说法: 怎么看", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="参考 这个说法: 怎么看")


def test_parse_valid_recent_reference_allows_ascii_colon_separator() -> None:
    parsed = parse_memory_reference("参考最近20条: 继续总结", mentioned_user_ids=[])

    assert parsed == MemoryReference(question="继续总结", limit=20)


def test_extract_at_user_ids_from_message_segments() -> None:
    class FakeSegment:
        def __init__(self, segment_type: str, qq: str) -> None:
            self.type = segment_type
            self.data = {"qq": qq}

    assert extract_at_user_ids([FakeSegment("at", "2001"), FakeSegment("text", "ignored")]) == [
        2001
    ]


def test_extract_at_user_ids_skips_invalid_payloads() -> None:
    class FakeSegment:
        def __init__(self, segment_type: str, qq: object) -> None:
            self.type = segment_type
            self.data = {"qq": qq}

    assert extract_at_user_ids([FakeSegment("at", "all"), FakeSegment("at", None)]) == []


def test_extract_at_user_ids_preserves_valid_mention_order() -> None:
    class FakeSegment:
        def __init__(self, segment_type: str, qq: str) -> None:
            self.type = segment_type
            self.data = {"qq": qq}

    assert extract_at_user_ids([FakeSegment("at", "2002"), FakeSegment("at", "2001")]) == [
        2002,
        2001,
    ]


def test_parse_uses_first_valid_mentioned_user_id() -> None:
    parsed = parse_memory_reference(
        "参考 @小明 的最近20条：总结他的想法", mentioned_user_ids=[2002, 2001]
    )

    assert parsed == MemoryReference(question="总结他的想法", user_id=2002, limit=20)


def test_parse_natural_mentioned_user_sent_messages_reference() -> None:
    parsed = parse_memory_reference("总结 @小明 发送的消息", mentioned_user_ids=[2002])

    assert parsed == MemoryReference(question="总结 发送的消息", user_id=2002)


def test_format_chat_context_includes_messages_and_ai_replies() -> None:
    rows = [
        ChatMemoryRow(1, 1001, 2001, "ai 你好", "2026-05-11T12:00:00+00:00", True, "你好呀"),
        ChatMemoryRow(2, 1001, 2002, "洛克王国", "2026-05-11T12:01:00+00:00", False, ""),
    ]

    context = format_chat_context(rows)

    assert "用户A：ai 你好" in context
    assert "机器人：你好呀" in context
    assert "用户B：洛克王国" in context
    # raw QQ ids never reach the prompt (aliasing, 实测泄露问题)
    assert "2001" not in context
    assert "2002" not in context


def test_format_chat_context_aliases_raw_user_ids() -> None:
    rows = [
        ChatMemoryRow(
            id=1,
            group_id=1,
            user_id=12345,
            message_text="今天活动是什么",
            created_at="2026-09-23T00:00:00+00:00",
            is_ai_prompt=False,
            ai_reply="",
        ),
        ChatMemoryRow(
            id=2,
            group_id=1,
            user_id=67890,
            message_text="同问",
            created_at="2026-09-23T00:01:00+00:00",
            is_ai_prompt=False,
            ai_reply="好的",
        ),
    ]

    text = format_chat_context(rows)

    assert "12345" not in text
    assert "67890" not in text
    assert "用户A：今天活动是什么" in text
    assert "用户B：同问" in text


def test_format_chat_context_reports_empty_history() -> None:
    assert format_chat_context([]) == "没有找到相关历史聊天记录。"


# ---- 真实群昵称渲染（S11-AUTO-04，部署者裁决 2026-10-25）----


def test_render_rows_with_aliases_prefers_nicknames_and_falls_back() -> None:
    rows = [
        ChatMemoryRow(
            id=1,
            group_id=1,
            user_id=12345,
            message_text="今天活动是什么",
            created_at="2026-09-23T00:00:00+00:00",
            is_ai_prompt=False,
            ai_reply="",
        ),
        ChatMemoryRow(
            id=2,
            group_id=1,
            user_id=67890,
            message_text="同问",
            created_at="2026-09-23T00:01:00+00:00",
            is_ai_prompt=False,
            ai_reply="",
        ),
    ]

    lines = render_rows_with_aliases(
        rows,
        nicknames={12345: "风哥", 67890: "123456"},  # 67890 昵称纯数字 → 回落字母
    )

    assert "风哥：今天活动是什么" in lines
    assert "用户A：同问" in lines  # 回落者不占昵称的字母位


def test_render_rows_with_aliases_without_nicknames_keeps_letters() -> None:
    rows = [
        ChatMemoryRow(
            id=1,
            group_id=1,
            user_id=12345,
            message_text="hi",
            created_at="2026-09-23T00:00:00+00:00",
            is_ai_prompt=False,
            ai_reply="",
        )
    ]

    assert render_rows_with_aliases(rows) == ["用户A：hi"]
    assert render_rows_with_aliases(rows, nicknames=None) == ["用户A：hi"]


# ---- 引用令牌匿名替换（S10-HUMAN-P0-06 H4）----


def test_reference_token_replaced_with_alias() -> None:
    alias = {1654321: "用户B"}
    assert (
        substitute_reference_tokens("回复 [引用 u1654321:上分]：冲", alias)
        == "回复 [引用 用户B:上分]：冲"
    )


def test_unknown_reference_token_becomes_anon() -> None:
    assert substitute_reference_tokens("[引用 u9:xx]", {1: "用户A"}) == "[引用 用户?:xx]"


def test_plain_text_untouched() -> None:
    assert substitute_reference_tokens("来u一下", {}) == "来u一下"


def test_ordinary_text_with_letter_u_digits_untouched() -> None:
    """Fix Round 1：替换锚定完整标记头 `[引用 u<id>:`，普通文本里的
    "au999"/"u4200" 之类不被误替换。"""
    assert substitute_reference_tokens("au999 和 u4200", {}) == "au999 和 u4200"


def test_format_chat_context_uses_nicknames_when_available() -> None:
    """S11-VISION-01 follow-up：@问答链上下文按真实群昵称渲染（与自主插话
    同一套解析），模型不再把"用户A/B"字母喊进群里；无昵称者仍回落字母。"""
    rows = [
        ChatMemoryRow(
            id=1,
            group_id=1,
            user_id=2001,
            message_text="早",
            created_at="",
            is_ai_prompt=False,
            ai_reply="",
        ),
        ChatMemoryRow(
            id=2,
            group_id=1,
            user_id=2002,
            message_text="[图片]",
            created_at="",
            is_ai_prompt=False,
            ai_reply="",
        ),
    ]

    context = format_chat_context(rows, nicknames={2001: "阿伟"})

    assert "阿伟：早" in context
    assert "用户A：[图片]" in context  # 无昵称者回落字母，序号只按回落者计
