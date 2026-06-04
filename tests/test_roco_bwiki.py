import json
from http.client import RemoteDisconnected
from urllib.error import HTTPError

from scripts.fetch_roco_pet_detail import (
    fetch_pet_details,
    fetch_html,
    load_bwiki_index_target_records,
    load_bwiki_index_targets,
    load_fetch_targets,
    main,
    write_pet_detail,
)
from qq_bot.services.roco_bwiki import parse_pet_detail
from qq_bot.services.roco_evolution import normalize_pet_details


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
    assert detail["total_race_value"] == 582
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
    assert detail["evolution_edges"] == []
    assert detail["metadata"]["parser_version"] == 6


def test_parse_pet_detail_extracts_raw_template_page() -> None:
    raw = """
    {{精灵信息
    |精灵名称=迪莫
    |精灵初阶名称=迪莫
    |精灵阶段=最终形态
    |精灵类型=永远的伙伴
    |精灵描述=勇气的伙伴。
    |主属性=光
    |2属性=
    |特性=最好的伙伴
    |特性描述=造成克制伤害后，获得攻防速+20%，并回复2能量。
    |生命=120
    |物攻=80
    |魔攻=80
    |物防=105
    |魔防=105
    |速度=92
    |体型=0.54~0.78
    |重量=5.5~7
    |技能=猛烈撞击,闪光,防御
    |技能解锁等级=1,1,1
    |血脉技能=星星撞击
    |可学技能石=借用
    |进化条件=无法进化
    }}
    """

    detail = parse_pet_detail("https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB?action=raw", raw)

    assert detail["name"] == "迪莫"
    assert detail["source_url"] == "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"
    assert detail["attributes"] == ["光"]
    assert detail["evolution_condition"] == "无法进化"
    assert detail["profile"]["初阶"] == "迪莫"
    assert detail["profile"]["阶段"] == "最终形态"
    assert detail["profile"]["体长"] == "0.54~0.78M"
    assert detail["profile"]["体重"] == "5.5~7KG"
    assert detail["stats"] == {
        "生命": 120,
        "物攻": 80,
        "魔攻": 80,
        "物防": 105,
        "魔防": 105,
        "速度": 92,
    }
    assert detail["total_race_value"] == 582
    assert detail["skills"][0]["rows"][:3] == [
        {"等级": "LV1", "技能": "猛烈撞击", "耗能": "", "类型": "", "威力": "", "效果": ""},
        {"等级": "LV1", "技能": "闪光", "耗能": "", "类型": "", "威力": "", "效果": ""},
        {"等级": "LV1", "技能": "防御", "耗能": "", "类型": "", "威力": "", "效果": ""},
    ]
    assert detail["skills"][1]["rows"][0]["技能"] == "星星撞击"
    assert detail["skills"][2]["rows"][0]["技能"] == "借用"


def test_parse_pet_detail_uses_empty_values_for_missing_fields() -> None:
    detail = parse_pet_detail("https://example.com/pet", "<html><body><h1>测试宠物</h1></body></html>")

    assert detail["name"] == "测试宠物"
    assert detail["attributes"] == []
    assert detail["evolution_condition"] == ""
    assert detail["profile"] == {}
    assert detail["stats"] == {}
    assert detail["evolution_edges"] == []
    assert detail["total_race_value"] is None
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
        <div class="rocom_sprite_info_total"><p>种族值</p><p>582</p></div>
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
        <div class="rocom_sprite_info_physique">
          <li>
            <div class="rocom_sprite_info_physique_icon">
              <img alt="图标 宠物 体质 身高.png" />
            </div>
            <p>0.54~0.78</p><p class="font-runeregular">M</p>
          </li>
          <li>
            <div class="rocom_sprite_info_physique_icon">
              <img alt="图标 宠物 体质 体重.png" />
            </div>
            <p>5.5~7</p><p class="font-runeregular">KG</p>
          </li>
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
    assert "进化条件" not in detail["profile"]
    assert detail["profile"]["体长"] == "0.54~0.78M"
    assert detail["profile"]["体重"] == "5.5~7KG"
    assert detail["stats"] == {"生命": 120, "物攻": 80}
    assert detail["total_race_value"] == 582
    assert detail["skills"] == [
        {
            "source": "技能",
            "rows": [
                {
                    "等级": "初始",
                    "技能": "闪光冲击",
                    "耗能": "一星",
                    "类型": "光",
                    "威力": "40",
                    "效果": "造成光系伤害。",
                }
            ],
        }
    ]


