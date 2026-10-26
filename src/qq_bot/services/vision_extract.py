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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("qq_bot.vision")

# ---- QQ 系统小表情 id → 名（NapCat face_config ∪ koishijs/QFace，0~484）。----
# 2026-10-25 重建：旧表按 CoolQ 时代口径（0=微笑）凭记忆手写，与新版 QQNT
# 实际编号错位（实为 0=惊讶、14=微笑，偷笑/白眼等一批同样偏移），读图标错名、
# 发脸发错表情。现整表合并两份腾讯官方数据源：NapCat ``face_config.json``（本
# 部署协议端给收到的表情段填 ``text`` 的同一份数据，读发两侧名称一致）为主，
# koishijs/QFace 补其缺口（惊呆/震惊/嗑到了等）；450+ 为官方经典表情重制批，
# 重名反查一律取最小 id（经典段优先）。
QQ_FACE_NAMES: dict[int, str] = {
    0: "惊讶",
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
    14: "微笑",
    15: "难过",
    16: "酷",
    18: "抓狂",
    19: "吐",
    20: "偷笑",
    21: "可爱",
    22: "白眼",
    23: "傲慢",
    24: "饥饿",
    25: "困",
    26: "惊恐",
    27: "流汗",
    28: "憨笑",
    29: "悠闲",
    30: "奋斗",
    31: "咒骂",
    32: "疑问",
    33: "嘘",
    34: "晕",
    35: "折磨",
    36: "衰",
    37: "骷髅",
    38: "敲打",
    39: "再见",
    41: "发抖",
    42: "爱情",
    43: "跳跳",
    46: "猪头",
    49: "拥抱",
    53: "蛋糕",
    54: "闪电",
    55: "炸弹",
    56: "刀",
    57: "足球",
    59: "便便",
    60: "咖啡",
    61: "饭",
    63: "玫瑰",
    64: "凋谢",
    66: "爱心",
    67: "心碎",
    69: "礼物",
    74: "太阳",
    75: "月亮",
    76: "赞",
    77: "踩",
    78: "握手",
    79: "胜利",
    85: "飞吻",
    86: "怄火",
    89: "西瓜",
    96: "冷汗",
    97: "擦汗",
    98: "抠鼻",
    99: "鼓掌",
    100: "糗大了",
    101: "坏笑",
    102: "左哼哼",
    103: "右哼哼",
    104: "哈欠",
    105: "鄙视",
    106: "委屈",
    107: "快哭了",
    108: "阴险",
    109: "左亲亲",
    110: "吓",
    111: "可怜",
    112: "菜刀",
    113: "啤酒",
    114: "篮球",
    115: "乒乓",
    116: "示爱",
    117: "瓢虫",
    118: "抱拳",
    119: "勾引",
    120: "拳头",
    121: "差劲",
    122: "爱你",
    123: "NO",
    124: "OK",
    125: "转圈",
    126: "磕头",
    127: "回头",
    128: "跳绳",
    129: "挥手",
    130: "激动",
    131: "街舞",
    132: "献吻",
    133: "左太极",
    134: "右太极",
    136: "双喜",
    137: "鞭炮",
    138: "灯笼",
    140: "K歌",
    144: "喝彩",
    145: "祈祷",
    146: "爆筋",
    147: "棒棒糖",
    148: "喝奶",
    151: "飞机",
    158: "钞票",
    168: "药",
    169: "手枪",
    171: "茶",
    172: "眨眼睛",
    173: "泪奔",
    174: "无奈",
    175: "卖萌",
    176: "小纠结",
    177: "喷血",
    178: "斜眼笑",
    179: "doge",
    180: "惊喜",
    181: "戳一戳",
    182: "笑哭",
    183: "我最美",
    184: "河蟹",
    185: "羊驼",
    187: "幽灵",
    188: "蛋",
    190: "菊花",
    192: "红包",
    193: "大笑",
    194: "不开心",
    197: "冷漠",
    198: "呃",
    199: "好棒",
    200: "求求",
    201: "点赞",
    202: "无聊",
    203: "托脸",
    204: "吃",
    205: "送花",
    206: "害怕",
    207: "花痴",
    208: "小样儿",
    210: "飙泪",
    211: "我不看",
    212: "托腮",
    214: "啵啵",
    215: "糊脸",
    216: "拍头",
    217: "扯一扯",
    218: "舔一舔",
    219: "蹭一蹭",
    220: "拽炸天",
    221: "顶呱呱",
    222: "抱抱",
    223: "暴击",
    224: "开枪",
    225: "撩一撩",
    226: "拍桌",
    227: "拍手",
    228: "恭喜",
    229: "干杯",
    230: "嘲讽",
    231: "哼",
    232: "佛系",
    233: "掐一掐",
    234: "惊呆",
    235: "颤抖",
    236: "啃头",
    237: "偷看",
    238: "扇脸",
    239: "原谅",
    240: "喷脸",
    241: "生日快乐",
    242: "头撞击",
    243: "甩头",
    244: "扔狗",
    245: "加油必胜",
    246: "加油抱抱",
    247: "口罩护体",
    260: "搬砖中",
    261: "忙到飞起",
    262: "脑阔疼",
    263: "沧桑",
    264: "捂脸",
    265: "辣眼睛",
    266: "哦哟",
    267: "头秃",
    268: "问号脸",
    269: "暗中观察",
    270: "emm",
    271: "吃瓜",
    272: "呵呵哒",
    273: "我酸了",
    274: "太南了",
    276: "辣椒酱",
    277: "汪汪",
    278: "汗",
    279: "打脸",
    280: "击掌",
    281: "无眼笑",
    282: "敬礼",
    283: "狂笑",
    284: "面无表情",
    285: "摸鱼",
    286: "魔鬼笑",
    287: "哦",
    288: "请",
    289: "睁眼",
    290: "敲开心",
    291: "震惊",
    292: "让我康康",
    293: "摸锦鲤",
    294: "期待",
    295: "拿到红包",
    296: "真好",
    297: "拜谢",
    298: "元宝",
    299: "牛啊",
    300: "胖三斤",
    301: "好闪",
    302: "左拜年",
    303: "右拜年",
    304: "红包包",
    305: "右亲亲",
    306: "牛气冲天",
    307: "喵喵",
    308: "求红包",
    309: "谢红包",
    310: "新年烟花",
    311: "打call",
    312: "变形",
    313: "嗑到了",
    314: "仔细分析",
    315: "加油",
    316: "我没事",
    317: "菜汪",
    318: "崇拜",
    319: "比心",
    320: "庆祝",
    321: "老色痞",
    322: "拒绝",
    323: "嫌弃",
    324: "吃糖",
    325: "惊吓",
    326: "生气",
    327: "加一",
    328: "错号",
    329: "对号",
    330: "完成",
    331: "明白",
    332: "举牌牌",
    333: "烟花",
    334: "虎虎生威",
    336: "豹富",
    337: "花朵脸",
    338: "我想开了",
    339: "舔屏",
    340: "热化了",
    341: "打招呼",
    342: "酸Q",
    343: "我方了",
    344: "大怨种",
    345: "红包多多",
    346: "你真棒棒",
    347: "大展宏兔",
    348: "福萝卜",
    349: "坚强",
    350: "贴贴",
    351: "敲敲",
    352: "咦",
    353: "拜托",
    354: "尊嘟假嘟",
    355: "耶",
    356: "666",
    357: "裂开",
    358: "骰子",
    359: "包剪锤",
    360: "亲亲",
    361: "狗狗笑哭",
    362: "好兄弟",
    363: "狗狗可怜",
    364: "超级赞",
    365: "狗狗生气",
    366: "芒狗",
    367: "狗狗疑问",
    368: "奥特笑哭",
    369: "彩虹",
    370: "祝贺",
    371: "冒泡",
    372: "气呼呼",
    373: "忙",
    374: "波波流泪",
    375: "超级鼓掌",
    376: "跺脚",
    377: "嗨",
    378: "企鹅笑哭",
    379: "企鹅流泪",
    380: "真棒",
    381: "路过",
    382: "emo",
    383: "企鹅爱心",
    384: "晚安",
    385: "太气了",
    386: "呜呜呜",
    387: "太好笑",
    388: "太头疼",
    389: "太赞了",
    390: "太头秃",
    391: "太沧桑",
    392: "龙年快乐",
    393: "新年中龙",
    394: "新年大龙",
    395: "略略略",
    396: "狼狗",
    397: "抛媚眼",
    398: "超级ok",
    399: "tui",
    400: "快乐",
    401: "超级转圈",
    402: "别说话",
    403: "出去玩",
    404: "闪亮登场",
    405: "好运来",
    406: "姐是女王",
    407: "我听听",
    408: "臭美",
    409: "送你花花",
    410: "么么哒",
    411: "一起嗨",
    412: "开心",
    413: "摇起来",
    415: "划龙舟",
    416: "中龙舟",
    417: "大龙舟",
    419: "火车",
    420: "中火车",
    421: "大火车",
    422: "粽于等到你",
    423: "复兴号",
    424: "续标识",
    425: "求放过",
    426: "玩火",
    427: "偷感",
    428: "收到",
    429: "蛇年快乐",
    430: "蛇身",
    431: "蛇尾",
    432: "灵蛇献瑞",
    450: "撇嘴",
    451: "色",
    452: "微笑",
    453: "发呆",
    454: "得意",
    455: "害羞",
    456: "闭嘴",
    457: "睡",
    458: "我吗",
    459: "优雅",
    460: "硬撑",
    461: "宕机",
    462: "无语",
    463: "新年快乐",
    464: "马上到",
    465: "拆红包",
    466: "羞羞哒",
    467: "摇花手",
    468: "失眠",
    469: "坚毅",
    470: "马到成功",
    472: "心动",
    474: "给你一拳",
    475: "干饭",
    476: "不是吧",
    477: "你懂的",
    478: "对的对的",
    479: "不对不对",
    480: "散味儿",
    481: "学习",
    482: "热化了",
    483: "略",
    484: "比爱心",
}

