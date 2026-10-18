"""确定性错别字注入（S10-HUMAN-P0 H3，参考 MaiBot typo_generator 思路）：
近音字替换 + 相邻字换位，零依赖。只作用于自主生成的口语回复；+1 跟发、
含数字/字母/命令/URL 的回复永不注入；每条最多 1 处；表内均为常用混淆字对，
不可能引入新敏感词。"""

from __future__ import annotations

import re
from typing import Callable

# 高频近音/形近混淆对（双向展开即映射表）。来源：中文输入法高频错字常识集。
_TYPO_PAIRS: tuple[tuple[str, str], ...] = (
    ("在", "再"),
    ("再", "在"),
    ("做", "作"),
    ("作", "做"),
    ("那", "哪"),
    ("哪", "那"),
    ("的", "得"),
    ("得", "的"),
    ("地", "得"),
    ("以", "已"),
    ("已", "以"),
    ("象", "像"),
    ("像", "象"),
    ("带", "待"),
    ("待", "带"),
    ("竞", "竟"),
    ("竟", "竞"),
    ("决", "绝"),
    ("绝", "决"),
    ("度", "渡"),
    ("渡", "度"),
    ("汇", "会"),
    ("会", "汇"),
    ("记", "纪"),
    ("纪", "记"),
    ("形", "型"),
    ("型", "形"),
    ("账", "帐"),
    ("帐", "账"),
    ("园", "圆"),
    ("圆", "园"),
    ("密", "秘"),
    ("秘", "密"),
    ("返", "反"),
    ("反", "返"),
    ("联", "连"),
    ("连", "联"),
    ("练", "炼"),
    ("炼", "练"),
    ("末", "未"),
    ("未", "末"),
    ("候", "侯"),
    ("侯", "候"),
    ("奈", "耐"),
    ("耐", "奈"),
)
_TYPO_MAP = dict(_TYPO_PAIRS)

# 任意数字即豁免（"版本2.0更新了" 这类含数字回复永不注入，docstring 契约）
_DIGIT_RE = re.compile(r"\d")
_ASCII_RE = re.compile(r"[A-Za-z]")


def _swap_adjacent(chars: list[str], rng: Callable[[], float]) -> bool:
    idxs = [
        i
        for i in range(len(chars) - 1)
        if not chars[i].isspace() and not chars[i + 1].isspace() and chars[i] != chars[i + 1]
    ]
    if not idxs:
        return False
    i = idxs[int(rng() * len(idxs)) % len(idxs)]
    chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return True


def maybe_apply_typo(text: str, *, rate: float, rng: Callable[[], float]) -> tuple[str, bool]:
    """满足全部条件才注入一处错别字，返回 (文本, 是否注入)。"""
    if rate <= 0 or rng() >= rate:
        return text, False
    core = re.sub(r"\s+", "", text)
    if len(core) < 4:
        return text, False
    if _DIGIT_RE.search(text) or _ASCII_RE.search(text):
        return text, False
    if "http" in text or text.startswith("/"):
        return text, False
    chars = list(text)
    eligible = [i for i, ch in enumerate(chars) if ch in _TYPO_MAP]
    swap_ok = len(core) >= 6
    if eligible and (not swap_ok or rng() < 0.7):
        i = eligible[int(rng() * len(eligible)) % len(eligible)]
        chars[i] = _TYPO_MAP[chars[i]]
        return "".join(chars), True
    if swap_ok and _swap_adjacent(chars, rng):
        return "".join(chars), True
    return text, False