def test_parse_pet_detail_extracts_component_trait() -> None:
    html = """
    <html>
      <body>
        <h1>迪莫</h1>
        <div class="rocom_sprite_trait">
          <p>特性</p>
          <p>最好的伙伴</p>
          <p>造成克制伤害后，获得攻防速+20%，并回复2能量</p>
        </div>
        <div>精灵属性</div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/dimo", html)

    assert detail["profile"]["最佳拍档"] == "最好的伙伴"
    assert detail["profile"]["简介"] == "造成克制伤害后，获得攻防速+20%，并回复2能量"


def test_parse_pet_detail_skips_repeated_component_trait_name() -> None:
    html = """
    <html>
      <body>
        <h1>霜翼领主</h1>
        <div class="rocom_sprite_trait">
          <p>特性</p>
          <p>破空</p>
          <p>破空</p>
          <p>若先于敌方攻击，本次技能威力+75%。</p>
        </div>
        <div>精灵属性</div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/frostwing", html)

    assert detail["profile"]["最佳拍档"] == "破空"
    assert detail["profile"]["简介"] == "若先于敌方攻击，本次技能威力+75%。"


def test_parse_pet_detail_ignores_empty_component_physique_values() -> None:
    html = """
    <html>
      <body>
        <h1>圣光迪莫</h1>
        <div class="rocom_sprite_info_physique">
          <li>
            <div class="rocom_sprite_info_physique_icon">
              <img alt="图标 宠物 体质 身高.png" />
            </div>
            <p class="mw-empty-elt"></p><p class="font-runeregular">M</p>
          </li>
          <li>
            <div class="rocom_sprite_info_physique_icon">
              <img alt="图标 宠物 体质 体重.png" />
            </div>
            <p class="mw-empty-elt"></p><p class="font-runeregular">KG</p>
          </li>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)

    assert "体长" not in detail["profile"]
    assert "体重" not in detail["profile"]


def test_parse_pet_detail_extracts_multiple_component_attributes() -> None:
    html = """
    <html>
      <body>
        <h1>寒音蛇（本来的样子）</h1>
        <div class="rocom_sprite_grament_attributes_text"><p>萌</p></div>
        <div class="rocom_sprite_grament_attributes_text"><p>毒</p></div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)

    assert detail["attributes"] == ["萌", "毒"]
    assert detail["profile"]["系别"] == "萌、毒"


