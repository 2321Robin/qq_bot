# Roco Pet Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and send a local PNG pet card for successful Rock Kingdom World pet lookups.

**Architecture:** Keep lookup/data concerns in `roco_pets.py`, add a focused Pillow renderer in `roco_pet_cards.py`, and keep the NoneBot plugin responsible only for command flow and fallback behavior. The plugin sends image messages for found pets and falls back to existing text if rendering fails.

**Tech Stack:** Python 3.11, NoneBot2, OneBot v11 `MessageSegment`, Pillow, pytest, pytest-asyncio, Ruff.

---

## File Map

- Modify `pyproject.toml`: add Pillow runtime dependency.
- Modify `data/roco_pets.json`: add card fields to at least the existing records, with complete sample card data for `迪莫`.
- Modify `src/qq_bot/services/roco_pets.py`: extend `PetRecord`, parse optional card fields, and preserve text formatting.
- Create `src/qq_bot/services/roco_pet_cards.py`: render a `PetRecord` as PNG bytes with Pillow.
- Modify `src/qq_bot/plugins/roco.py`: generate and send image messages for successful lookups with text fallback.
- Modify `tests/test_roco_pets.py`: cover new fields and compatibility.
- Create `tests/test_roco_pet_cards.py`: cover PNG generation and missing optional fields.
- Modify `tests/test_roco_plugin.py`: cover image response and fallback response.
- Modify `README.md`: document that `/精灵` now returns an image card for found pets.

---

### Task 1: Extend Pet Data Model

**Files:**
- Modify: `src/qq_bot/services/roco_pets.py`
- Modify: `tests/test_roco_pets.py`
- Modify: `data/roco_pets.json`

- [ ] **Step 1: Write failing tests for optional card fields**

Update `tests/test_roco_pets.py` imports and add this test below `test_load_pet_records_reads_local_json_data`:

```python
def test_load_pet_records_reads_card_fields() -> None:
    records = load_pet_records(Path("data/roco_pets.json"))
    dimo = next(record for record in records if record.name == "迪莫")

    assert dimo.height_weight == "5.5~7KG"
    assert dimo.body_length == "0.54~0.78M"
    assert dimo.favorite_partner == "最好的伙伴"
    assert dimo.description == "造成翼制伤害后，获得攻防速+20%，并回复2能量"
    assert dimo.race_value == 582
    assert dimo.stats == {
        "hp": 120,
        "physical_attack": 80,
        "magic_attack": 80,
        "physical_defense": 105,
        "magic_defense": 105,
        "speed": 92,
    }
```

Add this test below `test_format_pet_record_includes_evolution_condition_and_source`:

```python
def test_format_pet_record_still_works_without_card_fields() -> None:
    record = PetRecord(
        name="测试宠物",
        aliases=[],
        number="999",
        attributes=["光"],
        stage="Ⅰ阶",
        evolution_chain=["测试宠物"],
        evolution_condition="暂无普通等级进化条件。",
        source_url="https://example.com/pet",
    )

    text = format_pet_record(record)

    assert "测试宠物" in text
    assert "编号：999" in text
    assert "进化条件：暂无普通等级进化条件。" in text
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python -m pytest tests/test_roco_pets.py -v`

Expected: FAIL because `PetRecord` has no `height_weight`, `body_length`, `favorite_partner`, `description`, `race_value`, or `stats` fields.

- [ ] **Step 3: Extend `PetRecord` and JSON parsing**

In `src/qq_bot/services/roco_pets.py`, replace the `PetRecord` dataclass with:

```python
@dataclass(frozen=True)
class PetRecord:
    name: str
    aliases: list[str]
    number: str
    attributes: list[str]
    stage: str
    evolution_chain: list[str]
    evolution_condition: str
    source_url: str
    height_weight: str = ""
    body_length: str = ""
    favorite_partner: str = ""
    description: str = ""
    race_value: int | None = None
    stats: dict[str, int] | None = None
```

Update `_record_from_item` to pass the new fields:

