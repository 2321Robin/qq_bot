import json

from prometheus_client import REGISTRY

from qq_bot.services.chat_memory import ChatMemoryRow, JargonRow, PersonObservationRow
from qq_bot.services.expression_learner import (
    _process_batch,
    run_learning_round,
    split_slots,
    validate_expression,
)


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


# 满员 20 条消息批次的提取 JSON 正文下限（含每条候选的 template + evidence_quote）。
# 生产实测（2026-10-19，glm-4-flash）同形状载荷 1188~1247 字符；取 1000 作下限，
# 任何低于它的 _LLM_MAX_TOKENS 都会被 provider 截断成不可解析 JSON。
_FULL_PAYLOAD_MIN_CHARS = 1000


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
                    {"template": "那{对象}杀我", "evidence_quote": "那新活动杀我"},
                    # 校验拒绝：含槽位模板的固定片段带内容词（"杀我" 不在虚词白名单）
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

    calls = {"extract": 0, "review": 0, "define": 0}

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            calls["extract"] += 1
            if calls["extract"] == 1:
                return "不是json"  # 第一批提取失败
            return _extract_payload(
                expressions=[{"template": "确实", "evidence_quote": texts[20]}], jargon=[]
            )
        if "表达审核器" in system:
            calls["review"] += 1
            return _review_payload(accept_indexes={0}, count=1)
        calls["define"] += 1
        return json.dumps({"definition": "群内用语"})  # 黑话释义

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=_settings(), complete=complete, quota=None
    )
    assert summary.status == "ok" and summary.batches == 2
    # 第一批提取失败 → 该批不进入校验/自检；第二批照常（自检分支被真实走到）
    assert calls["extract"] == 2 and calls["review"] == 1
    assert summary.rejected_review == 0
    assert summary.stored == 1  # 第二批照常
    assert len(mem.stored) == 1 and mem.stored[0]["template"] == "确实"


async def test_extract_max_tokens_covers_full_length_payload():
    """生产事故回归（2026-10-19 部署实测）：glm-4-flash 对 20 条消息的提取调用
    在 max_tokens=500 下返回 1247 字符的 JSON 被 provider 截断
    （``Unterminated string``），``_parse_extract`` 返回 None → 整批零入库，
    学习轮表面上 status=ok 但 expressions/jargon 全为 0。

    此用例锁死提取调用的 max_tokens 下限：一个 20 条消息的满员批次至少有
    ``_FULL_PAYLOAD_MIN_CHARS`` 字符的 JSON 正文（含全部候选的 template +
    evidence_quote），请求的上限必须容纳它——截断即静默丢批，比调用失败
    更难发现（指标只体现 extract/ok，不入库原因不可见）。"""
    from qq_bot.services import expression_learner as learner

    # 消息与模板共用一套合法形状：模板固定片段只含虚词/语气词（过校验器），
    # 而 evidence_quote 逐条不同、各带长数字序号，复制 20 遍仍撑出
    # ≥1000 字符的 JSON 正文，与线上载荷同量级。
    texts = [f"那第{i}个新活动,真的要冲吗" for i in range(20)]
    candidates = [{"template": "那{对象}吗", "evidence_quote": t} for t in texts]
    payload = _extract_payload(expressions=candidates, jargon=[])
    mem = FakeMemory(texts)
    seen: dict[str, int] = {}

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            seen["max_tokens"] = kwargs["max_tokens"]
            return payload
        if "表达审核器" in system:
            return _review_payload(accept_indexes=set(range(20)), count=20)
        raise AssertionError("本用例不应产生黑话释义调用")

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=_settings(), complete=complete, quota=None
    )

    assert learner._LLM_MAX_TOKENS >= _FULL_PAYLOAD_MIN_CHARS
    assert seen["max_tokens"] == learner._LLM_MAX_TOKENS
    assert len(payload) >= _FULL_PAYLOAD_MIN_CHARS
    # 完整载荷端到端落库：截断会让这一行变成 0（真实事故的静默失败形态）
    assert summary.stored == 20
    assert summary.expressions_extracted == 20


