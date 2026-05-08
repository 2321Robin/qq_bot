from qq_bot.services.prompt import extract_ai_prompt


def test_extract_ai_prompt_from_prefix() -> None:
    assert extract_ai_prompt("ai 介绍一下你自己", prefix="ai") == "介绍一下你自己"


def test_extract_ai_prompt_accepts_case_insensitive_prefix() -> None:
    assert extract_ai_prompt("AI 你好", prefix="ai") == "你好"


def test_extract_ai_prompt_returns_empty_string_for_prefix_only() -> None:
    assert extract_ai_prompt("ai", prefix="ai") == ""


def test_extract_ai_prompt_ignores_normal_chat() -> None:
    assert extract_ai_prompt("今天吃什么", prefix="ai") is None


def test_extract_ai_prompt_uses_custom_prefix() -> None:
    assert extract_ai_prompt("ask 天气怎么样", prefix="ask") == "天气怎么样"