```python
def _record_from_item(item: Any) -> PetRecord:
    if not isinstance(item, dict):
        raise ValueError("roco pet records must be objects")
    return PetRecord(
        name=_string_value(item, "name"),
        aliases=_string_list(item, "aliases"),
        number=_string_value(item, "number"),
        attributes=_string_list(item, "attributes"),
        stage=_string_value(item, "stage"),
        evolution_chain=_string_list(item, "evolution_chain"),
        evolution_condition=_string_value(item, "evolution_condition"),
        source_url=_string_value(item, "source_url"),
        height_weight=_string_value(item, "height_weight"),
        body_length=_string_value(item, "body_length"),
        favorite_partner=_string_value(item, "favorite_partner"),
        description=_string_value(item, "description"),
        race_value=_optional_int(item, "race_value"),
        stats=_stats_value(item),
    )
```

Add these helper functions after `_string_list`:

```python
def _optional_int(item: dict[str, Any], key: str) -> int | None:
    value = item.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _stats_value(item: dict[str, Any]) -> dict[str, int] | None:
    value = item.get("stats")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("stats must be an object")

    stats: dict[str, int] = {}
    for key, stat_value in value.items():
        if not isinstance(key, str):
            raise ValueError("stats keys must be strings")
        if not isinstance(stat_value, int):
            raise ValueError("stats values must be integers")
        stats[key] = stat_value
    return stats
```

- [ ] **Step 4: Add card fields to `迪莫` record**

In `data/roco_pets.json`, update the `迪莫` object to include these fields after `source_url`:

```json
    "source_url": "https://lokewangguoshijie.com/",
    "height_weight": "5.5~7KG",
    "body_length": "0.54~0.78M",
    "favorite_partner": "最好的伙伴",
    "description": "造成翼制伤害后，获得攻防速+20%，并回复2能量",
    "race_value": 582,
    "stats": {
      "hp": 120,
      "physical_attack": 80,
      "magic_attack": 80,
      "physical_defense": 105,
      "magic_defense": 105,
      "speed": 92
    }
```

Ensure the preceding JSON commas remain valid.

- [ ] **Step 5: Run model tests**

Run: `.\.venv\Scripts\python -m pytest tests/test_roco_pets.py -v`

Expected: PASS.

- [ ] **Step 6: Commit data model changes**

```bash
git add src/qq_bot/services/roco_pets.py tests/test_roco_pets.py data/roco_pets.json
git commit -m "feat: add roco pet card fields"
```

---

### Task 2: Add PNG Card Renderer

**Files:**
- Modify: `pyproject.toml`
- Create: `src/qq_bot/services/roco_pet_cards.py`
- Create: `tests/test_roco_pet_cards.py`

- [ ] **Step 1: Write failing renderer tests**

Create `tests/test_roco_pet_cards.py`:

```python
from qq_bot.services.roco_pet_cards import render_pet_card_png
from qq_bot.services.roco_pets import PetRecord


def test_render_pet_card_png_returns_png_bytes() -> None:
    record = PetRecord(
        name="迪莫",
        aliases=["小迪莫"],
        number="001",
        attributes=["光"],
        stage="最终形态",
        evolution_chain=["迪莫"],
        evolution_condition="图鉴显示为最终形态；暂无普通等级进化条件。",
        source_url="https://example.com/dimo",
        height_weight="5.5~7KG",
        body_length="0.54~0.78M",
        favorite_partner="最好的伙伴",
        description="造成翼制伤害后，获得攻防速+20%，并回复2能量",
        race_value=582,
        stats={
            "hp": 120,
            "physical_attack": 80,
            "magic_attack": 80,
            "physical_defense": 105,
            "magic_defense": 105,
            "speed": 92,
        },
    )

    image = render_pet_card_png(record)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1_000


def test_render_pet_card_png_handles_missing_optional_fields() -> None:
    record = PetRecord(
        name="测试宠物",
        aliases=[],
        number="999",
        attributes=[],
        stage="Ⅰ阶",
        evolution_chain=["测试宠物"],
        evolution_condition="暂无普通等级进化条件。",
        source_url="https://example.com/pet",
    )

    image = render_pet_card_png(record)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
```

