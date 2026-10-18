from qq_bot.services.quote_reference import (
    QuoteReference,
    QuoteReferenceLRU,
    parse_reply_reference,
    render_quoted_raw_text,
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
