from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from qq_bot.services.chat_memory import ChatMemoryRow


class MessageSegmentLike(Protocol):
    type: str
    data: dict[str, object]


@dataclass(frozen=True)
class MemoryReference:
    question: str
    user_id: int | None = None
    keyword: str | None = None
    limit: int | None = None


def extract_at_user_ids(segments: Iterable[MessageSegmentLike]) -> list[int]:
    user_ids: list[int] = []
    for segment in segments:
        if segment.type != "at":
            continue
        try:
            user_ids.append(int(segment.data.get("qq", "")))
        except (TypeError, ValueError):
            continue
    return user_ids


def extract_at_user_ids_before_separator(segments: Iterable[MessageSegmentLike]) -> list[int]:
    user_ids: list[int] = []
    for segment in segments:
        if segment.type == "text" and re.search(r"[：:]", str(segment.data.get("text", ""))):
            break
        if segment.type != "at":
            continue
        try:
            user_ids.append(int(segment.data.get("qq", "")))
        except (TypeError, ValueError):
            continue
    return user_ids


def parse_memory_reference(prompt: str, *, mentioned_user_ids: list[int]) -> MemoryReference:
    text = prompt.strip()
    if not text.startswith("参考"):
        return MemoryReference(question=text)

    separator_match = re.search(r"[：:]", text)
    if not separator_match:
        return MemoryReference(question=text)

    head = text[: separator_match.start()].strip()
    question = text[separator_match.end() :].strip()
    limit_match = re.search(r"最近\s*(\d+)\s*条", head)
    limit = int(limit_match.group(1)) if limit_match else None
    keyword = _extract_keyword(head)
    user_id = mentioned_user_ids[0] if _references_mentioned_user(head, mentioned_user_ids) else None
    if limit is None and keyword is None and user_id is None:
        return MemoryReference(question=text)

    return MemoryReference(question=question, user_id=user_id, keyword=keyword, limit=limit)


def format_chat_context(rows: list[ChatMemoryRow]) -> str:
    if not rows:
        return "没有找到相关历史聊天记录。"

    lines = ["历史聊天记录："]
    for row in rows:
        lines.append(f"用户{row.user_id}：{row.message_text}")
        if row.ai_reply:
            lines.append(f"机器人：{row.ai_reply}")
    return "\n".join(lines)


def _extract_keyword(head: str) -> str | None:
    keyword_match = re.search(r"关于\s*(.*?)\s*的聊天", head)
    if not keyword_match:
        keyword_match = re.search(r"参考\s*(.*?)\s*的聊天", head)
    if not keyword_match:
        return None

    keyword = keyword_match.group(1).strip()
    keyword = re.sub(r"@\S+", "", keyword).strip()
    if not keyword or re.fullmatch(r"最近\s*\d*\s*条?", keyword):
        return None
    return keyword


def _references_mentioned_user(head: str, mentioned_user_ids: list[int]) -> bool:
    if not mentioned_user_ids:
        return False
    if "@" in head:
        return True
    return bool(re.search(r"参考\s*(?:的)?\s*最近\s*\d*\s*条", head))
