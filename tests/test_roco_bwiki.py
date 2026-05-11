import json

from scripts.fetch_roco_pet_detail import write_pet_detail
from qq_bot.services.roco_bwiki import parse_pet_detail


def test_parse_pet_detail_extracts_profile_stats_and_skill_groups() -> None:
    html = """
    <html>
      <head><title>迪莫 - 洛克王国世界WIKI_BWIKI</title></head>
      <body>
        <h1>迪莫</h1>
        <table>
          <tr><th>编号</th><td>001</td><th>系别</th><td>光</td></tr>
          <tr><th>进化条件</th><td>图鉴显示为最终形态；暂无普通等级进化条件。</td></tr>
          <tr><th>身高体重</th><td>5.5~7KG</td><th>体长</th><td>0.54~0.78M</td></tr>
        </table>
        <h2>种族值</h2>
        <table>
          <tr><th>精力</th><th>物攻</th><th>魔攻</th><th>物防</th><th>魔防</th><th>速度</th></tr>
          <tr><td>120</td><td>80</td><td>80</td><td>105</td><td>105</td><td>92</td></tr>
        </table>
        <h2>本身就有</h2>
        <table>
          <tr><th>技能名</th><th>属性</th><th>威力</th><th>说明</th></tr>
          <tr><td>闪光冲击</td><td>光</td><td>40</td><td>造成光系伤害。</td></tr>
        </table>
        <h2>技能石学习</h2>
        <table>
          <tr><th>技能名</th><th>属性</th><th>威力</th><th>说明</th></tr>
          <tr><td>圣光守护</td><td>光</td><td>-</td><td>提升防御。</td></tr>
        </table>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB", html)

    assert detail["name"] == "迪莫"
    assert detail["source_url"] == "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"
    assert detail["attributes"] == ["光"]
    assert detail["evolution_condition"] == "图鉴显示为最终形态；暂无普通等级进化条件。"
    assert detail["profile"]["编号"] == "001"
    assert detail["profile"]["身高体重"] == "5.5~7KG"
    assert detail["profile"]["体长"] == "0.54~0.78M"
    assert detail["stats"] == {
        "精力": 120,
        "物攻": 80,
        "魔攻": 80,
        "物防": 105,
        "魔防": 105,
        "速度": 92,
    }
    assert detail["skills"] == [
        {
            "source": "本身就有",
            "rows": [
                {"技能名": "闪光冲击", "属性": "光", "威力": "40", "说明": "造成光系伤害。"}
            ],
        },
        {
            "source": "技能石学习",
            "rows": [
                {"技能名": "圣光守护", "属性": "光", "威力": "-", "说明": "提升防御。"}
            ],
        },
    ]
    assert detail["metadata"]["parser_version"] == 1


def test_parse_pet_detail_uses_empty_values_for_missing_fields() -> None:
    detail = parse_pet_detail("https://example.com/pet", "<html><body><h1>测试宠物</h1></body></html>")

    assert detail["name"] == "测试宠物"
    assert detail["attributes"] == []
    assert detail["evolution_condition"] == ""
    assert detail["profile"] == {}
    assert detail["stats"] == {}
    assert detail["skills"] == []


def test_parse_pet_detail_extracts_profile_text_from_nested_tables() -> None:
    html = """
    <html>
      <body>
        <h1>嵌套宠物</h1>
        <table>
          <tr>
            <th>编号</th>
            <td><table><tr><td>001</td></tr></table></td>
          </tr>
          <tr><th>系别</th><td>光</td></tr>
          <tr><th>体长</th><td><span>0.54M</span></td></tr>
        </table>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/nested", html)

    assert detail["profile"]["编号"] == "001"
    assert detail["profile"]["系别"] == "光"
    assert detail["profile"]["体长"] == "0.54M"
    assert detail["attributes"] == ["光"]


