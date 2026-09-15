"""Persona resolution (S7-AUTO): the config-driven bot identity shared by
the autonomous chat path and the @-addressed prompt injection."""

from __future__ import annotations

from dataclasses import dataclass

from qq_bot.config import BotSettings


@dataclass(frozen=True)
class Persona:
    name: str
    aliases: tuple[str, ...]
    prompt: str


def load_persona(settings: BotSettings) -> Persona:
    return Persona(
        name=settings.persona_name.strip(),
        aliases=settings.persona_alias_list,
        prompt=settings.effective_persona_prompt,
    )


def mentions_persona(text: str, persona: Persona) -> bool:
    """True when the message addresses the bot by its configured nickname."""
    if not persona.name:
        return False
    return any(name and name in text for name in (persona.name, *persona.aliases))


def casual_system_prompt(persona: Persona) -> str:
    """System prompt for the casual ("群友模式") generator. Output is plain
    text — the JSON claims/evidence format belongs to the Q&A path only."""
    name_part = f"，群友叫你“{persona.name}”" if persona.name else ""
    return (
        f"你在QQ群里以真实群友的身份聊天{name_part}。{persona.prompt}。"
        "回复要求：1~2 个口语短句，一般不超过 30 个字；可以用不完整句、语气词和表情；"
        "禁止列点，禁止“首先/其次/总之”，禁止书面腔和客服腔。"
        "不确定的事就含糊带过，不要编造事实、链接、时间或价格。"
        "除非被直接问到，不要主动说自己是机器人或 AI。"
        "直接输出要发送的那句话，不要任何前缀、引号或解释。"
    )