- [ ] **Step 2: Run renderer tests to verify failure**

Run: `.\.venv\Scripts\python -m pytest tests/test_roco_pet_cards.py -v`

Expected: FAIL because `qq_bot.services.roco_pet_cards` does not exist.

- [ ] **Step 3: Add Pillow dependency**

In `pyproject.toml`, add Pillow to `[project].dependencies`:

```toml
  "pillow>=10.0.0,<12.0.0",
```

Place it near the other runtime dependencies.

- [ ] **Step 4: Install updated editable dependencies**

Run: `.\.venv\Scripts\python -m pip install -e ".[dev]"`

Expected: command exits 0 and Pillow is installed.

- [ ] **Step 5: Implement renderer module**

Create `src/qq_bot/services/roco_pet_cards.py`:

```python
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from qq_bot.services.roco_pets import PetRecord


CARD_WIDTH = 700
CARD_HEIGHT = 765
BACKGROUND = "#2c2f33"
PANEL = "#3b3f44"
PILL = "#202326"
TEXT = "#f4f4f4"
MUTED = "#c9ccd1"
ORANGE = "#d8742b"
GOLD = "#ffad2f"
BAR_BG = "#1c1f22"

STAT_ROWS = [
    ("hp", "生命"),
    ("physical_attack", "物攻"),
    ("magic_attack", "魔攻"),
    ("physical_defense", "物防"),
    ("magic_defense", "魔防"),
    ("speed", "速度"),
]


def render_pet_card_png(record: PetRecord) -> bytes:
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _load_font(32)
    large_font = _load_font(30)
    normal_font = _load_font(24)
    small_font = _load_font(20)
    tiny_font = _load_font(18)

    _rounded(draw, (25, 25, 675, 341), 44, PANEL)
    _rounded(draw, (46, 46, 654, 166), 60, PILL)
    _draw_avatar(draw, record, title_font)
    _draw_top_info(draw, record, title_font, normal_font, small_font)
    _draw_description(draw, record, normal_font, small_font)

    _rounded(draw, (25, 361, 675, 681), 44, PANEL)
    _draw_stats(draw, record, large_font, normal_font, small_font)

    footer_font = small_font
    draw.text((134, 712), "生成自群机器人 @小呱呱", fill=TEXT, font=footer_font)
    draw.text((380, 712), "数据来源：roco.cn", fill=TEXT, font=footer_font)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_avatar(draw: ImageDraw.ImageDraw, record: PetRecord, font: ImageFont.ImageFont) -> None:
    center = (118, 106)
    radius = 48
    attribute = record.attributes[0] if record.attributes else "?"
    fill = _attribute_color(attribute)
    draw.ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
        fill=fill,
        outline=MUTED,
        width=3,
    )
    initial = record.name[:1] or "?"
    _center_text(draw, initial, center, font, TEXT)


def _draw_top_info(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    title_font: ImageFont.ImageFont,
    normal_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    _rounded(draw, (186, 68, 279, 101), 16, ORANGE)
    _center_text(draw, _value(record.number), (233, 84), small_font, "#202326")
    draw.text((292, 67), record.name or "未知", fill=TEXT, font=title_font)

    attr_text = "、".join(record.attributes) if record.attributes else "未知"
    _rounded(draw, (376, 72, 433, 98), 13, "#34383d")
    _center_text(draw, attr_text[:2], (404, 85), small_font, TEXT)

    _rounded(draw, (185, 112, 329, 141), 15, PILL)
    draw.text((205, 115), "⚖", fill=ORANGE, font=small_font)
    draw.text((236, 115), _value(record.height_weight), fill=MUTED, font=small_font)

    _rounded(draw, (346, 112, 526, 141), 15, PILL)
    draw.text((367, 115), "📏", fill=ORANGE, font=small_font)
    draw.text((410, 115), _value(record.body_length), fill=MUTED, font=small_font)


def _draw_description(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    normal_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    _rounded(draw, (64, 164, 180, 194), 15, GOLD)
    _center_text(draw, _value(record.favorite_partner), (122, 179), small_font, "#202326")
    _rounded(draw, (46, 184, 654, 252), 34, PILL)
    draw.text((78, 211), _value(record.description), fill=TEXT, font=normal_font)
    _rounded(draw, (316, 274, 385, 308), 17, PILL, outline=ORANGE)
    _center_text(draw, record.name or "未知", (350, 291), small_font, TEXT)


def _draw_stats(
    draw: ImageDraw.ImageDraw,
    record: PetRecord,
    large_font: ImageFont.ImageFont,
    normal_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    _rounded(draw, (47, 383, 653, 425), 21, PILL)
    draw.text((76, 391), "种族值", fill=TEXT, font=normal_font)
    race_value = str(record.race_value) if record.race_value is not None else "未知"
    draw.text((564, 386), race_value, fill=GOLD, font=large_font)

    stats = record.stats or {}
    max_value = 180
    y = 445
    for key, label in STAT_ROWS:
        value = stats.get(key)
        display_value = str(value) if value is not None else "未知"
        draw.text((78, y - 4), label, fill=TEXT, font=normal_font)
        _rounded(draw, (161, y, 569, y + 22), 11, BAR_BG)
        if value is not None and value > 0:
            width = max(12, min(408, int(408 * value / max_value)))
            _rounded(draw, (161, y, 161 + width, y + 22), 11, ORANGE)
        draw.text((585, y - 6), display_value, fill=MUTED, font=normal_font)
        y += 38


def _rounded(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: str,
    *,
    outline: str | None = None,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2 if outline else 1)


def _center_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    center: tuple[int, int],
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text((center[0] - width / 2, center[1] - height / 2 - 2), text, fill=fill, font=font)


def _value(value: str) -> str:
    return value or "未知"


def _attribute_color(attribute: str) -> str:
    return {
        "光": "#4f8edc",
        "草": "#4a9b5d",
        "火": "#d86a38",
        "水": "#3f84c8",
    }.get(attribute, "#64748b")


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()
```