def test_parse_pet_detail_extracts_real_page_component_layout() -> None:
    html = """
    <html>
      <body>
        <h1>迪莫</h1>
        <div class="rocom_sprite_grament_name">001 迪莫</div>
        <div class="rocom_sprite_grament_attributes_text">光</div>
        <div>进化条件:<span class="rocom_evolution_data">无法进化</span></div>
        <div class="rocom_sprite_info_qualification">
          <div>
            <span class="rocom_sprite_info_qualification_name">生命</span>
            <span class="rocom_sprite_info_qualification_value">120</span>
          </div>
          <div>
            <span class="rocom_sprite_info_qualification_name">物攻</span>
            <span class="rocom_sprite_info_qualification_value">80</span>
          </div>
        </div>
        <div class="rocom_sprite_skill_box">
          <span class="rocom_sprite_skill_level">初始</span>
          <span class="rocom_sprite_skillName">闪光冲击</span>
          <span class="rocom_sprite_skillDamage">一星</span>
          <span class="rocom_sprite_skillType">光</span>
          <span class="rocom_sprite_skill_power">40</span>
          <span class="rocom_sprite_skillContent">造成光系伤害。</span>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB", html)

    assert detail["attributes"] == ["光"]
    assert detail["evolution_condition"] == "无法进化"
    assert detail["profile"]["编号"] == "001"
    assert detail["profile"]["系别"] == "光"
    assert detail["profile"]["进化条件"] == "无法进化"
    assert detail["stats"] == {"生命": 120, "物攻": 80}
    assert detail["skills"] == [
        {
            "source": "技能",
            "rows": [
                {
                    "等级": "初始",
                    "技能": "闪光冲击",
                    "星级": "一星",
                    "类型": "光",
                    "威力": "40",
                    "效果": "造成光系伤害。",
                }
            ],
        }
    ]


def test_parse_pet_detail_deduplicates_repeated_component_skill_boxes() -> None:
    html = """
    <html>
      <body>
        <h1>迪莫</h1>
        <div class="rocom_sprite_skill_box">
          <span class="rocom_sprite_skill_level">初始</span>
          <span class="rocom_sprite_skillName">闪光冲击</span>
          <span class="rocom_sprite_skillDamage">一星</span>
          <span class="rocom_sprite_skillType">光</span>
          <span class="rocom_sprite_skill_power">40</span>
          <span class="rocom_sprite_skillContent">造成光系伤害。</span>
        </div>
        <div class="rocom_sprite_skill_box">
          <span class="rocom_sprite_skill_level">初始</span>
          <span class="rocom_sprite_skillName">闪光冲击</span>
          <span class="rocom_sprite_skillDamage">一星</span>
          <span class="rocom_sprite_skillType">光</span>
          <span class="rocom_sprite_skill_power">40</span>
          <span class="rocom_sprite_skillContent">造成光系伤害。</span>
        </div>
        <div class="rocom_sprite_skill_box">
          <span class="rocom_sprite_skill_level">10</span>
          <span class="rocom_sprite_skillName">光芒护盾</span>
          <span class="rocom_sprite_skillDamage">二星</span>
          <span class="rocom_sprite_skillType">光</span>
          <span class="rocom_sprite_skill_power">60</span>
          <span class="rocom_sprite_skillContent">提高自身防御。</span>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/dimo", html)

    assert detail["skills"] == [
        {
            "source": "技能",
            "rows": [
                {
                    "等级": "初始",
                    "技能": "闪光冲击",
                    "星级": "一星",
                    "类型": "光",
                    "威力": "40",
                    "效果": "造成光系伤害。",
                },
                {
                    "等级": "10",
                    "技能": "光芒护盾",
                    "星级": "二星",
                    "类型": "光",
                    "威力": "60",
                    "效果": "提高自身防御。",
                },
            ],
        },
    ]


def test_write_pet_detail_creates_parent_and_writes_utf8_json(tmp_path) -> None:
    detail = {
        "name": "迪莫",
        "source_url": "https://example.com/dimo",
        "attributes": ["光"],
        "evolution_condition": "暂无普通等级进化条件。",
        "profile": {"编号": "001"},
        "stats": {},
        "skills": [],
        "metadata": {"parser_version": 1},
    }
    output_path = tmp_path / "nested" / "迪莫.json"

    write_pet_detail(detail, output_path)

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["name"] == "迪莫"
    assert saved["attributes"] == ["光"]
