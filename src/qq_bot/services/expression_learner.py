"""表达学习管线（S10-HUMAN P1 H5/H6）：从群聊语料提取槽位化句式模板与黑话，
确定性校验 + LLM 自检双闸后入库，运行时注入为纯 DB 读。
证据原文不落库（仅 row_id + sha256 摘要）；模板/词条不进日志与指标。"""

from __future__ import annotations

import re

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