def test_parse_pet_detail_describes_level_evolution_from_component_chain() -> None:
    html = """
    <html>
      <body>
        <h1>寒音蛇（本来的样子）</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1">
            <a href="/rocom/source" title="古钟蛇（本来的样子）">source</a>
          </div>
          <div class="rocom_spirit_evolution_level">
            <p class="rocom_spirit_evolution_level_num">38</p>
          </div>
          <div class="rocom_spirit_evolution_2">
            <a href="/rocom/current" title="寒音蛇（本来的样子）">target</a>
          </div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)
    assert detail["evolution_edges"] == [
        {
            "source": "古钟蛇（本来的样子）",
            "target": "寒音蛇（本来的样子）",
            "condition": "升至38级",
            "raw_condition": "38",
            "forward_text": "升至38级可进化为寒音蛇（本来的样子）",
            "backward_text": "可由古钟蛇（本来的样子）升至38级进化得",
        }
    ]

    assert detail["evolution_condition"] == "由古钟蛇（本来的样子）等级38级进化"
    assert "进化条件" not in detail["profile"]


def test_parse_pet_detail_describes_battle_evolution_from_component_chain() -> None:
    html = """
    <html>
      <body>
        <h1>秩序鱿墨</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1">
            <a href="/rocom/source" title="墨鱿士">source</a>
          </div>
          <div class="rocom_spirit_evolution_level">
            <p class="rocom_spirit_evolution_level_num">打败三只恶系精灵</p>
          </div>
          <div class="rocom_spirit_evolution_2">
            <a href="/rocom/current" title="秩序鱿墨">target</a>
          </div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)
    assert detail["evolution_edges"][0]["condition"] == "击败3个恶系精灵"
    assert detail["evolution_edges"][0]["forward_text"] == "击败3个恶系精灵可进化为秩序鱿墨"

    assert detail["evolution_condition"] == "由墨鱿士击败3个恶系精灵进化"
    assert "进化条件" not in detail["profile"]


def test_parse_pet_detail_preserves_non_level_evolution_text_from_component_chain() -> None:
    html = """
    <html>
      <body>
        <h1>冬羽雀（夏天的样子）</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1">
            <a href="/rocom/source" title="雪绒鸟（夏天的样子）">source</a>
          </div>
          <div class="rocom_spirit_evolution_level">
            <p class="rocom_spirit_evolution_level_num">亲密度进化</p>
          </div>
          <div class="rocom_spirit_evolution_2">
            <a href="/rocom/current" title="冬羽雀（夏天的样子）">target</a>
          </div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)
    assert detail["evolution_edges"][0]["condition"] == "亲密度进化"
    assert detail["evolution_edges"][0]["backward_text"] == "可由雪绒鸟（夏天的样子）亲密度进化得"
    assert detail["evolution_condition"] == "由雪绒鸟（夏天的样子）亲密度进化"
    assert "进化条件" not in detail["profile"]



def test_parse_pet_detail_extracts_middle_form_evolution_edges() -> None:
    html = """
    <html>
      <body>
        <h1>喵呜</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1"><a href="/rocom/喵喵" title="喵喵">喵喵</a></div>
          <div class="rocom_spirit_evolution_level"><p class="rocom_spirit_evolution_level_num">16</p></div>
          <div class="rocom_spirit_evolution_2"><a href="/rocom/喵呜" title="喵呜">喵呜</a></div>
        </div>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1"><a href="/rocom/喵呜" title="喵呜">喵呜</a></div>
          <div class="rocom_spirit_evolution_level"><p class="rocom_spirit_evolution_level_num">36</p></div>
          <div class="rocom_spirit_evolution_2"><a href="/rocom/魔力猫" title="魔力猫">魔力猫</a></div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)

    assert detail["evolution_condition"] == "由喵喵等级16级进化"
    assert detail["evolution_edges"] == [
        {
            "source": "喵喵",
            "target": "喵呜",
            "condition": "升至16级",
            "raw_condition": "16",
            "forward_text": "升至16级可进化为喵呜",
            "backward_text": "可由喵喵升至16级进化得",
        },
        {
            "source": "喵呜",
            "target": "魔力猫",
            "condition": "升至36级",
            "raw_condition": "36",
            "forward_text": "升至36级可进化为魔力猫",
            "backward_text": "可由喵呜升至36级进化得",
        },
    ]