async def test_round_review_parse_failure_rejects_all_and_stores_nothing():
    """自检返回非自检 JSON（如提取形状的载荷）→ 全拒，本批零入库（fail closed）。"""
    mem = FakeMemory(["那新活动吗", "确实"])

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(
                expressions=[
                    {"template": "那{对象}吗", "evidence_quote": "那新活动吗"},
                    {"template": "确实", "evidence_quote": "确实"},
                ],
                jargon=[],
            )
        if "表达审核器" in system:
            # 提取形状的载荷：_parse_review 找不到 results / 长度不符 → None
            return _extract_payload(
                expressions=[{"template": "那{对象}吗", "evidence_quote": "那新活动吗"}], jargon=[]
            )
        raise AssertionError("自检失败后不应再发其它调用")

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=_settings(), complete=complete, quota=None
    )
    assert summary.expressions_extracted == 2  # 通过校验的候选照常计数
    assert summary.rejected_review == 2
    assert summary.stored == 0 and summary.duplicated == 0
    assert mem.stored == []  # upsert 从未被调用


async def test_round_review_call_raises_rejects_all_and_stores_nothing():
    """自检调用本身抛异常 → 无文本 → 同样全拒、零入库（spec 护栏 4）。"""
    mem = FakeMemory(["那新活动吗", "确实"])

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(
                expressions=[
                    {"template": "那{对象}吗", "evidence_quote": "那新活动吗"},
                    {"template": "确实", "evidence_quote": "确实"},
                ],
                jargon=[],
            )
        if "表达审核器" in system:
            raise RuntimeError("provider down")
        raise AssertionError("自检失败后不应再发其它调用")

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=_settings(), complete=complete, quota=None
    )
    assert summary.expressions_extracted == 2
    assert summary.rejected_review == 2
    assert summary.stored == 0 and summary.duplicated == 0
    assert mem.stored == []


def _expression_metric(stage: str, result: str) -> float:
    """与 test_ai_chat_plugin._auto_chat_metric 同一套 REGISTRY 采样手法。"""
    return (
        REGISTRY.get_sample_value("qq_bot_expression_total", {"stage": stage, "result": result})
        or 0.0
    )


async def test_round_validate_kept_metric_is_batch_granular():
    """validate/kept 为批粒度（本批至少一条候选跨过校验 → 一次），
    与候选粒度的 validate/rejected 并存（「本批表达式中 kept/rejected 多少」）。"""
    mem = FakeMemory(["那新活动吗", "确实"])

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(
                expressions=[
                    {"template": "那{对象}吗", "evidence_quote": "那新活动吗"},  # 通过校验
                    {
                        "template": "那{对象}杀我",
                        "evidence_quote": "那新活动杀我",
                    },  # fixed_content 拒
                ],
                jargon=[],
            )
        if "表达审核器" in system:
            return _review_payload(accept_indexes={0}, count=1)
        raise AssertionError("本用例不应触发黑话释义调用")

    kept_before = _expression_metric("validate", "kept")
    rejected_before = _expression_metric("validate", "rejected")

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=_settings(), complete=complete, quota=None
    )

    assert summary.expressions_extracted == 1 and summary.rejected_validation == 1
    assert _expression_metric("validate", "kept") == kept_before + 1
    assert _expression_metric("validate", "rejected") == rejected_before + 1


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


class _BotReplyPollutionMemory(FakeMemory):
    """messages_since 返回的行里，机器人自己的回复写在 ``ai_reply`` 列
    （人类行的附带回复），``message_text`` 仍是人类自己的话。"""

    async def messages_since(self, *, group_id, days, now=None):
        from datetime import UTC, datetime, timedelta

        from qq_bot.services.chat_memory import ChatMemoryRow

        return [
            ChatMemoryRow(
                id=i + 1,
                group_id=group_id,
                user_id=2001 + i,
                message_text=t,
                created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                is_ai_prompt=False,
                # 机器人说过的话只在 ai_reply 列，绝不作为消息行正文出现
                ai_reply="那新活动吗" if i == 0 else "",
            )
            for i, t in enumerate(self.texts)
        ]


