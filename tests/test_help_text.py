from qq_bot.services.help_text import build_help_text


def test_help_text_lists_supported_commands() -> None:
    text = build_help_text(ai_prefix="ai")

    assert "基础命令" in text
    assert "/help" in text
    assert "/帮助" in text
    assert "/version" in text
    assert "/版本" in text
    assert "洛克查询" in text
    assert "/精灵 迪莫" in text
    assert "/洛克 迪莫" in text
    assert "/技能 闪光" in text
    assert "@机器人 迪莫" in text
    assert "捕捉计数" in text
    assert "/计数 迪莫" in text
    assert "/计数 异色 迪莫" in text
    assert "按当前群、当前用户、当前赛季分别统计" in text
    assert "/ping" not in text
    assert "/状态" not in text
    assert "AI 对话" not in text
    assert "群聊记忆" not in text
    assert "联网搜索" not in text
    assert "配置功能" not in text


def test_help_text_uses_configured_ai_prefix() -> None:
    text = build_help_text(ai_prefix="ask")

    assert "ask" not in text
