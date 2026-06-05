from qq_bot.services.roco_knowledge import build_roco_context
from qq_bot.services.roco_pets import EvolutionRelation, PetRecord
from qq_bot.services.roco_skills import SkillRecord


def pet(
    name: str,
    *,
    condition: str = "",
    chain: list[str] | None = None,
    number: str = "001",
    attributes: list[str] | None = None,
    evolution_from: list[EvolutionRelation] | None = None,
    evolution_to: list[EvolutionRelation] | None = None,
) -> PetRecord:
    return PetRecord(
        name=name,
        aliases=[],
        number=number,
        attributes=attributes or ["普通"],
        stage="未知",
        evolution_chain=chain or [name],
        evolution_condition=condition,
        source_url=f"https://example.com/{name}",
        evolution_from=evolution_from or [],
        evolution_to=evolution_to or [],
    )


def test_roco_context_returns_empty_for_unrelated_chat() -> None:
    context = build_roco_context(
        "今天晚饭吃什么？",
        pet_records=[pet("画精灵")],
        skill_records=[SkillRecord("加固", "LV1", "2", "状态", "0", "提升防御。", "画精灵")],
    )

    assert context == ""


def test_roco_context_describes_outgoing_evolution_from_local_records() -> None:
    records = [
        pet("画精灵", chain=["画精灵"], number="299"),
        pet("画像守护", condition="由画精灵等级16级进化", chain=["画像守护"], number="300"),
    ]

    context = build_roco_context("画精灵怎么进化？", pet_records=records, skill_records=[])

    assert "问题类型：进化" in context
    assert "匹配精灵：画精灵" in context
    assert "画精灵 -> 画像守护" in context
    assert "- 画像守护：由画精灵等级16级进化" in context


def test_roco_context_includes_full_downstream_evolution_routes() -> None:
    records = [
        pet(
            "画精灵",
            chain=["画精灵"],
            number="299",
            evolution_to=[
                EvolutionRelation("画精灵", "画像守护", "升至16级", "升至16级可进化为画像守护")
            ],
        ),
        pet(
            "画像守护",
            condition="可由画精灵升至16级进化得；达到36级并击败3个武系精灵可进化为画间法师手；达到36级并击败3个幻系精灵可进化为画间沉铁兽",
            chain=["画像守护"],
            number="300",
            evolution_from=[
                EvolutionRelation("画精灵", "画像守护", "升至16级", "可由画精灵升至16级进化得")
            ],
            evolution_to=[
                EvolutionRelation(
                    "画像守护",
                    "画间法师手",
                    "达到36级并击败3个武系精灵",
                    "达到36级并击败3个武系精灵可进化为画间法师手",
                ),
                EvolutionRelation(
                    "画像守护",
                    "画间沉铁兽",
                    "达到36级并击败3个幻系精灵",
                    "达到36级并击败3个幻系精灵可进化为画间沉铁兽",
                ),
            ],
        ),
        pet(
            "画间法师手",
            chain=["画间法师手"],
            number="301",
            evolution_from=[
                EvolutionRelation(
                    "画像守护",
                    "画间法师手",
                    "达到36级并击败3个武系精灵",
                    "可由画像守护达到36级并击败3个武系精灵进化得",
                )
            ],
        ),
        pet(
            "画间沉铁兽",
            chain=["画间沉铁兽"],
            number="302",
            evolution_from=[
                EvolutionRelation(
                    "画像守护",
                    "画间沉铁兽",
                    "达到36级并击败3个幻系精灵",
                    "可由画像守护达到36级并击败3个幻系精灵进化得",
                )
            ],
        ),
    ]

    context = build_roco_context("画精灵怎么进化？", pet_records=records, skill_records=[])

    assert "完整进化路线：" in context
    assert "- 画精灵 -> 画像守护：升至16级" in context
    assert "- 画像守护 -> 画间法师手：达到36级并击败3个武系精灵" in context
    assert "- 画像守护 -> 画间沉铁兽：达到36级并击败3个幻系精灵" in context



def test_roco_context_matches_pet_by_number_in_evolution_question() -> None:
    records = [
        pet(
            "钨丝贝贝",
            condition="提升为1星可进化为辉光幕机",
            chain=["钨丝贝贝", "辉光幕机"],
            number="348",
        ),
        pet("辉光幕机", condition="可由钨丝贝贝提升为1星进化得", chain=["钨丝贝贝", "辉光幕机"], number="349"),
    ]

    context = build_roco_context("序号348怎么进化？", pet_records=records, skill_records=[])

    assert "匹配精灵：钨丝贝贝" in context
    assert "进化条件：提升为1星可进化为辉光幕机" in context

def test_roco_context_describes_how_target_evolves_from_local_record() -> None:
    records = [
        pet("画精灵", chain=["画精灵"], number="299"),
        pet("画间沉铁兽", condition="击败三只幻系精灵", chain=["画精灵", "画间沉铁兽"], number="302"),
    ]

    context = build_roco_context("画间沉铁兽是怎么进化得到的？", pet_records=records, skill_records=[])

    assert "匹配精灵：画间沉铁兽" in context
    assert "进化链：画精灵 -> 画间沉铁兽" in context
    assert "进化条件：击败三只幻系精灵" in context
    assert "上一形态：画精灵" in context


def test_roco_context_uses_skill_pet_set_intersection() -> None:
    skill_records = [
        SkillRecord("加固", "LV1", "2", "状态", "0", "提升防御。", "精灵甲"),
        SkillRecord("加固", "LV1", "2", "状态", "0", "提升防御。", "精灵乙"),
        SkillRecord("除厄", "LV1", "2", "魔攻", "60", "造成伤害。", "精灵乙"),
        SkillRecord("除厄", "LV1", "2", "魔攻", "60", "造成伤害。", "精灵丙"),
    ]

    context = build_roco_context(
        "什么精灵既能学习加固技能又能学习除厄技能？",
        pet_records=[],
        skill_records=skill_records,
    )

    assert "问题类型：技能交集" in context
    assert "匹配技能：加固、除厄" in context
    assert "同时可学习精灵：精灵乙" in context
    assert "精灵甲" in context
    assert "精灵丙" in context


def test_roco_context_reports_missing_record_for_roco_question() -> None:
    context = build_roco_context("不存在精灵怎么进化？", pet_records=[], skill_records=[])

    assert "本地洛克王国资料暂时没有找到" in context
    assert "不要凭模型记忆补全" in context