async def test_bot_reply_text_is_never_learnable_evidence():
    """自我污染回归（reviewer 已复核关闭，此处锁死行为）。

    机器人的回复只存在于 ``ai_reply`` 列，从不作为 ``message_text`` 行；
    因此以机器人回复为 evidence_quote 的候选模板必须被拒——证据原文不在
    任何一条人类消息正文里（``_resolve_evidence_row`` 找不到行 →
    validate/rejected），本批零入库。

    注意：本用例只锁死轮级（解析器）行为——即使机器人回复泄漏进
    ``message_text``，证据也能解析到行，本断言仍然会绿。真正的不变式
    （``messages_since`` 从不把机器人文本当消息正文返回）在
    ``tests/test_chat_memory_learning.py``
    ``test_messages_since_never_returns_bot_reply_as_message_text``
    的真实 sqlite 上锁死；那一处才是污染闭环的入口。"""
    mem = _BotReplyPollutionMemory(["随便聊聊", "确实"])
    settings = _settings()

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            # 提取器错把机器人回复当群友句式引用（正是要防的情形）
            return _extract_payload(
                expressions=[{"template": "那{对象}吗", "evidence_quote": "那新活动吗"}],
                jargon=[],
            )
        raise AssertionError("候选未过校验 → 不应有自检调用")

    summary = await run_learning_round(
        group_id=1, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.stored == 0
    assert summary.rejected_validation == 1
    assert mem.stored == []


def test_run_learning_round_has_no_rng_parameter():
    """学习轮不抽样：``rng`` 曾作为死参数穿过 _process_batch /
    run_learning_round（Task 3 预留，Task 4 后抽样落在 run_auto_chat），
    此处锁死签名不再长回来。"""
    import inspect

    assert "rng" not in inspect.signature(run_learning_round).parameters
    assert "rng" not in inspect.signature(_process_batch).parameters


class _ObserveMemory(FakeMemory):
    """在 FakeMemory 上加观察卡存取与 /记忆关闭 状态。

    所有素材行归同一位群友（2001）：观察卡的活跃发言者门槛按用户聚合，
    每行一个用户会让任何批量都达不到门槛。"""

    def __init__(self, texts: list[str], *, opted_out: bool = False):
        super().__init__(texts)
        self.opted_out = opted_out
        self.cards: dict[int, dict] = {}

    async def messages_since(self, *, group_id, days, now=None):
        from datetime import UTC, datetime, timedelta

        return [
            ChatMemoryRow(
                id=i + 1,
                group_id=group_id,
                user_id=2001,
                message_text=t,
                created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                is_ai_prompt=False,
                ai_reply="",
            )
            for i, t in enumerate(self.texts)
        ]

    async def observations_enabled(self, *, group_id, user_id):
        return not self.opted_out

    async def get_person_observation(self, *, group_id, user_id):
        card = self.cards.get(user_id)
        if card is None:
            return None
        return PersonObservationRow(
            group_id=group_id,
            user_id=user_id,
            observation=str(card.get("observation", "")),
            source_message_id=card.get("source_message_id"),
            insufficient=bool(card.get("insufficient", False)),
            updated_at="",
        )

    async def upsert_person_observation(self, **kwargs):
        self.cards[kwargs["user_id"]] = kwargs


def _observe_settings(**overrides):
    base = {
        "person_observe_enabled": True,
        "person_observe_min_messages": 4,
        "person_observe_max_chars": 100,
    }
    base.update(overrides)
    return _settings(**base)


def _observe_payload(observation: str):
    return json.dumps({"observation": observation}, ensure_ascii=False)


async def test_observation_cards_written_for_active_speakers():
    texts = [f"消息{i}" for i in range(6)]  # 同一用户 6 条
    mem = _ObserveMemory(texts)
    settings = _observe_settings()

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        return _observe_payload("话多，爱发表情")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_updated == 1
    assert summary.observations_insufficient == 0
    card = mem.cards[2001]
    assert card["observation"] == "话多，爱发表情"
    assert card["insufficient"] is False
    assert card["source_message_id"] == 6  # 本批该用户最后一条


async def test_observation_skipped_below_min_messages_and_when_closed():
    texts = ["消息0", "消息1"]  # 2 条 < 门槛 4
    mem = _ObserveMemory(texts)
    settings = _observe_settings()

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        raise AssertionError("发言数不足 → 不应有观察调用")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_updated == 0
    assert mem.cards == {}

    # /记忆关闭（opted_out）→ 不写卡
    mem = _ObserveMemory([f"消息{i}" for i in range(6)], opted_out=True)

    async def opted_out_complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        raise AssertionError("用户关闭记忆 → 不应有观察调用")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=opted_out_complete, quota=None
    )
    assert summary.observations_updated == 0
    assert mem.cards == {}


