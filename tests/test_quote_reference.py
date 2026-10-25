from qq_bot.services.quote_reference import (
    QuoteReference,
    QuoteReferenceLRU,
    parse_reply_reference,
    render_quoted_raw_text,
    resolve_reply_reference,
)


class FakeSeg:
    def __init__(self, type_: str, data: dict):
        self.type = type_
        self.data = data


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_prefers_reply_segment_embedded_fields():
    segs = [FakeSeg("reply", {"id": 7, "qq": 12345, "text": "  上一把太离谱了  "})]
    ref = parse_reply_reference(segs, self_id=999, lru=None)
    assert ref == QuoteReference(ref_user_id=12345, ref_text="上一把太离谱了")


def test_falls_back_to_lru_lookup():
    lru = QuoteReferenceLRU()
    lru.remember(7, 54321, "凌晨上分真的会猝死")
    segs = [FakeSeg("reply", {"id": 7})]
    ref = parse_reply_reference(segs, self_id=999, lru=lru)
    assert ref is not None and ref.ref_user_id == 54321
    assert ref.ref_text == "凌晨上分真的会猝死"


def test_lru_ttl_expiry_and_capacity():
    clock = FakeClock()
    lru = QuoteReferenceLRU(capacity=2, ttl_seconds=30.0, clock=clock)
    lru.remember(1, 101, "一")
    lru.remember(2, 102, "二")
    lru.remember(3, 103, "三")  # 容量淘汰：1 被逐出
    assert lru.lookup(1) is None
    assert lru.lookup(3) is not None
    clock.now += 31
    assert lru.lookup(3) is None  # TTL 过期


def test_no_reply_segment_returns_none():
    assert parse_reply_reference([FakeSeg("text", {"text": " hi "})], self_id=1, lru=None) is None


def test_missing_both_sources_returns_none():
    segs = [FakeSeg("reply", {"id": 404})]
    assert parse_reply_reference(segs, self_id=1, lru=QuoteReferenceLRU()) is None


def test_render_marker_format():
    ref = QuoteReference(ref_user_id=42, ref_text="原文")
    assert render_quoted_raw_text("真的假的", ref) == "回复 [引用 u42:原文]：真的假的"


# ---- 三级解析（S11-QUOTE-01）：扩展字段 → LRU → get_msg 回查 ----


async def test_resolve_fetches_message_when_lru_misses():
    """LRU 没中（重启/超时/超容量）且带 fetch 闭包时按消息 id 回查原文。"""

    async def fetch(message_id: int):
        assert message_id == 404
        return (54321, "凌晨上分真的会猝死")

    segs = [FakeSeg("reply", {"id": 404})]
    ref = await resolve_reply_reference(segs, self_id=999, lru=QuoteReferenceLRU(), fetch=fetch)
    assert ref == QuoteReference(ref_user_id=54321, ref_text="凌晨上分真的会猝死")


async def test_resolve_fetch_failure_degrades_to_none():
    """get_msg 失败（NapCat 离线/权限）返回 None，调用方降级为无引用。"""

    async def fetch(message_id: int):
        raise RuntimeError("napcat offline")

    segs = [FakeSeg("reply", {"id": 404})]
    assert await resolve_reply_reference(segs, self_id=1, lru=None, fetch=fetch) is None


async def test_resolve_extension_fields_skip_fetch():
    """扩展字段命中（go-cqhttp 惯例）就不该再付一次 get_msg 调用。"""
    calls: list[int] = []

    async def fetch(message_id: int):
        calls.append(message_id)
        return (1, "不该被用到")

    segs = [FakeSeg("reply", {"id": 7, "qq": 12345, "text": "原文"})]
    ref = await resolve_reply_reference(segs, self_id=999, lru=None, fetch=fetch)
    assert ref is not None and ref.ref_user_id == 12345
    assert calls == []


async def test_resolve_lru_hit_skips_fetch():
    calls: list[int] = []

    async def fetch(message_id: int):
        calls.append(message_id)
        return (1, "不该被用到")

    lru = QuoteReferenceLRU()
    lru.remember(7, 54321, "LRU 里的原文")
    segs = [FakeSeg("reply", {"id": 7})]
    ref = await resolve_reply_reference(segs, self_id=999, lru=lru, fetch=fetch)
    assert ref is not None and ref.ref_text == "LRU 里的原文"
    assert calls == []


async def test_resolve_without_fetch_keeps_legacy_behavior():
    """没给 fetch 闭包时与旧版 parse 等价：拿不到就 None，不报错。"""
    segs = [FakeSeg("reply", {"id": 404})]
    assert await resolve_reply_reference(segs, self_id=1, lru=QuoteReferenceLRU()) is None


async def test_resolve_fetch_text_cleaned_and_truncated():
    async def fetch(message_id: int):
        return (54321, "  很长\n很长的\t原文  " * 10)

    segs = [FakeSeg("reply", {"id": 404})]
    ref = await resolve_reply_reference(segs, self_id=999, lru=None, fetch=fetch)
    assert ref is not None
    assert len(ref.ref_text) <= 30
    assert "\n" not in ref.ref_text and "\t" not in ref.ref_text


# ---- 渲染→匿名替换的组合行为（S10-HUMAN-P0-06）----


def test_render_then_substitute_roundtrip():
    from datetime import UTC, datetime

    from qq_bot.services.chat_memory import ChatMemoryRow
    from qq_bot.services.memory_prompt import build_user_aliases, substitute_reference_tokens

    def _row(text: str, user_id: int, row_id: int) -> ChatMemoryRow:
        return ChatMemoryRow(
            id=row_id,
            group_id=1001,
            user_id=user_id,
            message_text=text,
            created_at=datetime.now(UTC).isoformat(),
            is_ai_prompt=False,
            ai_reply=None,
        )

    ref = QuoteReference(ref_user_id=1654321, ref_text="上分")
    raw = render_quoted_raw_text("真的假的", ref)
    rows = [_row(raw, user_id=3001, row_id=1), _row("占位", user_id=1654321, row_id=2)]
    alias = build_user_aliases(rows)
    line = substitute_reference_tokens(f"用户A：{raw}", alias)
    assert "u1654321" not in line
    assert "用户B" in line
