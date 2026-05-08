def build_help_text(ai_prefix: str) -> str:
    prefix = ai_prefix.strip() or "ai"
    return "\n".join(
        [
            "可用功能：",
            "/help - 查看帮助",
            "/ping - 检查机器人是否在线",
            f"{prefix} 你好 - 向 AI 提问",
            "定时任务 - 由配置文件控制自动发送",
        ]
    )
