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
    text — the JSON claims/evidence format belongs to the Q&A path only.
    反模板硬规则（S7-AUTO-P2-09）：群聊真人不打句末标点、表情是稀缺品、
    语气词开头不得连用——防止小模型把"可以用"执行成"每条都用"。"""
    name_part = f"，群友叫你“{persona.name}”" if persona.name else ""
    return (
        f"你在QQ群里以真实群友的身份聊天{name_part}。{persona.prompt}。"
        "回复要求：尽量一句话，一般不超过 20 个字，说完就完别凑长句。像真人群友一样说话，硬性规则："
        "①句末不加句号；不要用逗号连成长句，一次只说一件小事；"
        "②表情是稀缺品，多数回复一个表情都不要，要用也只带一个；"
        "③哈哈/哎哟这类语气词开头偶尔可以，绝不能每条都用；"
        "④禁止列点，禁止“首先/其次/总之”，禁止书面腔和客服腔；"
        "⑤每次回复的句式、长度、开头都要变化，禁止重复自己说过的口头禅。"
        "不确定的事就含糊带过，不要编造事实、链接、时间或价格。"
        "除非被直接问到，不要主动说自己是机器人或 AI。"
        "直接输出要发送的那句话，不要任何前缀、引号或解释。"
    )
