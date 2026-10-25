from qq_bot.config import BotSettings
from qq_bot.services.persona import (
    Persona,
    casual_system_prompt,
    load_persona,
    mentions_persona,
)


def _persona(**overrides) -> Persona:
    defaults = {"name": "小洛", "aliases": ("洛洛",), "prompt": "说话简短随意"}
    defaults.update(overrides)
    return Persona(**defaults)


def test_load_persona_reads_settings() -> None:
    settings = BotSettings(persona_name=" 小洛 ", persona_aliases="洛洛,圈圈")
    persona = load_persona(settings)
    assert persona.name == "小洛"
    assert persona.aliases == ("洛洛", "圈圈")
    assert persona.prompt == settings.effective_persona_prompt


def test_mentions_persona_matches_name_and_aliases() -> None:
    persona = _persona()
    assert mentions_persona("小洛 在吗", persona) is True
    assert mentions_persona("问问洛洛", persona) is True
    assert mentions_persona("随便聊聊", persona) is False


def test_mentions_persona_false_when_name_empty() -> None:
    assert mentions_persona("随便聊聊", _persona(name="", aliases=())) is False


def test_casual_system_prompt_contains_persona_and_constraints() -> None:
    prompt = casual_system_prompt(_persona())
    assert "小洛" in prompt
    assert "说话简短随意" in prompt
    assert "十几个字" in prompt  # 长度弹性：短为主但允许变化
    assert "不要编造" in prompt


def test_casual_system_prompt_anti_formula_rules() -> None:
    prompt = casual_system_prompt(_persona())
    assert "句末不加句号" in prompt  # 群聊没有句末标点
    assert "表情" in prompt  # 表情稀缺化
    assert "语气词" in prompt  # 语气词不得连用
    assert "感叹号" in prompt  # 长度/感叹号是一上限而非一刀切


def test_casual_system_prompt_requires_context_followup() -> None:
    """2026-10-25 复盘 + 部署者裁决：问题是不接上文，不是某句话本身——
    “你喊我干嘛”在合适语境里可以说，禁具体短语是把措辞当根因，过度矫正。
    只钉“接上文”要求，并用否断言钉住“不得出现短语级禁令”。"""
    prompt = casual_system_prompt(_persona())
    assert "接话必须接上文" in prompt
    assert "别装作被点名" in prompt
    assert "你喊我干嘛" not in prompt


def test_casual_system_prompt_works_without_name() -> None:
    prompt = casual_system_prompt(_persona(name="", aliases=()))
    assert "说话简短随意" in prompt


def test_casual_system_prompt_encourages_nickname_address() -> None:
    """S11-AUTO-04（部署者裁决 2026-10-25）：prompt 带真实群昵称后，模型可
    自然称呼群友；字母代称仍禁用（回落场景的兜底规则保留）。"""
    prompt = casual_system_prompt(_persona())
    assert "用昵称自然称呼对方" in prompt
    assert "绝对不要用它们或任何字母称呼群友" in prompt
