import json

from qq_bot.services.chat_memory import ChatMemoryRow, JargonRow
from qq_bot.services.expression_learner import run_learning_round, split_slots, validate_expression


class FakeQuota:
    def __init__(self, requests: int = 0):
        self.requests = requests
        self.recorded = 0

    async def summary(self, *, scope_type, scope_id):
        return {"requests": self.requests}

    async def record_usage(self, *, scope_type, scope_id, tokens, cost):
        self.recorded += 1


class FakeMemory:
    def __init__(self, texts: list[str]):
        self.texts = texts
        self.stored: list[dict] = []
        self.jargon: dict[str, dict] = {}

    async def messages_since(self, *, group_id, days, now=None):
        from datetime import UTC, datetime, timedelta

        return [
            ChatMemoryRow(
                id=i + 1,
                group_id=group_id,
                user_id=2001 + i,
                message_text=t,
                created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                is_ai_prompt=False,
                ai_reply="",
            )
            for i, t in enumerate(self.texts)
        ]

    async def upsert_expression(self, **kwargs):
        self.stored.append(kwargs)
        return "created"

    async def upsert_jargon(self, *, group_id, term, now=None):
        entry = self.jargon.setdefault(term, {"hits": 0})
        entry["hits"] += 1
        return "new" if entry["hits"] == 1 else "hit"

    async def get_jargon(self, *, group_id, term):
        entry = self.jargon.get(term)
        if entry is None:
            return None
        return JargonRow(
            id=1,
            group_id=group_id,
            term=term,
            definition="",
            status="unconfirmed",
            hits=entry["hits"],
            manual=0,
            first_seen_at="",
            last_seen_at="",
            confirmed_at=None,
        )

    async def confirm_jargon(self, *, group_id, term, definition, manual=False, now=None):
        self.jargon[term].update(definition=definition, status="confirmed")
        return True


def _extract_payload(expressions, jargon):
    return json.dumps({"expressions": expressions, "jargon": jargon}, ensure_ascii=False)


def _review_payload(accept_indexes: set[int], count: int):
    return json.dumps(
        {
            "results": [
                {"index": i, "accept": i in accept_indexes, "reason": "ok"} for i in range(count)
            ]
        }
    )


def _settings(**overrides):
    from qq_bot.config import BotSettings

    base = {
        "expression_learn_enabled": True,
        "expression_learn_batch_messages": 20,
        "expression_learn_max_batches": 5,
        "expression_learn_lookback_days": 7,
        "expression_min_active": 10,
        "jargon_confirm_hits": 4,
    }
    base.update(overrides)
    return BotSettings(**base)


async def test_round_disabled_returns_disabled():
    summary = await run_learning_round(
        group_id=1,
        memory_store=FakeMemory([]),
        settings=_settings(expression_learn_enabled=False),
        complete=None,
        quota=None,
    )
    assert summary.status == "disabled"


async def test_round_quota_denied():
    summary = await run_learning_round(
        group_id=1,
        memory_store=FakeMemory(["随便"]),
        settings=_settings(),
        complete=None,
        quota=FakeQuota(requests=30),
    )
    assert summary.status == "quota_denied"


async def test_round_insufficient_material():
    summary = await run_learning_round(
        group_id=1,
        memory_store=FakeMemory([]),
        settings=_settings(),
        complete=None,
        quota=None,
    )
    assert summary.status == "insufficient_material"