async def test_observation_model_text_is_validated_before_store():
    """含长数字串（隐私）或超长的观察被确定性闸拒绝，落负卡。"""
    mem = _ObserveMemory(["消息0", "消息1", "消息2", "消息3"])
    settings = _observe_settings()  # max_chars=100

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        return _observe_payload("他的QQ号是123456789，加他好友")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_updated == 0
    assert summary.observations_insufficient == 1
    card = mem.cards[2001]
    assert card["observation"] == "" and card["insufficient"] is True


async def test_observation_merges_old_card_into_prompt_and_store():
    """旧卡文本进合并素材，长期事实（称呼偏好/喜好）由模型合并保留。"""
    mem = _ObserveMemory([f"消息{i}" for i in range(6)])
    mem.cards[2001] = {
        "observation": "喜欢被叫阿明，爱聊钓鱼",
        "insufficient": False,
    }
    settings = _observe_settings(person_observe_max_chars=200)
    observe_prompts: list[tuple[str, str]] = []

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        observe_prompts.append((system, kwargs["user_prompt"]))
        return _observe_payload("喜欢被叫阿明，最近在聊装修")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_updated == 1
    assert summary.observations_kept == 0
    system, user_prompt = observe_prompts[0]
    assert "旧观察" in user_prompt
    assert "喜欢被叫阿明，爱聊钓鱼" in user_prompt
    assert "不超过200字" in system  # 字数上限与配置同源，不再硬编码 100
    card = mem.cards[2001]
    assert card["observation"] == "喜欢被叫阿明，最近在聊装修"
    assert card["insufficient"] is False
    assert card["source_message_id"] == 6


async def test_observation_unparseable_output_keeps_existing_card():
    """模型返回不可解析 → 有旧卡时保留不覆盖（长期记忆不失忆）。"""
    mem = _ObserveMemory(["消息0", "消息1", "消息2", "消息3"])
    mem.cards[2001] = {"observation": "旧观察：喜欢被叫阿明", "insufficient": False}
    settings = _observe_settings()

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        return "不是json"

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_kept == 1
    assert summary.observations_updated == 0
    assert summary.observations_insufficient == 0
    # 旧卡原样保留，未被负卡覆盖
    assert mem.cards[2001]["observation"] == "旧观察：喜欢被叫阿明"
    assert mem.cards[2001]["insufficient"] is False


async def test_observation_validator_reject_keeps_old_card():
    """隐私闸拒绝的输出不能抹掉旧卡（拒收≠清空长期记忆）。"""
    mem = _ObserveMemory(["消息0", "消息1", "消息2", "消息3"])
    mem.cards[2001] = {"observation": "爱聊钓鱼", "insufficient": False}
    settings = _observe_settings()

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        return _observe_payload("他的QQ号是123456789，加他好友")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_kept == 1
    assert summary.observations_updated == 0
    assert mem.cards[2001]["observation"] == "爱聊钓鱼"
    assert mem.cards[2001]["insufficient"] is False


async def test_observation_disabled_by_default_writes_nothing():
    mem = _ObserveMemory(["消息0", "消息1", "消息2", "消息3"])
    settings = _settings()  # person_observe_enabled 默认 False

    async def complete(**kwargs):
        system = kwargs["system_prompt"]
        if "表达分析器" in system:
            return _extract_payload(expressions=[], jargon=[])
        raise AssertionError("功能关闭 → 不应有观察调用")

    summary = await run_learning_round(
        group_id=1001, memory_store=mem, settings=settings, complete=complete, quota=None
    )
    assert summary.observations_updated == 0
    assert mem.cards == {}
