from qq_bot.services.help_text import build_help_text


def test_help_text_lists_supported_commands() -> None:
    text = build_help_text(ai_prefix="ai")

    assert "/help" in text
    assert "/ping" in text
    assert "/精灵 迪莫" in text
    assert "/技能 闪光" in text
    assert "/计数 迪莫" in text
    assert "ai 你好" in text
    assert "定时任务" in text


def test_help_text_uses_configured_ai_prefix() -> None:
    text = build_help_text(ai_prefix="ask")

    assert "ask 你好" in text