# 机器人主动回表情的白名单（进 prompt 的可选名单，也是发脸标记的校验表）：
# 经典段保日常聊天基本盘，现代段收 2023+ 新表情（咦/吃瓜/嗑到了/裂开…）。
# 只收语义无歧义的名字，避免小模型拿"菜刀/炸弹"玩梗翻车。
QA_FACE_WHITELIST: tuple[str, ...] = (
    "呲牙",
    "偷笑",
    "害羞",
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
    "惊恐",
    "可爱",
    "鼓掌",
    "坏笑",
    "鄙视",
    "委屈",
    "可怜",
    "玫瑰",
    "爱心",
    "心碎",
    "蛋糕",
    "握手",
    "胜利",
    "抱拳",
    "OK",
    "爱你",
    "飞吻",
    "再见",
    "擦汗",
    "咦",
    "吃瓜",
    "打call",
    "抱抱",
    "拒绝",
    "拍手",
    "捂脸",
    "emm",
    "呵呵哒",
    "无眼笑",
    "摸鱼",
    "让我康康",
    "比心",
    "嫌弃",
    "笑哭",
    "裂开",
    "耶",
    "尊嘟假嘟",
    "emo",
    "点赞",
    "害怕",
    "原谅",
    "生气",
    "泪奔",
    "无奈",
    "呜呜呜",
)

