import json
from http.client import RemoteDisconnected
from urllib.error import HTTPError

from scripts.fetch_roco_pet_detail import (
    fetch_pet_details,
    fetch_html,
    load_bwiki_index_targets,
    load_fetch_targets,
    write_pet_detail,
)
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
    assert detail["metadata"]["parser_version"] == 5


def test_parse_pet_detail_uses_empty_values_for_missing_fields() -> None:
    detail = parse_pet_detail("https://example.com/pet", "<html><body><h1>测试宠物</h1></body></html>")

    assert detail["name"] == "测试宠物"
    assert detail["attributes"] == []
    assert detail["evolution_condition"] == ""
    assert detail["profile"] == {}
    assert detail["stats"] == {}
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

    assert detail["evolution_condition"] == "由雪绒鸟（夏天的样子）亲密度进化"
    assert "进化条件" not in detail["profile"]


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
    pets_path = tmp_path / "roco_pets.json"
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


def test_fetch_pet_details_skips_existing_files_unless_force_is_enabled(tmp_path) -> None:
    calls: list[str] = []

    def fake_fetch_html(url: str) -> str:
        calls.append(url)
        return "<html><body><h1>喵喵</h1></body></html>"

    existing_path = tmp_path / "002-喵喵.json"
    existing_path.write_text(
        json.dumps({"name": "old", "metadata": {"parser_version": 5}}, ensure_ascii=False),
        encoding="utf-8",
    )

    errors = fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
    )

    assert errors == []
    assert calls == []
    assert json.loads(existing_path.read_text(encoding="utf-8"))["name"] == "old"

    fetch_pet_details(
        [("喵喵", "https://wiki.biligame.com/rocom/喵喵")],
        tmp_path,
        fetch_html_func=fake_fetch_html,
        force=True,
    )

    assert calls == ["https://wiki.biligame.com/rocom/喵喵"]
    assert json.loads(existing_path.read_text(encoding="utf-8"))["name"] == "喵喵"


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
    )

    assert errors == []
    assert attempts == [
        "https://wiki.biligame.com/rocom/喵喵",
        "https://wiki.biligame.com/rocom/喵喵",
    ]
    assert json.loads((tmp_path / "喵喵.json").read_text(encoding="utf-8"))["name"] == "喵喵"