def test_parse_pet_detail_extracts_branching_component_edges() -> None:
    html = """
    <html>
      <body>
        <h1>分支宠物</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1"><a title="源宠物">源宠物</a></div>
          <div class="rocom_spirit_evolution_level"><p class="rocom_spirit_evolution_level_num">20</p></div>
          <div class="rocom_spirit_evolution_2"><a title="分支甲">分支甲</a></div>
          <div class="rocom_spirit_evolution_2"><a title="分支乙">分支乙</a></div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)

    assert detail["evolution_edges"] == [
        {
            "source": "源宠物",
            "target": "分支甲",
            "condition": "升至20级",
            "raw_condition": "20",
            "forward_text": "升至20级可进化为分支甲",
            "backward_text": "可由源宠物升至20级进化得",
        },
        {
            "source": "源宠物",
            "target": "分支乙",
            "condition": "升至20级",
            "raw_condition": "20",
            "forward_text": "升至20级可进化为分支乙",
            "backward_text": "可由源宠物升至20级进化得",
        },
    ]


def test_parse_pet_detail_preserves_named_battle_evolution_edge() -> None:
    html = """
    <html>
      <body>
        <h1>黑棋主教</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1"><a title="棋骑士（白子）">source</a></div>
          <div class="rocom_spirit_evolution_level"><p class="rocom_spirit_evolution_level_num">击败三次棋骑士（黑子）</p></div>
          <div class="rocom_spirit_evolution_2"><a title="黑棋主教">target</a></div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)

    assert detail["evolution_condition"] == "由棋骑士（白子）击败三次棋骑士（黑子）"
    assert detail["evolution_edges"] == [
        {
            "source": "棋骑士（白子）",
            "target": "黑棋主教",
            "condition": "击败三次棋骑士（黑子）",
            "raw_condition": "击败三次棋骑士（黑子）",
            "forward_text": "击败三次棋骑士（黑子）可进化为黑棋主教",
            "backward_text": "可由棋骑士（白子）击败三次棋骑士（黑子）进化得",
        }
    ]