_FACE_NAME_TO_ID: dict[str, int] = {
    name: face_id for face_id, name in QQ_FACE_NAMES.items() if name in QA_FACE_WHITELIST
}

FACE_ID_MAX = 1000

# 发送可行性最终裁判：本部署 NapCat 容器里 QQ 客户端（3.2.32）自带的
# face_config.json 收录的 id。客户端不认识的 face 段会被静默丢弃——
# 2026-10-25 实测：模型正确输出 [发脸:#493] 且 API 返回成功，但群里
# 什么都没出现，用户以为机器人撒谎。静态数据表（NapCat/QFace）收录的
# id 可能超出客户端范围（如 收到=428、嗑到了=313），不能作为发送依据。
# QQ 镜像升级后需从容器 /app/.config/QQ/nt_qq/global/nt_data/Emoji/
# emoji-resource/face_config.json 重新生成本表。
CLIENT_KNOWN_FACE_IDS: frozenset[int] = frozenset(
    {
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        41,
        42,
        43,
        46,
        49,
        53,
        55,
        56,
        59,
        60,
        63,
        64,
        66,
        67,
        74,
        75,
        76,
        77,
        78,
        79,
        85,
        86,
        89,
        96,
        97,
        98,
        99,
        100,
        101,
        102,
        103,
        104,
        105,
        106,
        107,
        108,
        109,
        110,
        111,
        112,
        114,
        116,
        118,
        119,
        120,
        121,
        122,
        123,
        124,
        125,
        129,
        137,
        144,
        146,
        147,
        148,
        169,
        171,
        172,
        173,
        174,
        175,
        176,
        177,
        178,
        179,
        180,
        181,
        182,
        183,
        185,
        187,
        193,
        194,
        198,
        200,
        201,
        202,
        203,
        204,
        206,
        210,
        211,
        212,
        214,
        215,
        216,
        217,
        218,
        219,
        221,
        222,
        223,
        224,
        225,
        226,
        227,
        229,
        230,
        231,
        232,
        233,
        235,
        237,
        238,
        239,
        240,
        241,
        243,
        244,
        262,
        263,
        264,
        265,
        266,
        267,
        268,
        269,
        270,
        271,
        272,
        273,
        277,
        278,
        281,
        282,
        283,
        284,
        285,
        286,
        287,
        288,
        289,
        290,
        292,
        293,
        294,
        295,
        297,
        298,
        299,
        300,
        301,
        302,
        303,
        305,
        306,
        307,
        311,
        312,
        314,
        317,
        318,
        319,
        320,
        322,
        323,
        324,
        325,
        326,
        332,
        333,
        334,
        336,
        337,
        338,
        339,
        341,
        342,
        343,
        344,
        345,
        346,
        347,
        348,
        349,
        350,
        351,
        352,
        353,
        354,
        355,
        356,
        357,
        358,
        359,
        360,
        361,
        362,
        363,
        364,
        365,
        366,
        367,
        368,
        369,
        370,
        371,
        372,
        373,
        374,
        375,
        376,
        377,
        378,
        379,
        380,
        381,
        382,
        383,
        384,
        385,
        386,
        387,
        388,
        389,
        390,
        391,
        392,
        393,
        394,
        395,
        396,
        397,
        398,
        399,
        400,
        401,
        402,
        403,
        404,
        405,
        406,
        407,
        408,
        409,
        410,
        411,
        412,
        413,
        415,
        416,
        417,
    }
)


