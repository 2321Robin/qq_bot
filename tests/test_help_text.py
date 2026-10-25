from qq_bot.services.help_text import build_help_text


def test_help_text_lists_every_command_surface() -> None:
    """覆盖审查（2026-10-26 部署者裁决）：/help 必须列全所有指令分组。"""
    text = build_help_text()

    # 基础
    assert "基础命令" in text
    assert "/help 或 /帮助" in text
    assert "/version 或 /版本" in text
    # AI 问答（只列 @入口；ai 前缀是部署者配置，不进群友帮助——2026-10-26 部署者裁决）
    assert "AI 问答" in text
    assert "@机器人 <问题>" in text
    assert "ai <问题>" not in text
    # 早晚报
    assert "/早报" in text
    assert "/晚报" in text
    assert "/AI早报" in text
    # 个人记忆
    assert "/记忆保存" in text
    assert "/记忆查看" in text
    assert "/记忆删除" in text
    assert "/记忆关闭" in text
    # 洛克查询（开关开时）
    assert "洛克查询" in text
    assert "/精灵 迪莫" in text
    assert "/洛克 迪莫" in text
    assert "/技能 闪光" in text
    # 管理员
    assert "/表达学习" in text
    assert "/表达 列表" in text
    assert "/黑话 列表" in text
    assert "/配额" in text
    assert "/最近故障" in text
    # 不存在的指令绝不出现
    assert "/计数" not in text
    assert "/捕捉计数" not in text
    assert "/ping" not in text
    assert "/状态" not in text


def test_help_text_omits_roco_block_when_disabled() -> None:
    text = build_help_text(roco_enabled=False)

    assert "洛克" not in text
    assert "精灵" not in text
    assert "/help 或 /帮助" in text
    # 洛克关掉不影响其余分组
    assert "/早报" in text
    assert "/记忆保存" in text
    assert "/表达学习" in text
