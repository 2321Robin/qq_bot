"""视觉输入/输出桥（S11-VISION-01，V1 + V2-lite）。

把 OneBot v11 消息段变成多模态模型输入，把模型回复里的发图标记变成可发送
的段。纯数据转换，不依赖 nonebot——测试与插件两侧都只喂 ``type``/``data``
形态的对象。

读（入站）：

- ``image`` 段：``data.url`` 优先（公网直链，多模态 API 直接取）；``file``
  为本地路径且视觉开启时读文件转 base64 data URL（NapCat 与机器人同机部署
  才有本地路径，读不到就丢弃该张）。
- ``face`` 段：QQ 经典小表情，渲染 ``[表情:名字]`` 文本占位——NapCat 自带
  ``text`` 时优先，否则查内置 id 表，未知 id 留 ``[表情#id]``。
- ``mface`` 段：商城表情包，渲染 ``[表情包:文本]`` 占位，url 同时作为图片
  候选（视觉开启时）。

历史记忆不走图片入库：带图消息在 ``stored_text`` 末尾追加 ``[图片]`` 占位
（V2 设计文档的决策——历史图只留占位符控制 token 成本），表情占位同样进
记忆，让后续轮次知道"刚才那张图/表情"的存在。

发（出站）：模型在回复文本里插 ``[发图:ID]`` / ``[发脸:名字]`` 标记，
``parse_reply_parts`` 确定性解析成 ReplyPart 序列（text/sticker/face）交插件
拼装。未知 ID/名字、超上限的标记整段丢弃并计数，绝不把标记原文发给群友。
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("qq_bot.vision")

# ---- QQ 经典小表情 id → 名（OneBot v11 / CoolQ 标准表，0~98）----
# 仅作 NapCat 未带 text 时的兜底；个别 id 修正以实际客户端为准，未知一律
# 回落 [表情#id]，不猜。
QQ_FACE_NAMES: dict[int, str] = {
    0: "微笑",
    1: "撇嘴",
    2: "色",
    3: "发呆",
    4: "得意",
    5: "流泪",
    6: "害羞",
    7: "闭嘴",
    8: "睡",
    9: "大哭",
    10: "尴尬",
    11: "发怒",
    12: "调皮",
    13: "呲牙",
    14: "惊讶",
    15: "难过",
    16: "囧",
    17: "抓狂",
    18: "吐",
    19: "偷笑",
    20: "可爱",
    21: "白眼",
    22: "傲慢",
    23: "饥饿",
    24: "困",
    25: "惊恐",
    26: "流汗",
    27: "憨笑",
    28: "悠闲",
    29: "奋斗",
    30: "咒骂",
    31: "疑问",
    32: "嘘",
    33: "晕",
    34: "衰",
    35: "骷髅",
    36: "敲打",
    37: "再见",
    38: "擦汗",
    39: "抠鼻",
    40: "鼓掌",
    41: "坏笑",
    42: "右哼哼",
    43: "鄙视",
    44: "委屈",
    45: "快哭了",
    46: "阴险",
    47: "亲亲",
    48: "吓",
    49: "可怜",
    50: "菜刀",
    51: "西瓜",
    52: "啤酒",
    53: "篮球",
    54: "乒乓",
    55: "咖啡",
    56: "饭",
    57: "猪头",
    58: "玫瑰",
    59: "凋谢",
    60: "嘴唇",
    61: "爱心",
    62: "心碎",
    63: "蛋糕",
    64: "闪电",
    65: "炸弹",
    66: "刀",
    67: "足球",
    68: "瓢虫",
    69: "便便",
    70: "月亮",
    71: "太阳",
    72: "礼物",
    73: "拥抱",
    74: "强",
    75: "弱",
    76: "握手",
    77: "胜利",
    78: "抱拳",
    79: "勾引",
    80: "拳头",
    81: "差劲",
    82: "爱你",
    83: "NO",
    84: "OK",
    85: "爱情",
    86: "飞吻",
    87: "跳跳",
    88: "发抖",
    89: "转圈",
    90: "磕头",
    91: "回头",
    92: "跳绳",
    93: "投降",
    94: "激动",
    95: "乱舞",
    96: "献吻",
    97: "左太极",
    98: "右太极",
}

# 机器人主动回表情的白名单（进 prompt 的可选名单，也是发脸标记的校验表）：
# 只收日常聊天里高频、语义无歧义的名字，避免小模型拿"菜刀/炸弹"玩梗翻车。
QA_FACE_WHITELIST: tuple[str, ...] = (
    "呲牙",
    "偷笑",
    "害羞",
    "微笑",
    "撇嘴",
    "大哭",
    "流泪",
    "疑问",
    "晕",
    "抓狂",
    "白眼",
    "憨笑",
    "流汗",
    "尴尬",
    "可爱",
    "鼓掌",
    "坏笑",
    "鄙视",
    "委屈",
    "亲亲",
    "可怜",
    "玫瑰",
    "爱心",
    "心碎",
    "蛋糕",
    "强",
    "握手",
    "胜利",
    "抱拳",
    "OK",
    "爱你",
    "飞吻",
    "再见",
    "擦汗",
)

_FACE_NAME_TO_ID: dict[str, int] = {
    name: face_id for face_id, name in QQ_FACE_NAMES.items() if name in QA_FACE_WHITELIST
}


def face_id_for_name(name: str) -> int | None:
    """白名单表情名 → QQ face id；不在白名单返回 None（调用方丢标记）。"""
    return _FACE_NAME_TO_ID.get(name.strip())


# ---- 入站提取 ----

_MIME_BY_SUFFIX = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
_DEFAULT_MIME = "image/jpeg"
_IMAGE_DATA_LIMIT_BYTES = 8 * 1024 * 1024  # 本地文件转 base64 的单张上限


@dataclass(frozen=True)
class VisionInputs:
    """一条消息的视觉侧提取结果。

    ``images`` 是可直接放进多模态请求的图片源（公网 URL 或 base64 data
    URL）；``face_tags`` 是 ``[表情:名字]``/``[表情包:文本]`` 占位；总图片
    段数 ``image_segments`` 与实际携带数之差即被丢弃数（视觉关闭时全部
    不携带属正常，不计丢弃）。
    """

    images: tuple[str, ...] = ()
    face_tags: tuple[str, ...] = ()
    image_segments: int = 0
    dropped_images: int = 0

    @property
    def has_attachments(self) -> bool:
        return self.image_segments > 0 or bool(self.face_tags)


@dataclass
class _ExtractorState:
    images: list[str] = field(default_factory=list)
    face_tags: list[str] = field(default_factory=list)
    image_segments: int = 0
    dropped: int = 0


def _read_local_image(path: Path) -> str | None:
    """本地图片 → base64 data URL；不存在/过大/不可读返回 None。"""
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > _IMAGE_DATA_LIMIT_BYTES:
        return None
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), _DEFAULT_MIME)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _image_source(data: dict, *, read_local: Callable[[Path], str | None]) -> str | None:
    """image/mface 段 → 模型可用的图片源：公网 URL 优先，本地路径兜底。"""
    url = data.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    file_field = data.get("file")
    if isinstance(file_field, str) and file_field.startswith(("http://", "https://")):
        return file_field
    if isinstance(file_field, str) and file_field:
        # NapCat 本地路径（file:/// 或裸路径）；同机部署才读得到
        raw = file_field.removeprefix("file:///").removeprefix("file://")
        path = Path(raw)
        if path.is_absolute():
            return read_local(path)
    return None


def extract_vision_inputs(
    segments: Iterable[object],
    *,
    vision_enabled: bool,
    max_images: int,
    read_local: Callable[[Path], str | None] = _read_local_image,
) -> VisionInputs:
    """从消息段序列提取图片源与表情占位。

    ``vision_enabled=False`` 时只保留计数与表情占位（零 IO、零 URL 提取），
    关闭开关后行为与纯文本版对齐。"""
    state = _ExtractorState()
    for segment in segments:
        seg_type = getattr(segment, "type", None)
        data = getattr(segment, "data", None)
        if not isinstance(data, dict):
            continue
        if seg_type == "image":
            state.image_segments += 1
            if not vision_enabled:
                continue
            source = _image_source(data, read_local=read_local)
            if source is None:
                logger.warning("vision image segment has no usable source; dropped")
                state.dropped += 1
            elif len(state.images) < max_images:
                state.images.append(source)
            else:
                state.dropped += 1
        elif seg_type == "mface":
            state.image_segments += 1
            text = data.get("text")
            tag = (
                f"[表情包:{text.strip()}]" if isinstance(text, str) and text.strip() else "[表情包]"
            )
            state.face_tags.append(tag)
            if not vision_enabled:
                continue
            source = _image_source(data, read_local=read_local)
            if source is None:
                state.dropped += 1
            elif len(state.images) < max_images:
                state.images.append(source)
            else:
                state.dropped += 1
        elif seg_type == "face":
            state.face_tags.append(_render_face_tag(data))
    return VisionInputs(
        images=tuple(state.images),
        face_tags=tuple(state.face_tags),
        image_segments=state.image_segments,
        dropped_images=state.dropped if vision_enabled else 0,
    )


def _render_face_tag(data: dict) -> str:
    """face 段 → [表情:名字]；NapCat text 优先，内置表兜底，未知留 id。"""
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        return f"[表情:{text.strip()}]"
    raw_id = data.get("id")
    try:
        face_id = int(str(raw_id))
    except (TypeError, ValueError):
        return "[表情]"
    name = QQ_FACE_NAMES.get(face_id)
    return f"[表情:{name}]" if name else f"[表情#{face_id}]"


def append_attachment_tags(text: str, vision: VisionInputs) -> str:
    """把 [图片] / 表情占位追加到记忆文本末尾（历史轮次知道有图/有表情）。"""
    tags = ""
    if vision.image_segments:
        tags += "[图片]"
    tags += "".join(vision.face_tags)
    if not tags:
        return text
    return f"{text} {tags}".strip()


# ---- 近期图片回忆 ----

_RECALL_MAX_PER_GROUP = 3
_RECALL_MAX_GROUPS = 32


@dataclass(frozen=True)
class RecalledImage:
    """回捞结果：TTL 内该群最近一条带图消息的图片源与发送者。

    发送者随图一并交还，调用方才能在 prompt 里说清"这张图是谁发的"
    （2026-10-25 线上实测：模型看得到图但不知道归属，把 A 提问的图当成
    A 发的，回错对象）。"""

    urls: tuple[str, ...]
    sender_user_id: int | None


class ImageRecallBuffer:
    """进程内近期图片回忆（S11-VISION-01 跨消息补丁）。

    QQ 群聊习惯是"先发表情包/截图，再 @ 机器人说话"——两条消息，@那条
    本身不带图。问答链只看 @消息自身时永远看不到图（2026-10-25 线上实测：
    视觉计数为零，机器人只能念记忆里的 [图片] 占位）。此缓冲在收到带图
    消息时记住图片源与发送者；@消息自己没带图时按 TTL 回捞同群最近一张。

    只存进程内存（重启即空），不写库、不进日志——图片 URL 的暴露面与
    消息本身相同，且远短于 provider 侧缓存；与 [图片] 占位同一条隐私红线。
    时钟可注入（测试），结构与 quote_reference 的 LRU 同族。"""

    def __init__(
        self,
        *,
        max_per_group: int = _RECALL_MAX_PER_GROUP,
        max_groups: int = _RECALL_MAX_GROUPS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_per_group = max(1, max_per_group)
        self._max_groups = max(1, max_groups)
        self._clock = clock
        self._entries: dict[int, deque[tuple[float, tuple[str, ...], int | None]]] = {}
        self._order: deque[int] = deque()

    def remember(self, group_id: int, urls: Sequence[str], sender_user_id: int) -> None:
        """记录一条带图消息的图片源与发送者（空 urls 忽略；每群只留最近 N 条）。"""
        cleaned = tuple(url for url in urls if url)
        if not cleaned:
            return
        now = self._clock()
        entries = self._entries.get(group_id)
        if entries is None:
            if len(self._entries) >= self._max_groups:
                oldest = self._order.popleft()
                self._entries.pop(oldest, None)
            entries = deque(maxlen=self._max_per_group)
            self._entries[group_id] = entries
            self._order.append(group_id)
        entries.append((now, cleaned, sender_user_id))

    def recall(self, group_id: int, *, ttl_seconds: float) -> RecalledImage | None:
        """TTL 内该群最近一条带图消息的图片源与发送者；过期/无记录返回 None。"""
        if ttl_seconds <= 0:
            return None
        entries = self._entries.get(group_id)
        if not entries:
            return None
        now = self._clock()
        for timestamp, urls, sender_user_id in reversed(entries):
            if now - timestamp <= ttl_seconds:
                return RecalledImage(urls=urls, sender_user_id=sender_user_id)
        return None


_SHARED_IMAGE_RECALL: ImageRecallBuffer | None = None


def shared_image_recall() -> ImageRecallBuffer:
    """进程级回忆缓冲单例（同 shared_quote_lru 的形态）。"""
    global _SHARED_IMAGE_RECALL
    if _SHARED_IMAGE_RECALL is None:
        _SHARED_IMAGE_RECALL = ImageRecallBuffer()
    return _SHARED_IMAGE_RECALL


# ---- 出站发图标记 ----

_REPLY_MARKER_RE = re.compile(r"\[(发图|发脸)\s*[:：]\s*([^\[\]]{1,64})\]")
MAX_REPLY_STICKERS = 2
MAX_REPLY_FACES = 3


@dataclass(frozen=True)
class ReplyPart:
    """回复的一部分：text 原文 / sticker 库 ID / face 白名单表情名。"""

    kind: str
    value: str


@dataclass(frozen=True)
class ParsedReply:
    parts: tuple[ReplyPart, ...]
    stickers: int
    faces: int
    dropped: int


def parse_reply_parts(
    reply: str, *, max_stickers: int = MAX_REPLY_STICKERS, max_faces: int = MAX_REPLY_FACES
) -> ParsedReply:
    """解析回复文本里的 [发图:ID]/[发脸:名字] 标记（支持全角冒号）。

    标记之间的文本成为 text 段（保序）；未知/超额标记整段丢弃并计入
    ``dropped``。没有任何标记时 parts 就是原回复本身，调用方走纯文本路径。"""
    parts: list[ReplyPart] = []
    stickers = faces = dropped = 0
    cursor = 0
    for match in _REPLY_MARKER_RE.finditer(reply):
        before = reply[cursor : match.start()].strip()
        if before:
            parts.append(ReplyPart(kind="text", value=before))
        cursor = match.end()
        action, value = match.group(1), match.group(2).strip()
        if action == "发图":
            if stickers >= max_stickers:
                dropped += 1
                continue
            stickers += 1
            parts.append(ReplyPart(kind="sticker", value=value))
        else:
            if faces >= max_faces:
                dropped += 1
                continue
            faces += 1
            parts.append(ReplyPart(kind="face", value=value))
    tail = reply[cursor:].strip()
    if tail:
        parts.append(ReplyPart(kind="text", value=tail))
    return ParsedReply(parts=tuple(parts), stickers=stickers, faces=faces, dropped=dropped)


def strip_reply_markers(reply: str) -> str:
    """仅剥掉发图标记保留文本（标记全部不可用时的兜底文案）。"""
    text = _REPLY_MARKER_RE.sub("", reply)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