async def test_round_happy_path_extracts_stores_and_confirms_jargon():
    mem = FakeMemory(["那新活动吗", "确实", "咸鱼翻车", "又咸鱼翻车了", "笑死"])
    settings = _settings(jargon_confirm_hits=2)

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(
                expressions=[
                    {"template": "那{对象}吗", "evidence_quote": "那新活动吗"},
                    {"template": "确实", "evidence_quote": "确实"},
                    {
                        "template": "去打新活动吗",
                        "evidence_quote": "那新活动吗",
                    },  # 校验拒绝：内容词
                ],
                jargon=["咸鱼翻车"],
            )
        if "表达审核器" in system:
            return _review_payload(accept_indexes={0, 1}, count=2)
        return json.dumps({"definition": "搞砸了"})  # 黑话释义

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=FakeQuota()
    )
    assert summary.status == "ok"
    assert summary.expressions_extracted == 2
    assert summary.rejected_validation == 1
    assert summary.rejected_review == 0
    assert summary.stored == 2
    assert summary.jargon_new == 1 and summary.jargon_hit == 1
    assert summary.jargon_confirmed == 1
    assert mem.jargon["咸鱼翻车"]["definition"] == "搞砸了"


async def test_round_extract_failure_isolates_batch():
    texts = [f"消息{i}" for i in range(25)]  # 2 批
    mem = FakeMemory(texts)

    calls = {"n": 0}

    async def complete(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "不是json"  # 第一批提取失败
        return _extract_payload(
            expressions=[{"template": "确实", "evidence_quote": texts[20]}], jargon=[]
        )

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=_settings(), complete=complete, quota=None
    )
    assert summary.status == "ok" and summary.batches == 2
    assert summary.stored == 1  # 第二批照常


class TestSplitSlots:
    def test_slot_and_fragments(self):
        fragments, count = split_slots("那{对象}吗")
        assert count == 1
        assert "".join(fragments) == "那吗"

    def test_no_slots(self):
        fragments, count = split_slots("确实")
        assert count == 0 and fragments == ["确实"]


class TestValidateExpression:
    def test_slotted_template_ok(self):
        ok, reason = validate_expression(
            template="那{对象}吗", evidence_quote="那新活动吗", evidence_text="那新活动吗 冲"
        )
        assert (ok, reason) == (True, "ok")

    def test_unslotted_short_word_ok(self):
        ok, reason = validate_expression(
            template="确实", evidence_quote="确实", evidence_text="确实 哈哈"
        )
        assert (ok, reason) == (True, "ok")

    def test_unslotted_over_six_rejected(self):
        ok, reason = validate_expression(
            template="这个真的太离谱了",
            evidence_quote="这个真的太离谱了",
            evidence_text="这个真的太离谱了",
        )
        assert (ok, reason) == (False, "unslotted_too_long")

    def test_fixed_content_word_rejected_for_slotted(self):
        ok, reason = validate_expression(
            template="{对象}杀我", evidence_quote="雾克杀我", evidence_text="雾克杀我"
        )
        # "杀" 不在虚词白名单 → 含槽位模板的固定片段带内容词
        assert (ok, reason) == (False, "fixed_content")

    def test_fixed_not_grounded_rejected(self):
        ok, reason = validate_expression(
            template="那{对象}吗", evidence_quote="冲冲冲", evidence_text="冲冲冲"
        )
        assert (ok, reason) == (False, "fixed_not_grounded")

    def test_bad_slot_rejected(self):
        ok, reason = validate_expression(
            template="{东西}吗", evidence_quote="冲吗", evidence_text="冲吗"
        )
        assert (ok, reason) == (False, "bad_slot")

    def test_strategy_rejected(self):
        ok, reason = validate_expression(
            template="别理他们", evidence_quote="别理他们", evidence_text="别理他们"
        )
        assert (ok, reason) == (False, "strategy")

    def test_qq_number_rejected(self):
        ok, reason = validate_expression(
            template="u1654321", evidence_quote="u1654321", evidence_text="u1654321"
        )
        assert (ok, reason) == (False, "forbidden")

    def test_too_long_rejected(self):
        ok, reason = validate_expression(
            template="很" * 21, evidence_quote="很" * 21, evidence_text="很" * 21
        )
        assert (ok, reason) == (False, "too_long")

    def test_empty_rejected(self):
        assert validate_expression(template="  ", evidence_quote="x", evidence_text="x") == (
            False,
            "empty",
        )