- [ ] **Step 6: Run renderer tests**

Run: `.\.venv\Scripts\python -m pytest tests/test_roco_pet_cards.py -v`

Expected: PASS.

- [ ] **Step 7: Commit renderer changes**

```bash
git add pyproject.toml src/qq_bot/services/roco_pet_cards.py tests/test_roco_pet_cards.py
git commit -m "feat: render roco pet cards"
```

---

### Task 3: Send Card Images From Plugin

**Files:**
- Modify: `src/qq_bot/plugins/roco.py`
- Modify: `tests/test_roco_plugin.py`

- [ ] **Step 1: Update plugin tests for image output and fallback**

In `tests/test_roco_plugin.py`, change `FinishCalled` to accept any object:

```python
class FinishCalled(Exception):
    def __init__(self, message: object):
        self.message = message
```

Change all `fake_finish` signatures in this file from `message: str` to `message: object`.

Update `test_roco_pet_command_replies_with_local_pet` assertions to:

```python
    message = exc_info.value.message
    assert "image" in str(message)
```

Update `test_roco_mention_lookup_replies_when_pet_exists` assertions to:

```python
    message = exc_info.value.message
    assert "image" in str(message)
```

Add this test before `test_roco_mention_lookup_returns_when_pet_missing`:

```python
@pytest.mark.asyncio
async def test_roco_pet_command_falls_back_to_text_when_card_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_finish(message: object) -> None:
        raise FinishCalled(message)

    def fake_render_card(record: object) -> bytes:
        raise RuntimeError("render failed")

    monkeypatch.setattr(roco_plugin, "get_settings", lambda: BotSettings(allowed_group_ids="1001"))
    monkeypatch.setattr(roco_plugin, "render_pet_card_png", fake_render_card)
    monkeypatch.setattr(roco_plugin.roco_pet_command, "finish", fake_finish)

    with pytest.raises(FinishCalled) as exc_info:
        await roco_plugin.handle_roco_pet(FakeEvent(), FakeArgs("迪莫"))  # type: ignore[arg-type]

    assert "迪莫" in str(exc_info.value.message)
    assert "进化条件" in str(exc_info.value.message)
```

