"""视觉输入/输出桥（S11-VISION-01）单测：消息段提取、QQ表情名映射、
出站发图标记解析。全部纯数据转换，不依赖 nonebot。"""

from pathlib import Path
from types import SimpleNamespace

from qq_bot.services.vision_extract import (
    QA_FACE_WHITELIST,
    QQ_FACE_NAMES,
    VisionInputs,
    append_attachment_tags,
    extract_vision_inputs,
    face_id_for_name,
    parse_reply_parts,
    strip_reply_markers,
)


def segment(seg_type: str, **data: object) -> SimpleNamespace:
    return SimpleNamespace(type=seg_type, data=data)


# ---- 入站提取：image ----


def test_extract_image_url_passes_through() -> None:
    vision = extract_vision_inputs(
        [segment("image", file="abc.jpg", url="https://multimedia.example/abc.jpg")],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.images == ("https://multimedia.example/abc.jpg",)
    assert vision.image_segments == 1
    assert vision.dropped_images == 0


def test_extract_image_caps_at_max_images() -> None:
    urls = [f"https://multimedia.example/{i}.jpg" for i in range(3)]
    vision = extract_vision_inputs(
        [segment("image", url=url) for url in urls],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.images == tuple(urls[:2])
    assert vision.image_segments == 3
    assert vision.dropped_images == 1


def test_extract_image_local_file_becomes_data_url(tmp_path: Path) -> None:
    image_path = tmp_path / "sticker.png"
    image_path.write_bytes(b"\x89PNG fake bytes")
    vision = extract_vision_inputs(
        [segment("image", file=str(image_path))],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.images
    assert vision.images[0].startswith("data:image/png;base64,")


def test_extract_image_missing_local_file_is_dropped(tmp_path: Path) -> None:
    vision = extract_vision_inputs(
        [segment("image", file=str(tmp_path / "gone.jpg"))],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.images == ()
    assert vision.dropped_images == 1


def test_extract_image_url_missing_is_dropped() -> None:
    vision = extract_vision_inputs(
        [segment("image", file="abc.jpg")],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.images == ()
    assert vision.dropped_images == 1


def test_extract_disabled_keeps_counts_but_no_sources() -> None:
    vision = extract_vision_inputs(
        [
            segment("image", url="https://multimedia.example/a.jpg"),
            segment("face", id="21", text="白眼"),
        ],
        vision_enabled=False,
        max_images=2,
    )
    assert vision.images == ()
    assert vision.dropped_images == 0  # 关闭属正常，不计丢弃
    assert vision.image_segments == 1
    assert vision.face_tags == ("[表情:白眼]",)
    assert vision.has_attachments


# ---- 入站提取：face / mface ----


def test_face_tag_prefers_provider_text() -> None:
    vision = extract_vision_inputs(
        [segment("face", id="13", text="呲牙")],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.face_tags == ("[表情:呲牙]",)
    assert vision.images == ()


def test_face_tag_falls_back_to_builtin_map() -> None:
    vision = extract_vision_inputs(
        [segment("face", id="21")],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.face_tags == ("[表情:白眼]",)


def test_face_tag_unknown_id_keeps_id_placeholder() -> None:
    vision = extract_vision_inputs(
        [segment("face", id="999")],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.face_tags == ("[表情#999]",)


def test_mface_yields_tag_and_image_source() -> None:
    vision = extract_vision_inputs(
        [segment("mface", text="哈哈哈", url="https://multimedia.example/mface.jpg")],
        vision_enabled=True,
        max_images=2,
    )
    assert vision.face_tags == ("[表情包:哈哈哈]",)
    assert vision.images == ("https://multimedia.example/mface.jpg",)
    assert vision.image_segments == 1


def test_mface_without_text_renders_bare_tag() -> None:
    vision = extract_vision_inputs(
        [segment("mface")],
        vision_enabled=False,
        max_images=2,
    )
    assert vision.face_tags == ("[表情包]",)


# ---- 记忆占位 ----


def test_append_attachment_tags_combines_image_and_faces() -> None:
    vision = VisionInputs(face_tags=("[表情:呲牙]",), image_segments=1)
    assert append_attachment_tags("看看这个", vision) == "看看这个 [图片][表情:呲牙]"


def test_append_attachment_tags_noop_without_attachments() -> None:
    assert append_attachment_tags("纯文本", VisionInputs()) == "纯文本"


def test_append_attachment_tags_empty_text_gets_tags_only() -> None:
    vision = VisionInputs(face_tags=("[表情:白眼]",))
    assert append_attachment_tags("", vision) == "[表情:白眼]"


# ---- 出站标记解析 ----


def test_parse_reply_without_markers_is_plain() -> None:
    parsed = parse_reply_parts("就是一句话")
    assert parsed.stickers == 0 and parsed.faces == 0 and parsed.dropped == 0
    assert parsed.parts[0].kind == "text"
    assert parsed.parts[0].value == "就是一句话"


def test_parse_reply_mixed_markers_keep_order() -> None:
    parsed = parse_reply_parts("哈哈 [发图:dog] [发脸:呲牙] 尬住了")
    kinds = [part.kind for part in parsed.parts]
    assert kinds == ["text", "sticker", "face", "text"]
    assert parsed.parts[1].value == "dog"
    assert parsed.parts[2].value == "呲牙"
    assert parsed.stickers == 1 and parsed.faces == 1


def test_parse_reply_accepts_fullwidth_colon() -> None:
    parsed = parse_reply_parts("[发图：dog]")
    assert parsed.stickers == 1
    assert parsed.parts[0].value == "dog"


def test_parse_reply_caps_markers_per_reply() -> None:
    parsed = parse_reply_parts("[发图:a][发图:b][发图:c][发脸:呲牙][发脸:晕][发脸:大哭][发脸:羞]")
    assert parsed.stickers == 2
    assert parsed.faces == 3
    assert parsed.dropped == 2


def test_parse_reply_markers_only_leaves_no_text_part() -> None:
    parsed = parse_reply_parts("[发图:dog]")
    assert [part.kind for part in parsed.parts] == ["sticker"]


def test_strip_reply_markers_keeps_text() -> None:
    assert strip_reply_markers("哈哈 [发图:dog] 尬住") == "哈哈 尬住"


# ---- 表情名反查 ----


def test_face_id_for_name_resolves_whitelist() -> None:
    assert face_id_for_name("呲牙") == 13
    assert face_id_for_name(" OK ") == 84


def test_face_id_for_name_rejects_outside_whitelist() -> None:
    # 菜刀在完整表里但不在机器人回表情的白名单里
    assert QQ_FACE_NAMES.get(50) == "菜刀"
    assert face_id_for_name("菜刀") is None
    assert face_id_for_name("不存在的表情") is None


def test_face_whitelist_all_resolvable() -> None:
    for name in QA_FACE_WHITELIST:
        assert face_id_for_name(name) is not None, name


# ---- 近期图片回忆（S11-VISION-01 跨消息补丁）----


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_recall_returns_recent_urls_within_ttl() -> None:
    from qq_bot.services.vision_extract import ImageRecallBuffer

    clock = _FakeClock()
    buffer = ImageRecallBuffer(clock=clock)
    buffer.remember(1001, ["https://img.example/a.jpg"])

    assert buffer.recall(1001, ttl_seconds=300) == ("https://img.example/a.jpg",)


def test_recall_expires_after_ttl() -> None:
    from qq_bot.services.vision_extract import ImageRecallBuffer

    clock = _FakeClock()
    buffer = ImageRecallBuffer(clock=clock)
    buffer.remember(1001, ["https://img.example/a.jpg"])
    clock.now += 301

    assert buffer.recall(1001, ttl_seconds=300) == ()


def test_recall_isolated_per_group_and_prefers_latest() -> None:
    from qq_bot.services.vision_extract import ImageRecallBuffer

    clock = _FakeClock()
    buffer = ImageRecallBuffer(clock=clock)
    buffer.remember(1001, ["https://img.example/old.jpg"])
    clock.now += 10
    buffer.remember(1001, ["https://img.example/new.jpg"])
    buffer.remember(2002, ["https://img.example/other.jpg"])

    assert buffer.recall(1001, ttl_seconds=300) == ("https://img.example/new.jpg",)
    assert buffer.recall(2002, ttl_seconds=300) == ("https://img.example/other.jpg",)
    assert buffer.recall(3003, ttl_seconds=300) == ()


def test_recall_keeps_only_max_per_group() -> None:
    from qq_bot.services.vision_extract import ImageRecallBuffer

    clock = _FakeClock()
    buffer = ImageRecallBuffer(clock=clock, max_per_group=2)
    buffer.remember(1001, ["https://img.example/1.jpg"])
    clock.now += 1
    buffer.remember(1001, ["https://img.example/2.jpg"])
    clock.now += 1
    buffer.remember(1001, ["https://img.example/3.jpg"])

    # 最旧的 1 被挤出；回捞返回最近一条
    assert buffer.recall(1001, ttl_seconds=300) == ("https://img.example/3.jpg",)
    clock.now += 1
    assert buffer.recall(1001, ttl_seconds=1.5) in (
        ("https://img.example/3.jpg",),
        ("https://img.example/2.jpg",),
    )


def test_recall_disabled_with_zero_ttl() -> None:
    from qq_bot.services.vision_extract import ImageRecallBuffer

    buffer = ImageRecallBuffer(clock=_FakeClock())
    buffer.remember(1001, ["https://img.example/a.jpg"])

    assert buffer.recall(1001, ttl_seconds=0) == ()


def test_remember_ignores_empty_urls() -> None:
    from qq_bot.services.vision_extract import ImageRecallBuffer

    buffer = ImageRecallBuffer(clock=_FakeClock())
    buffer.remember(1001, [])

    assert buffer.recall(1001, ttl_seconds=300) == ()
