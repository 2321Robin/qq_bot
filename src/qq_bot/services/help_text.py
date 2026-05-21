def build_help_text(ai_prefix: str) -> str:
    prefix = ai_prefix.strip() or "ai"
    return "\n".join(
        [
            "可用功能：",
            "/help - 查看帮助",
            "/ping - 检查机器人是否在线",
            "/精灵 迪莫 - 查询洛克王国世界精灵图鉴和进化条件",
            "/技能 闪光 - 查询洛克王国世界技能效果和可用精灵",
            "/计数 迪莫 或 /计数 异色 迪莫 - 记录 S2 精灵捕捉数量",
            f"{prefix} 你好 - 向 AI 提问",
            "定时任务 - 由配置文件控制自动发送",
        ]
    )
