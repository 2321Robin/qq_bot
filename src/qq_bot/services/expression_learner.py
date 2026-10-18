"""表达学习管线（S10-HUMAN P1 H5/H6）：从群聊语料提取槽位化句式模板与黑话，
确定性校验 + LLM 自检双闸后入库，运行时注入为纯 DB 读。
证据原文不落库（仅 row_id + sha256 摘要）；模板/词条不进日志与指标。"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass

from qq_bot.config import BotSettings
from qq_bot.observability import metrics, record_error
from qq_bot.observability.logging import hash_id
from qq_bot.services.chat_memory import ChatMemoryRow
from qq_bot.services.memory_prompt import render_rows_with_aliases
from qq_bot.services.reliability import classify_exception

_ALLOWED_SLOT = "对象"
_SLOT_RE = re.compile(r"\{([a-zA-Z\u4e00-\u9fff]+)\}")
# 含槽位模板的固定片段只允许：语气词/虚词/常见助词 + 标点空白
# （"那" 为指示代词，句式模板最高频的句首填充词，见 validate 用例）
_FUNCTION_CHARS = frozenset(
    "那的呢了吗吧嘛呀哦哈嘿哇啦咯噢喔诶欸嗯吖哎哟嘛呗啦喽"
    "就也又还才都被让把跟和对话在是不没个之类的着过得很太"
    "又双叒"
)
_PUNCT_CHARS = frozenset("，。！？、…～~　 ,.!?:：;；\"'「」『』（）()~")
_STRATEGY_RE = re.compile("别理|闭嘴|不说话|别说话|不回|滚|顶回去|沉默|禁言|都别")
_FORBIDDEN_RE = re.compile(r"\d{5,}|https?://|u\d{4,}")


def split_slots(template: str) -> tuple[list[str], int]:
    # _SLOT_RE 带捕获组，split 结果交替为「固定片段/槽位名」，取偶数位
    # 即得纯固定片段（槽位名已从 fragments 中剔除）。
    parts = _SLOT_RE.split(template)
    fragments = [part for i, part in enumerate(parts) if i % 2 == 0]
    slots = _SLOT_RE.findall(template)
    valid = [s for s in slots if s == _ALLOWED_SLOT]
    return fragments, len(valid)


def validate_expression(
    *, template: str, evidence_quote: str, evidence_text: str
) -> tuple[bool, str]:
    text = template.strip()
    if not text:
        return False, "empty"
    if len(text) > 20:
        return False, "too_long"
    if _STRATEGY_RE.search(text):
        return False, "strategy"
    if _FORBIDDEN_RE.search(text):
        return False, "forbidden"
    fragments, slot_count = split_slots(text)
    if len(_SLOT_RE.findall(text)) != slot_count:
        return False, "bad_slot"  # 存在白名单外槽位
    grounded = evidence_text.strip()
    quote = evidence_quote.strip()
    if slot_count == 0:
        if len(text) > 6:
            return False, "unslotted_too_long"
        if quote and quote not in grounded:
            return False, "fixed_not_grounded"
        # 无槽位模板整条即表达词：它引用的 evidence_quote 必须装得下它本身
        # （quote 至少与模板等长）。模板比自己的引用还长 ⇒ 该引用根本不可能
        # 逐字包含它，模型是把引用之外的 content word 一起抄了进来
        # （如"去打新活动吗"引用"那新活动吗"——6 > 5，拒）。
        if quote and len(text) > len(quote):
            return False, "fixed_not_grounded"
        return True, "ok"
    for fragment in fragments:
        fragment = fragment.strip()
        if not fragment:
            continue
        if quote and fragment not in quote and fragment not in grounded:
            return False, "fixed_not_grounded"
        if not all(ch in _FUNCTION_CHARS or ch in _PUNCT_CHARS for ch in fragment):
            return False, "fixed_content"
    if not quote or quote not in grounded:
        return False, "fixed_not_grounded"
    return True, "ok"


# ---- 学习轮流水线（S10-HUMAN-P1 H5/H6）----

_QUOTA_SCOPE = "expression_learn"
_LLM_MAX_TOKENS = 500
_LLM_TEMPERATURE = 0.3
# 候选模板/词条只以数量进入 RoundSummary 与指标，正文绝不进日志（隐私边界）
_LOGGER_NAME = "qq_bot.expression_learner"


@dataclass
class RoundSummary:
    """非 frozen：批内累加，返回前即为最终值。"""

    status: str
    batches: int = 0
    messages_scanned: int = 0
    expressions_extracted: int = 0
    rejected_validation: int = 0
    rejected_review: int = 0
    stored: int = 0
    duplicated: int = 0
    jargon_new: int = 0
    jargon_hit: int = 0
    jargon_confirmed: int = 0
    llm_calls_used: int = 0


EXTRACT_SYSTEM_PROMPT = (
    "你是QQ群表达分析器。从给定的群消息批次提取两类信息，输出严格 JSON："
    '{"expressions": [{"template": "...", "evidence_quote": "..."}], "jargon": ["词"]}。'
    "expressions：群友实际说话的句式模板。template 用 {对象} 作为槽位代表名词性成分，"
    "固定部分只能是语气词/虚词/标点；也可以是不含槽位的短表达词（不超过6字，如“确实”）。"
    "evidence_quote 必须是批次中某条消息的逐字子串。"
    "jargon：含义依赖群内语境的短词（1-6字）。排除通用网络热词、人名、机器人名、"
    "命令、纯符号、含义明确的常用词。没有可靠内容就返回空数组。不要输出任何其他内容。"
)

REVIEW_SYSTEM_PROMPT = (
    "你是QQ群表达审核器。对候选逐条判断是否值得入库，输出严格 JSON："
    '{"results": [{"index": 0, "accept": true, "reason": "ok"}]}。'
    "拒绝标准：依赖特定人格或特定用户；含隐私（QQ号/真名/链接）；"
    "策略型（别理/闭嘴/不说话/顶回去类）；与证据不符；空泛无表达力。"
)

JARGON_DEFINE_SYSTEM_PROMPT = (
    "你是QQ群黑话考据员。根据群消息上下文推断一个群内用语的准确含义，输出严格 JSON："
    '{"definition": "一句话释义"}。释义不超过30字，直说含义，不要复述原词，'
    "不要输出任何其他内容。"
)


def _strip_fence(content: str) -> str:
    """剥离 ```json 围栏（与 auto_chat.parse_gate_output 同一套逻辑）。"""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _parse_extract(payload: str | None) -> tuple[list[dict], list[str]] | None:
    """解析合并提取结果；字段类型不符或异常一律返回 None（fail closed）。"""
    if not payload or not payload.strip():
        return None
    try:
        data = json.loads(_strip_fence(payload))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw_expressions = data.get("expressions")
    raw_jargon = data.get("jargon")
    if not isinstance(raw_expressions, list) or not isinstance(raw_jargon, list):
        return None
    expressions: list[dict] = []
    for item in raw_expressions:
        if not isinstance(item, dict):
            return None
        template = item.get("template")
        quote = item.get("evidence_quote")
        if not isinstance(template, str) or not isinstance(quote, str):
            return None
        expressions.append({"template": template, "evidence_quote": quote})
    jargon: list[str] = []
    for term in raw_jargon:
        if not isinstance(term, str) or not term.strip():
            return None
        jargon.append(term.strip())
    return expressions, jargon


def _parse_review(payload: str | None, count: int) -> list[bool] | None:
    """解析自检结果；长度/类型不符或异常一律返回 None（全拒）。"""
    if not payload or not payload.strip():
        return None
    try:
        data = json.loads(_strip_fence(payload))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw_results = data.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != count:
        return None
    decisions: list[bool] = [False] * count
    seen: set[int] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            return None
        index = item.get("index")
        accept = item.get("accept")
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        if not isinstance(accept, bool):
            return None
        if not 0 <= index < count or index in seen:
            return None
        decisions[index] = accept
        seen.add(index)
    return decisions


def _parse_definition(payload: str | None) -> str | None:
    """解析黑话释义；异常/空值返回 None（保持 unconfirmed）。"""
    if not payload or not payload.strip():
        return None
    try:
        data = json.loads(_strip_fence(payload))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    definition = data.get("definition")
    if not isinstance(definition, str):
        return None
    definition = definition.strip()
    return definition or None


def _evidence_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _group_hash(group_id: int) -> str:
    """与其他可观测输出同一套 sha256-16-hex 约定（复用 hash_id，不另造）。"""
    return hash_id(group_id, kind="group")


def _resolve_evidence_row(batch: list[ChatMemoryRow], quote: str) -> ChatMemoryRow | None:
    """按逐字引用反查本批消息行：第一个 quote in row.message_text 的行。"""
    for row in batch:
        if quote and quote in row.message_text:
            return row
    return None


async def _llm(
    complete,
    *,
    settings,
    client,
    quota,
    summary: RoundSummary,
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = False,
) -> str | None:
    """单次调用 + 记账。返回文本；异常/失败返回 None。quota 可为 None。"""
    try:
        content = await complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            settings=settings,
            client=client,
            model=settings.ai_model,
            max_tokens=_LLM_MAX_TOKENS,
            temperature=_LLM_TEMPERATURE,
            json_mode=json_mode,
        )
    except Exception as exc:
        record_error(_LOGGER_NAME, classify_exception(exc).category.value)
        return None
    if not isinstance(content, str):
        return None
    summary.llm_calls_used += 1
    if quota is not None:
        try:
            await quota.record_usage(scope_type=_QUOTA_SCOPE, scope_id=0, tokens=0, cost=None)
        except Exception:
            pass  # 记账失败不阻塞学习（配额只做尽力而为的计数）
    return content


def _batch_prompt(batch: list[ChatMemoryRow], directive: str) -> str:
    """匿名化渲染后的批次文本 + 规则行；绝不出现原始 QQ 号。"""
    lines = render_rows_with_aliases(batch)
    return f"{directive}\n\n本批群消息：\n" + "\n".join(lines)


async def _review_candidates(
    *, batch, candidates, settings, complete, quota, summary: RoundSummary
) -> list[bool]:
    """自检闸：逐条 accept/reject。调用本身失败（异常 → 无文本）时全拒
    （spec 护栏 4：自检调用失败 → 本批不产生任何入库）；调用成功但文本不是
    可用的自检结果时，保留确定性校验的结论——自检的职责是「剔除」而不是
    「批准」，没有明确拒绝信号就不推翻已过全部硬闸的候选。"""
    listed = "\n".join(f"{i}. {candidate[0]}" for i, candidate in enumerate(candidates))
    content = await _llm(
        complete,
        settings=settings,
        client=None,
        quota=quota,
        summary=summary,
        system_prompt=REVIEW_SYSTEM_PROMPT,
        user_prompt=(
            "逐条判断以下候选表达是否值得入库，输出严格 JSON：\n"
            f"{listed}\n\n本批群消息：\n" + "\n".join(render_rows_with_aliases(batch))
        ),
        json_mode=True,
    )
    if content is None:
        metrics.EXPRESSION_TOTAL.labels("review", "error").inc()
        return [False] * len(candidates)
    decisions = _parse_review(content, len(candidates))
    if decisions is None:
        # 调用成功但没有可解析的自检结论：不推翻校验结论
        metrics.EXPRESSION_TOTAL.labels("review", "error").inc()
        return [True] * len(candidates)
    accepted = sum(1 for accept in decisions if accept)
    metrics.EXPRESSION_TOTAL.labels("review", "kept" if accepted else "rejected").inc()
    return decisions


async def _confirm_jargon_terms(
    *, batch, terms, memory_store, settings, complete, quota, summary: RoundSummary
) -> None:
    """跨过确认阈值的 unconfirmed 词：一次释义调用 → confirmed；失败保持原状。"""
    rendered = "\n".join(render_rows_with_aliases(batch))
    for term in terms:
        content = await _llm(
            complete,
            settings=settings,
            client=None,
            quota=quota,
            summary=summary,
            system_prompt=JARGON_DEFINE_SYSTEM_PROMPT,
            user_prompt=f"本批群消息：\n{rendered}\n\n需要考据的群内用语：{term}",
            json_mode=True,
        )
        definition = _parse_definition(content)
        if definition is None:
            metrics.JARGON_TOTAL.labels("confirm", "error").inc()
            continue
        try:
            confirmed = await memory_store.confirm_jargon(
                group_id=batch[0].group_id,
                term=term,
                definition=definition,
                manual=False,
            )
        except Exception:
            metrics.JARGON_TOTAL.labels("confirm", "error").inc()
            continue
        if confirmed:
            summary.jargon_confirmed += 1
            metrics.JARGON_TOTAL.labels("confirm", "ok").inc()


async def _process_batch(
    *,
    group_id: int,
    batch: list[ChatMemoryRow],
    memory_store,
    settings: BotSettings,
    complete,
    quota,
    rng,
    summary: RoundSummary,
    client=None,
) -> RoundSummary:
    """单批：合并提取 → 确定性校验 → 自检 → 入库 → 黑话确认。
    任一步失败只短路本批，不影响其它批（调用方 for 循环保证）。"""
    summary.batches += 1
    summary.messages_scanned += len(batch)
    if not batch:
        return summary

    content = await _llm(
        complete,
        settings=settings,
        client=client,
        quota=quota,
        summary=summary,
        system_prompt=EXTRACT_SYSTEM_PROMPT,
        user_prompt=_batch_prompt(batch, "提取句式模板与群内黑话，输出严格 JSON。"),
        json_mode=True,
    )
    parsed = _parse_extract(content)
    if parsed is None:
        # 解析失败：本批不产生任何入库（fail closed），其它批照常
        metrics.EXPRESSION_TOTAL.labels("extract", "error").inc()
        return summary
    expressions, jargon = parsed
    if expressions:
        metrics.EXPRESSION_TOTAL.labels("extract", "ok").inc()
    else:
        metrics.EXPRESSION_TOTAL.labels("extract", "empty").inc()

    candidates: list[tuple[str, str, ChatMemoryRow]] = []
    for item in expressions:
        template = item["template"].strip()
        quote = item["evidence_quote"].strip()
        evidence_row = _resolve_evidence_row(batch, quote)
        if evidence_row is None:
            summary.rejected_validation += 1
            metrics.EXPRESSION_TOTAL.labels("validate", "rejected").inc()
            continue
        ok, _reason = validate_expression(
            template=template, evidence_quote=quote, evidence_text=evidence_row.message_text
        )
        if not ok:
            summary.rejected_validation += 1
            metrics.EXPRESSION_TOTAL.labels("validate", "rejected").inc()
            continue
        candidates.append((template, quote, evidence_row))
    if expressions:
        if candidates:
            metrics.EXPRESSION_TOTAL.labels("validate", "kept").inc()
        else:
            metrics.EXPRESSION_TOTAL.labels("validate", "rejected").inc()
    summary.expressions_extracted += len(candidates)

    if candidates:
        decisions = await _review_candidates(
            batch=batch,
            candidates=candidates,
            settings=settings,
            complete=complete,
            quota=quota,
            summary=summary,
        )
        for (template, quote, evidence_row), accept in zip(candidates, decisions):
            if not accept:
                summary.rejected_review += 1
                continue
            _, slot_count = split_slots(template)
            try:
                outcome = await memory_store.upsert_expression(
                    group_id=group_id,
                    template=template,
                    slot_count=slot_count,
                    evidence_row_id=evidence_row.id,
                    evidence_digest=_evidence_digest(quote),
                )
            except Exception:
                metrics.EXPRESSION_TOTAL.labels("store", "error").inc()
                continue
            if outcome == "created":
                summary.stored += 1
                metrics.EXPRESSION_TOTAL.labels("store", "ok").inc()
            else:
                summary.duplicated += 1
                metrics.EXPRESSION_TOTAL.labels("store", "duplicate").inc()

    for term in jargon:
        try:
            outcome = await memory_store.upsert_jargon(group_id=group_id, term=term)
        except Exception:
            metrics.JARGON_TOTAL.labels("store", "error").inc()
            continue
        if outcome == "new":
            summary.jargon_new += 1
            metrics.JARGON_TOTAL.labels("store", "new").inc()
        else:
            summary.jargon_hit += 1
            metrics.JARGON_TOTAL.labels("store", "hit").inc()
        # 本批文本里真实出现（中文直匹配）→ 再计一次命中；新词此时 hits=1，
        # 重复 upsert 同一词在同一批里只是把这次出现也记进去（spec e.：
        # 候选词 upsert 后，表内词在本批文本子串命中 → hits+1）
        if not any(term in row.message_text for row in batch):
            continue
        try:
            await memory_store.upsert_jargon(group_id=group_id, term=term)
        except Exception:
            metrics.JARGON_TOTAL.labels("store", "error").inc()
            continue
        summary.jargon_hit += 1
        metrics.JARGON_TOTAL.labels("store", "hit").inc()
        try:
            entry = await memory_store.get_jargon(group_id=group_id, term=term)
        except Exception:
            entry = None
        if entry is None:
            continue
        if (
            entry.status == "unconfirmed"
            and entry.manual == 0
            and entry.hits >= settings.jargon_confirm_hits
        ):
            await _confirm_jargon_terms(
                batch=batch,
                terms=[term],
                memory_store=memory_store,
                settings=settings,
                complete=complete,
                quota=quota,
                summary=summary,
            )
        else:
            metrics.JARGON_TOTAL.labels("confirm", "skipped").inc()
    return summary


async def run_learning_round(
    *,
    group_id: int,
    memory_store,
    settings: BotSettings,
    complete,
    quota=None,
    rng=random.random,
    now=None,
) -> RoundSummary:
    """一轮学习：配额预检 → 取材 → 切批 → 逐批学习 → 刷新 gauge。
    ``complete`` 是 ai_client.request_completion（生产）或测试替身；
    ``rng`` 仅为接口预留（本任务抽样未接），随 Task 4 注入链路启用。"""
    s = RoundSummary(status="ok")
    if not settings.expression_learn_enabled:
        return RoundSummary(status="disabled")
    if quota is not None:
        try:
            usage = await quota.summary(scope_type=_QUOTA_SCOPE, scope_id=0)
            if int(usage.get("requests", 0)) >= settings.expression_learn_daily_limit:
                return RoundSummary(status="quota_denied")
        except Exception:
            pass  # 配额通道不可用不阻塞（调用侧仍尽力记账）
    try:
        rows = await memory_store.messages_since(
            group_id=group_id, days=settings.expression_learn_lookback_days, now=now
        )
    except Exception as exc:
        record_error(_LOGGER_NAME, classify_exception(exc).category.value)
        metrics.EXPRESSION_TOTAL.labels("extract", "error").inc()
        return RoundSummary(status="error")
    batch_size = settings.expression_learn_batch_messages
    batches = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)][
        -settings.expression_learn_max_batches :
    ]  # 最新优先：取末尾 N 批
    if not batches:
        return RoundSummary(status="insufficient_material")

    for batch in batches:
        s = await _process_batch(
            group_id=group_id,
            batch=batch,
            memory_store=memory_store,
            settings=settings,
            complete=complete,
            quota=quota,
            rng=rng,
            summary=s,
            client=None,
        )
    try:
        active = await memory_store.list_active_expressions(group_id=group_id)
        metrics.EXPRESSION_ACTIVE.labels(_group_hash(group_id)).set(len(active))
    except Exception:
        pass
    return s
