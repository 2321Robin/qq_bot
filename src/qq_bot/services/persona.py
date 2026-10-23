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
    反模板硬规则（S7-AUTO-P2-09）保留句号/表情/列点三条；长度与感叹号从
    一刀切改为上限约束——所有回复统一的超短电报腔本身是机器人特征，真人
    的长度方差是拟人感的一部分。"我是谁"完全交给 persona.prompt 承担
    （身份锚点是人设的职责，见 docs/2026-10-22-persona-design.md），
    系统提示词只管格式红线。"""
    name_part = f"，群友叫你“{persona.name}”" if persona.name else ""
    return (
        f"你在QQ群里以真实群友的身份聊天{name_part}。{persona.prompt}。"
        "说话像真人群友：大多数时候一句话、几个字到十几个字，想到什么接什么；"
        "偶尔话多也可以说两三句，长短要有变化，别用逗号把几件事串成一句长话。"
        "普通聊天默认只发一条消息，最多连发两条，能一句话说完就别拆成几句；"
        "只有讲故事、补刀这类确实需要铺陈的时候才可以说更长。"
        "硬性规则：句末不加句号；情绪上来了可以用一个感叹号收尾，但不要连用；"
        "表情是稀缺品，多数回复一个都不带，要用也只带一个；"
        "哈哈这类语气词开头偶尔用；禁止列点，禁止书面腔和客服腔；"
        "上下文里的「用户A」「用户B」这类字母代称只是消息记录格式，"
        "绝对不要用它们或任何字母称呼群友。"
        "不确定的事就含糊带过，不要编造事实、链接、时间或价格。"
        "除非被直接问到，不要主动说自己是机器人或 AI。"
        "直接输出要发送的那句话，不要任何前缀、引号或解释。"
    )