- [ ] **Step 2: Run plugin tests to verify failure**

Run: `.\.venv\Scripts\python -m pytest tests/test_roco_plugin.py -v`

Expected: FAIL because the plugin still sends plain text for found pets and does not import `render_pet_card_png`.

- [ ] **Step 3: Update plugin implementation**

Replace imports at the top of `src/qq_bot/plugins/roco.py` with:

```python
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from qq_bot.config import get_settings
from qq_bot.services.roco_pet_cards import render_pet_card_png
from qq_bot.services.roco_pets import (
    find_pet,
    format_pet_query_result,
    format_pet_record,
    get_pet_records,
)
```

Replace `handle_roco_pet` body after `query = ...` with:

```python
    records = get_pet_records()
    record = find_pet(records, query)
    if record is None:
        await roco_pet_command.finish(format_pet_query_result(query, records))

    await roco_pet_command.finish(_format_pet_card_message(record))
```

Replace the final line of `handle_roco_mention_pet` with:

```python
    await roco_mention_pet.finish(_format_pet_card_message(record))
```

Add this helper at the end of the file:

```python
def _format_pet_card_message(record: object) -> MessageSegment | str:
    if not hasattr(record, "name"):
        return "本地图鉴暂时没有收录该精灵。"
    try:
        image = render_pet_card_png(record)  # type: ignore[arg-type]
    except Exception:
        return format_pet_record(record)  # type: ignore[arg-type]
    return MessageSegment.image(image)
```

Then refine the helper signature by importing `PetRecord` from `roco_pets` and replacing the helper with:

```python
def _format_pet_card_message(record: PetRecord) -> MessageSegment | str:
    try:
        image = render_pet_card_png(record)
    except Exception:
        return format_pet_record(record)
    return MessageSegment.image(image)
```

- [ ] **Step 4: Run plugin tests**

Run: `.\.venv\Scripts\python -m pytest tests/test_roco_plugin.py -v`

Expected: PASS.

- [ ] **Step 5: Commit plugin changes**

```bash
git add src/qq_bot/plugins/roco.py tests/test_roco_plugin.py
git commit -m "feat: send roco pet card images"
```

---

### Task 4: Documentation And Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README feature text**

In `README.md`, replace the feature bullet:

```markdown
- `/精灵 迪莫` or `/洛克 迪莫`: Query the local Rock Kingdom World pet database, including evolution conditions.
```

with:

```markdown
- `/精灵 迪莫` or `/洛克 迪莫`: Query the local Rock Kingdom World pet database and return a static pet card image for found pets.
```

Replace the manual verification bullet:

```markdown
- Send `/精灵 迪莫` in an allowed group and expect local pet data with evolution conditions.
```

with:

```markdown
- Send `/精灵 迪莫` in an allowed group and expect a static pet card image. Send `/精灵 不存在` and expect a not-found text reply.
```

- [ ] **Step 2: Run full test suite**

Run: `.\.venv\Scripts\python -m pytest -v`

Expected: all tests PASS.

- [ ] **Step 3: Run Ruff**

Run: `.\.venv\Scripts\python -m ruff check .`

Expected: exits 0 with no lint errors.

- [ ] **Step 4: Verify bot imports**

Run: `.\.venv\Scripts\python -c "import bot; import qq_bot.plugins.roco"`

Expected: exits 0.

- [ ] **Step 5: Commit docs and final verification state**

```bash
git add README.md
git commit -m "docs: describe roco pet card lookup"
```

---

## Self-Review

- Spec coverage: data model fields are implemented in Task 1, rendering is implemented in Task 2, bot behavior and fallback are implemented in Task 3, dependency and verification are covered in Tasks 2 and 4.
- Placeholder scan: the plan contains no TBD, TODO, or deferred implementation placeholders.
- Type consistency: `PetRecord`, `render_pet_card_png(record: PetRecord) -> bytes`, and plugin helper `_format_pet_card_message(record: PetRecord) -> MessageSegment | str` are consistent across tasks.