def test_parse_pet_detail_uses_anchor_text_when_evolution_title_is_missing() -> None:
    html = """
    <html>
      <body>
        <h1>目标宠物</h1>
        <div class="rocom_spirit_evolution_box">
          <div class="rocom_spirit_evolution_1"><a>来源宠物</a></div>
          <div class="rocom_spirit_evolution_level"><p class="rocom_spirit_evolution_level_num">使用道具</p></div>
          <div class="rocom_spirit_evolution_2"><a>目标宠物</a></div>
        </div>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/pet", html)

    assert detail["evolution_condition"] == "由来源宠物使用道具"
    assert detail["evolution_edges"][0]["raw_condition"] == "使用道具"


def test_normalize_pet_details_builds_bidirectional_middle_form_text() -> None:
    details = [
        {"name": "喵喵", "evolution_condition": "", "evolution_edges": []},
        {"name": "喵呜", "evolution_condition": "由喵喵等级16级进化", "evolution_edges": []},
        {"name": "魔力猫", "evolution_condition": "由喵呜等级36级进化", "evolution_edges": []},
    ]

    normalize_pet_details(details)

    assert details[0]["evolution_condition"] == "升至16级可进化为喵呜"
    assert details[0]["evolution"]["evolution_condition"] == "升至16级可进化为喵呜"
    assert details[1]["evolution_condition"] == "可由喵喵升至16级进化得；升至36级可进化为魔力猫"
    assert details[1]["evolution"]["evolution_condition"] == "可由喵喵升至16级进化得；升至36级可进化为魔力猫"
    assert details[2]["evolution_condition"] == "可由喵呜升至36级进化得"
    assert details[2]["evolution"]["evolution_condition"] == "可由喵呜升至36级进化得"

    first_normalized = json.loads(json.dumps(details, ensure_ascii=False))

    normalize_pet_details(details)

    assert details == first_normalized
    assert details[1]["evolution"]["from"][0]["source"] == "喵喵"
    assert details[1]["evolution"]["to"][0]["target"] == "魔力猫"


def test_normalize_pet_details_uses_parser_edges_for_non_level_condition() -> None:
    details = [
        {"name": "雪绒鸟", "evolution_condition": "", "evolution_edges": []},
        {
            "name": "冬羽雀",
            "evolution_condition": "",
            "evolution_edges": [
                {
                    "source": "雪绒鸟",
                    "target": "冬羽雀",
                    "condition": "亲密度进化",
                    "raw_condition": "亲密度进化",
                    "forward_text": "亲密度进化可进化为冬羽雀",
                    "backward_text": "可由雪绒鸟亲密度进化得",
                }
            ],
        },
    ]

    normalize_pet_details(details)

    assert details[0]["evolution_condition"] == "亲密度进化可进化为冬羽雀"
    assert details[0]["evolution"]["evolution_condition"] == "亲密度进化可进化为冬羽雀"
    assert details[1]["evolution_condition"] == "可由雪绒鸟亲密度进化得"
    assert details[1]["evolution"]["evolution_condition"] == "可由雪绒鸟亲密度进化得"


def test_normalize_pet_details_keeps_explicit_final_note_as_compatibility_fallback() -> None:
    details = [{"name": "迪莫", "evolution_condition": "无法进化", "evolution_edges": []}]

    normalize_pet_details(details)

    assert details[0]["evolution_condition"] == "无法进化"
    assert details[0]["evolution"] == {"from": [], "to": [], "evolution_condition": "无法进化"}


def test_normalize_pet_details_infers_sourceless_branch_conditions_from_previous_form() -> None:
    details = [
        {"name": "画精灵", "evolution_condition": "", "evolution_edges": []},
        {"name": "画像守护", "evolution_condition": "由画精灵等级16级进化", "evolution_edges": []},
        {"name": "画间法师手", "evolution_condition": "击败三只武系精灵", "evolution_edges": []},
        {"name": "画间沉铁兽", "evolution_condition": "击败三只幻系精灵", "evolution_edges": []},
    ]

    normalize_pet_details(details)

    assert details[1]["evolution"]["evolution_condition"] == (
        "可由画精灵升至16级进化得；"
        "击败3个武系精灵可进化为画间法师手；"
        "击败3个幻系精灵可进化为画间沉铁兽"
    )
    assert details[2]["evolution_condition"] == "可由画像守护击败3个武系精灵进化得"
    assert details[3]["evolution_condition"] == "可由画像守护击败3个幻系精灵进化得"


def test_parse_pet_detail_sums_stats_when_total_race_value_is_missing() -> None:
    html = """
    <html>
      <body>
        <h1>迪莫</h1>
        <table>
          <tr><th>生命</th><th>物攻</th><th>魔攻</th><th>物防</th><th>魔防</th><th>速度</th></tr>
          <tr><td>120</td><td>80</td><td>80</td><td>105</td><td>105</td><td>92</td></tr>
        </table>
      </body>
    </html>
    """

    detail = parse_pet_detail("https://example.com/dimo", html)

    assert detail["total_race_value"] == 582


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
                    "耗能": "一星",
                    "类型": "光",
                    "威力": "40",
                    "效果": "造成光系伤害。",
                },
                {
                    "等级": "10",
                    "技能": "光芒护盾",
                    "耗能": "二星",
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
        "total_race_value": 582,
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
    assert saved["total_race_value"] == 582


def test_fetch_html_uses_browser_headers(monkeypatch) -> None:
    captured_headers: dict[str, str] = {}

    class FakeHeaders:
        def get_content_charset(self) -> str:
            return "utf-8"

    class FakeResponse:
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b"<html></html>"

    def fake_urlopen(request, timeout):
        assert timeout == 30
        captured_headers.update(dict(request.header_items()))
        return FakeResponse()

    monkeypatch.setattr("scripts.fetch_roco_pet_detail.urlopen", fake_urlopen)

    assert fetch_html("https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB") == "<html></html>"
    assert "Mozilla/5.0" in captured_headers["User-agent"]
    assert "zh-CN" in captured_headers["Accept-language"]


def test_fetch_html_falls_back_to_curl_when_urlopen_is_blocked(monkeypatch) -> None:
    captured_command: list[str] = []

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 567, "Unknown Status", {}, None)

    class FakeCompletedProcess:
        returncode = 0
        stdout = "<html>备用抓取成功</html>"
        stderr = ""

    def fake_run(command, capture_output, text, timeout, encoding, errors):
        captured_command.extend(command)
        assert capture_output is True
        assert text is True
        assert timeout == 30
        assert encoding == "utf-8"
        assert errors == "replace"
        return FakeCompletedProcess()

    monkeypatch.setattr("scripts.fetch_roco_pet_detail.urlopen", fake_urlopen)
    monkeypatch.setattr("scripts.fetch_roco_pet_detail.subprocess.run", fake_run)

    assert fetch_html("https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB") == "<html>备用抓取成功</html>"
    assert captured_command[:4] == ["curl.exe", "-L", "--retry", "2"]
    assert captured_command[-1] == "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"


def test_load_fetch_targets_uses_only_bwiki_source_urls(tmp_path) -> None:
    pets_path = tmp_path / "fetch_targets.json"
    pets_path.write_text(
        json.dumps(
            [
                {
                    "name": "迪莫",
                    "source_url": "https://lokewangguoshijie.com/",
                },
                {
                    "name": "喵喵",
                    "source_url": "https://wiki.biligame.com/rocom/喵喵",
                },
                {
                    "name": "火花",
                    "source_url": "https://wiki.biligame.com/rocom/%E7%81%AB%E8%8A%B1",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    targets = load_fetch_targets(pets_path)

    assert targets == [
        ("喵喵", "https://wiki.biligame.com/rocom/%E5%96%B5%E5%96%B5"),
        ("火花", "https://wiki.biligame.com/rocom/%E7%81%AB%E8%8A%B1"),
    ]


def test_fetch_pet_details_writes_successes_and_continues_after_failure(tmp_path) -> None:
    def fake_fetch_html(url: str) -> str:
        if url.endswith("fail"):
            raise TimeoutError("slow page")
        pet_name = url.rsplit("/", maxsplit=1)[-1]
        return f"<html><body><h1>{pet_name}</h1></body></html>"

    errors = fetch_pet_details(
        [
            ("喵喵", "https://wiki.biligame.com/rocom/喵喵"),
            ("失败宠物", "https://wiki.biligame.com/rocom/fail"),
            ("火花", "https://wiki.biligame.com/rocom/火花"),
        ],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        normalize=False,
    )

    assert [error[0] for error in errors] == ["失败宠物"]
    assert "slow page" in errors[0][2]
    assert json.loads((tmp_path / "喵喵.json").read_text(encoding="utf-8"))["name"] == "喵喵"
    assert json.loads((tmp_path / "火花.json").read_text(encoding="utf-8"))["name"] == "火花"
    assert not (tmp_path / "失败宠物.json").exists()


def test_fetch_pet_details_writes_numbered_filename_when_number_is_available(tmp_path) -> None:
    def fake_fetch_html(url: str) -> str:
        assert url == "https://wiki.biligame.com/rocom/喵喵"
        return """
        <html><body>
          <h1>喵喵</h1>
          <div class="rocom_sprite_grament_name">002 喵喵</div>
        </body></html>
        """

    errors = fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        normalize=False,
    )

    assert errors == []
    assert json.loads((tmp_path / "002-喵喵.json").read_text(encoding="utf-8"))["name"] == "喵喵"
    assert not (tmp_path / "喵喵.json").exists()


def test_fetch_pet_details_continues_after_remote_disconnect(tmp_path) -> None:
    def fake_fetch_html(url: str) -> str:
        if url.endswith("disconnect"):
            raise RemoteDisconnected("remote closed")
        return "<html><body><h1>喵喵</h1></body></html>"

    errors = fetch_pet_details(
        [
            ("断开宠物", "https://wiki.biligame.com/rocom/disconnect"),
            ("喵喵", "https://wiki.biligame.com/rocom/喵喵"),
        ],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        normalize=False,
    )

    assert [error[0] for error in errors] == ["断开宠物"]
    assert json.loads((tmp_path / "喵喵.json").read_text(encoding="utf-8"))["name"] == "喵喵"
    assert not (tmp_path / "断开宠物.json").exists()


def test_load_bwiki_index_targets_extracts_all_pet_names() -> None:
    html = """
    <html><body>
      <table>
        <tr>
          <th>精灵</th><th>精灵名称</th><th>属性</th><th>精灵编号</th><th>总种族值</th>
        </tr>
        <tr><td></td><td>迪莫</td><td>光</td><td>001</td><td>582</td></tr>
        <tr><td></td><td>圣光迪莫</td><td>光</td><td>001</td><td>531</td></tr>
        <tr><td></td><td>鸭吉吉（蓬松的样子）</td><td>普通</td><td>011</td><td>471</td></tr>
      </table>
    </body></html>
    """

    targets = load_bwiki_index_targets(html)

    assert targets == [
        ("迪莫", "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"),
        ("圣光迪莫", "https://wiki.biligame.com/rocom/%E5%9C%A3%E5%85%89%E8%BF%AA%E8%8E%AB"),
        (
            "鸭吉吉（蓬松的样子）",
            "https://wiki.biligame.com/rocom/%E9%B8%AD%E5%90%89%E5%90%89%EF%BC%88%E8%93%AC%E6%9D%BE%E7%9A%84%E6%A0%B7%E5%AD%90%EF%BC%89",
        ),
    ]


def test_load_bwiki_index_target_records_preserves_index_metadata() -> None:
    html = """
    <html><body>
      <table>
        <tr>
          <th>精灵</th><th>精灵名称</th><th>属性</th><th>精灵编号</th><th>总种族值</th>
        </tr>
        <tr><td></td><td>迪莫</td><td>光</td><td>001</td><td>582</td></tr>
      </table>
    </body></html>
    """

    targets = load_bwiki_index_target_records(html)

    assert targets == [
        (
            "迪莫",
            "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB",
            {"精灵": "", "精灵名称": "迪莫", "属性": "光", "精灵编号": "001", "总种族值": "582"},
        )
    ]


def test_fetch_pet_details_skips_existing_files_unless_force_is_enabled(tmp_path) -> None:
    calls: list[str] = []

    def fake_fetch_html(url: str) -> str:
        calls.append(url)
        return "<html><body><h1>喵喵</h1></body></html>"

    existing_path = tmp_path / "002-喵喵.json"
    existing_path.write_text(
        json.dumps({"name": "old", "metadata": {"parser_version": 6}}, ensure_ascii=False),
        encoding="utf-8",
    )

    errors = fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        normalize=False,
    )

    assert errors == []
    assert calls == []
    assert json.loads(existing_path.read_text(encoding="utf-8"))["name"] == "old"

    fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        force=True,
        normalize=False,
    )

    assert calls == ["https://wiki.biligame.com/rocom/喵喵"]
    assert json.loads(existing_path.read_text(encoding="utf-8"))["name"] == "喵喵"


def test_fetch_pet_details_normalizes_after_successful_refresh(tmp_path) -> None:
    def fake_fetch_html(url: str) -> str:
        name = url.rsplit("/", maxsplit=1)[-1]
        if name == "喵喵":
            return "<html><body><h1>喵喵</h1></body></html>"
        return """
        <html><body>
          <h1>喵呜</h1>
          <div class="rocom_spirit_evolution_box">
            <div class="rocom_spirit_evolution_1"><a title="喵喵">喵喵</a></div>
            <div class="rocom_spirit_evolution_level"><p class="rocom_spirit_evolution_level_num">16</p></div>
            <div class="rocom_spirit_evolution_2"><a title="喵呜">喵呜</a></div>
          </div>
        </body></html>
        """

    errors = fetch_pet_details(
        [
            ("喵喵", "https://wiki.biligame.com/rocom/喵喵"),
            ("喵呜", "https://wiki.biligame.com/rocom/喵呜"),
        ],
        tmp_path,
        fetch_html_func=fake_fetch_html,
    )

    assert errors == []
    source = json.loads((tmp_path / "喵喵.json").read_text(encoding="utf-8"))
    target = json.loads((tmp_path / "喵呜.json").read_text(encoding="utf-8"))
    assert source["evolution_condition"] == "升至16级可进化为喵呜"
    assert source["evolution"]["evolution_condition"] == "升至16级可进化为喵呜"
    assert target["evolution"]["from"][0]["text"] == "可由喵喵升至16级进化得"


def test_main_can_normalize_existing_detail_directory(tmp_path, monkeypatch) -> None:
    (tmp_path / "喵喵.json").write_text(
        json.dumps({"name": "喵喵", "evolution_condition": "", "evolution_edges": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "喵呜.json").write_text(
        json.dumps(
            {
                "name": "喵呜",
                "evolution_condition": "由喵喵等级16级进化",
                "evolution_edges": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.fetch_roco_pet_detail.sys.argv", ["fetch_roco_pet_detail.py", "--normalize-only", "--output-dir", str(tmp_path)])

    assert main() == 0

    normalized = json.loads((tmp_path / "喵喵.json").read_text(encoding="utf-8"))
    assert normalized["evolution"]["evolution_condition"] == "升至16级可进化为喵呜"


def test_fetch_pet_details_refreshes_outdated_parser_version(tmp_path) -> None:
    calls: list[str] = []

    def fake_fetch_html(url: str) -> str:
        calls.append(url)
        return "<html><body><h1>喵喵</h1></body></html>"

    existing_path = tmp_path / "喵喵.json"
    existing_path.write_text(
        json.dumps({"name": "old", "metadata": {"parser_version": 1}}, ensure_ascii=False),
        encoding="utf-8",
    )

    errors = fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        min_parser_version=3,
        normalize=False,
    )

    assert errors == []
    assert calls == ["https://wiki.biligame.com/rocom/喵喵"]
    saved = json.loads(existing_path.read_text(encoding="utf-8"))
    assert saved["name"] == "喵喵"


def test_fetch_pet_details_retries_transient_failures(tmp_path) -> None:
    attempts: list[str] = []

    def fake_fetch_html(url: str) -> str:
        attempts.append(url)
        if len(attempts) == 1:
            raise TimeoutError("temporary")
        return "<html><body><h1>喵喵</h1></body></html>"

    errors = fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        retries=1,
        normalize=False,
    )

    assert errors == []
    assert attempts == [
        "https://wiki.biligame.com/rocom/喵喵",
        "https://wiki.biligame.com/rocom/喵喵",
    ]
    assert json.loads((tmp_path / "喵喵.json").read_text(encoding="utf-8"))["name"] == "喵喵"


def test_fetch_pet_details_can_fetch_raw_pages_and_apply_index_metadata(tmp_path) -> None:
    calls: list[str] = []

    def fake_fetch_html(url: str) -> str:
        calls.append(url)
        return """
        {{精灵信息
        |精灵名称=迪莫
        |主属性=光
        |生命=120
        |物攻=80
        |魔攻=80
        |物防=105
        |魔防=105
        |速度=92
        |进化条件=无法进化
        }}
        """

    errors = fetch_pet_details(
        [
            (
                "迪莫",
                "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB",
                {"精灵编号": "001", "总种族值": "582"},
            )
        ],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        normalize=False,
        use_raw_pages=True,
    )

    assert errors == []
    assert calls == ["https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB?action=raw"]
    detail = json.loads((tmp_path / "001-迪莫.json").read_text(encoding="utf-8"))
    assert detail["name"] == "迪莫"
    assert detail["source_url"] == "https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB"
    assert detail["profile"]["编号"] == "001"
    assert detail["total_race_value"] == 582