def face_id_for_name(name: str) -> int | None:
    """白名单表情名 → QQ face id；不在白名单返回 None（调用方丢标记）。"""
    return _FACE_NAME_TO_ID.get(name.strip())


def parse_face_spec(value: str) -> int | None:
    """发脸标记的值 → face id；客户端不认识的一律 None（发出去只会被
    静默丢弃，制造"我说发了你没看到"的困惑）。

    两种形态：白名单名字反查；或 ``#编号`` 直发原始 id——仅当客户端
    认识该 id 时放行（读侧占位 [表情#编号] 自带 id，群里见过的、客户端
    支持的表情都能原样回发）。越界/非法/客户端未知返回 None。"""
    value = value.strip()
    if value.startswith("#"):
        try:
            face_id = int(value[1:])
        except ValueError:
            return None
        candidate = face_id if 0 <= face_id <= FACE_ID_MAX else None
    else:
        candidate = face_id_for_name(value)
    if candidate is None or candidate not in CLIENT_KNOWN_FACE_IDS:
        return None
    return candidate


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


# 出站占位符保险闸（S12-CHATFIX-01）：这些标记只属于记忆文本/发送指令格式，
# 原样出现在发送文本里都是穿帮——2026-10-26 线上实测 plusone 把字面「[图片]」
# 四个字符当消息发进了群。发送端逐段剥除，剥后为空的段整段丢弃。
_ATTACHMENT_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|表情(?:[:：][^\[\]]*|#\d+)?|表情包[:：][^\[\]]*"
    r"|发图[:：][^\[\]]*|发脸[:：][^\[\]]*)\]"
)


def strip_attachment_placeholders(text: str) -> str:
    """剥除记忆占位与发送指令标记，返回 strip 后的文本（可能为空串）。"""
    return _ATTACHMENT_PLACEHOLDER_RE.sub("", text).strip()


def is_attachment_only_text(text: str) -> bool:
    """纯图/表情消息的记忆文本视角：剥掉占位标记后没有剩余文字。

    plusone 复读撞上纯图时据此转「重发原图」路径（S12-CHATFIX-01）。"""
    return not strip_attachment_placeholders(text)


def read_image_as_data_url(path: str) -> str | None:
    """本地图片文件 → base64 data URL（多模态请求可直接携带）；文件缺失/
    超限返回 None。落库图回看（S12-IMGSTORE-01）的公开入口。"""
    return _read_local_image(Path(path))


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


_BARE_FACE_RE = re.compile(r"\[([^\[\]]{1,12})\]")


def _promote_bare_faces(reply: str) -> str:
    """把裸的文字表情 [偷笑] 升级为 [发脸:偷笑] 标记（S11-VISION-01 线上
    实测：模型习惯写 GLM 风格的 [名字] 文字表情，全格式标记反而少用）。
    只升级白名单内的名字；[图片]/[发图:x]/[发脸:x] 与名单外的 [笑哭] 等
    一律原样保留。"""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _FACE_NAME_TO_ID:
            return f"[发脸:{name}]"
        return match.group(0)

    return _BARE_FACE_RE.sub(_sub, reply)


def parse_reply_parts(
    reply: str, *, max_stickers: int = MAX_REPLY_STICKERS, max_faces: int = MAX_REPLY_FACES
) -> ParsedReply:
    """解析回复文本里的 [发图:ID]/[发脸:名字] 标记（支持全角冒号）。

    裸的 [偷笑] 文字表情先升级为发脸标记再解析。标记之间的文本成为 text
    段（保序）；未知/超额标记整段丢弃并计入 ``dropped``。没有任何标记时
    parts 就是原回复本身，调用方走纯文本路径。"""
    parts: list[ReplyPart] = []
    stickers = faces = dropped = 0
    cursor = 0
    for match in _REPLY_MARKER_RE.finditer(_promote_bare_faces(reply)):
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
