"""Bilibili space-dynamics client for the game-calendar sync tool (S9-GAMECAL-SYNC).

Anonymous access only: Wbi-signed feed API plus the finger/spi bootstrap that
mints a throwaway buvid cookie. The feed response strips caption text for
anonymous callers, so captions are fetched per post from the detail endpoint
(which does return them) — and only for posts that pass the tall-image
pre-filter, to keep request volume low. All IO goes through the injected
``fetch_json`` callable so tests never touch the network.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

_MIKIN_TABLE = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)
_EMPTY_FEED_RETRIES = 2
_EMPTY_FEED_BACKOFF_SECONDS = 3.0
_PAGE_DELAY_SECONDS = 1.0

FetchJson = Callable[..., Awaitable[dict[str, Any]]]


class BiliDynamicError(RuntimeError):
    """Bilibili dynamics API is unavailable or answered with an error code."""


@dataclass(frozen=True)
class DynamicImage:
    url: str
    width: int
    height: int

    @property
    def is_gif(self) -> bool:
        return ".gif" in self.url


@dataclass(frozen=True)
class DynamicPost:
    id_str: str
    pub_date: date
    images: tuple[DynamicImage, ...]

    def tall_images(self, min_height: int) -> tuple[DynamicImage, ...]:
        return tuple(img for img in self.images if img.height >= min_height and not img.is_gif)


@dataclass(frozen=True)
class CaptionedPost:
    post: DynamicPost
    caption: str


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIKIN_TABLE)[:32]


def sign_feed_params(params: dict[str, str], key: str, *, wts: int) -> dict[str, str]:
    signed = {k: "".join(ch for ch in v if ch not in "!'()*") for k, v in params.items()}
    signed["wts"] = str(wts)
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return signed


def _require_ok(payload: dict[str, Any], what: str) -> dict[str, Any]:
    if payload.get("code") != 0:
        raise BiliDynamicError(
            f"{what} failed: code={payload.get('code')} {payload.get('message')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BiliDynamicError(f"{what} returned no data object")
    return data


def parse_pub_date(item: dict[str, Any]) -> date:
    module_author = (item.get("modules") or {}).get("module_author") or {}
    pub_ts = module_author.get("pub_ts")
    if not pub_ts:
        return date.min
    return datetime.fromtimestamp(int(pub_ts)).date()


def _images_from_item(item: dict[str, Any]) -> tuple[DynamicImage, ...]:
    module_dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
    major = module_dynamic.get("major") or {}
    pics: list[dict[str, Any]] = []
    if major.get("type") in ("MAJOR_TYPE_OPUS", "MAJOR_TYPE_DRAW"):
        opus = major.get("opus") or {}
        draw = major.get("draw") or {}
        pics = list(opus.get("pics") or []) + list(draw.get("items") or [])
    images: list[DynamicImage] = []
    for pic in pics:
        url = pic.get("url") or pic.get("src") or ""
        if not url:
            continue
        images.append(
            DynamicImage(
                url=url, width=int(pic.get("width") or 0), height=int(pic.get("height") or 0)
            )
        )
    return tuple(images)


def parse_feed_items(payload: dict[str, Any]) -> list[DynamicPost]:
    data = _require_ok(payload, "feed")
    posts: list[DynamicPost] = []
    for item in data.get("items") or []:
        images = _images_from_item(item)
        if not images:
            continue
        posts.append(
            DynamicPost(
                id_str=str(item.get("id_str") or ""),
                pub_date=parse_pub_date(item),
                images=images,
            )
        )
    return posts


def _rich_text_to_str(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "RICH_TEXT_NODE_TYPE_RICH":
            parts.append(str((node.get("rich_text_node") or {}).get("text") or ""))
        else:
            parts.append(str((node.get("word") or {}).get("words") or node.get("text") or ""))
    return "".join(parts)


def _desc_text(module_dynamic: dict[str, Any]) -> str:
    desc = module_dynamic.get("desc")
    if isinstance(desc, str):
        return desc
    if isinstance(desc, dict):
        return _rich_text_to_str(desc.get("nodes"))
    return ""


def parse_caption(payload: dict[str, Any]) -> str:
    data = _require_ok(payload, "detail")
    item = data.get("item")
    if item is None and isinstance(data.get("items"), list):
        item = (data.get("items") or [{}])[0]
    if not isinstance(item, dict):
        return ""
    module_dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
    text = _desc_text(module_dynamic)
    if text:
        return text
    opus = (module_dynamic.get("major") or {}).get("opus") or {}
    summary = opus.get("summary")
    if isinstance(summary, str):
        return summary
    if isinstance(summary, dict):
        return _rich_text_to_str(summary.get("rich_text_nodes"))
    return ""


async def bootstrap_cookie(fetch_json: FetchJson) -> str:
    """Mint an anonymous buvid cookie; feed/detail endpoints reject plain UA calls."""
    try:
        payload = await fetch_json(SPI_URL)
        data = payload.get("data") or {}
        return f"buvid3={data.get('b_3', '')}; buvid4={data.get('b_4', '')}"
    except BiliDynamicError:
        raise
    except Exception as exc:  # noqa: BLE001 - cookie is best-effort, feed still signs
        raise BiliDynamicError(f"finger/spi bootstrap failed: {exc}") from exc


async def get_wbi_keys(fetch_json: FetchJson) -> str:
    payload = await fetch_json(NAV_URL)
    data = _require_ok(payload, "nav")
    wbi = data.get("wbi_img") or {}
    img_url, sub_url = wbi.get("img_url") or "", wbi.get("sub_url") or ""
    if not img_url or not sub_url:
        raise BiliDynamicError("nav response missing wbi_img urls")
    return mixin_key(
        img_url.rsplit("/", 1)[-1].split(".")[0], sub_url.rsplit("/", 1)[-1].split(".")[0]
    )


def _feed_headers(cookie: str) -> dict[str, str]:
    return {
        "User-Agent": BILI_UA,
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
    }


async def fetch_posts(
    fetch_json: FetchJson,
    uid: str,
    *,
    wbi_key: str,
    cookie: str,
    pages: int,
    cutoff: date,
) -> list[DynamicPost]:
    """Walk a space's dynamics back to ``cutoff``; empty pages retry (soft risk control)."""
    posts: list[DynamicPost] = []
    offset = ""
    headers = _feed_headers(cookie)
    for _ in range(max(1, pages)):
        for attempt in range(_EMPTY_FEED_RETRIES + 1):
            params = sign_feed_params(
                {"host_mid": uid, "platform": "web", "features": "itemOpusStyle"},
                wbi_key,
                wts=int(datetime.now().timestamp()),
            )
            if offset:
                params["offset"] = offset
            payload = await fetch_json(f"{FEED_URL}?{urlencode(params)}", headers=headers)
            data = _require_ok(payload, "feed")
            items = data.get("items")
            if items:
                break
            if attempt >= _EMPTY_FEED_RETRIES:
                return posts
            await asyncio.sleep(_EMPTY_FEED_BACKOFF_SECONDS * (attempt + 1))
        posts.extend(parse_feed_items({"code": 0, "data": data}))
        oldest = min((post.pub_date for post in posts), default=date.min)
        offset = str(data.get("offset") or "")
        if not offset or not data.get("has_more") or oldest < cutoff:
            break
        await asyncio.sleep(_PAGE_DELAY_SECONDS)
    return posts


async def fetch_caption(
    fetch_json: FetchJson, dynamic_id: str, *, wbi_key: str, cookie: str
) -> str:
    params = sign_feed_params(
        {"id": dynamic_id, "platform": "web", "features": "itemOpusStyle"},
        wbi_key,
        wts=int(datetime.now().timestamp()),
    )
    payload = await fetch_json(f"{DETAIL_URL}?{urlencode(params)}", headers=_feed_headers(cookie))
    return parse_caption(payload)


async def fetch_account_posts(
    fetch_json: FetchJson,
    uid: str,
    *,
    pages: int,
    cutoff: date,
) -> list[DynamicPost]:
    """Bootstrap wbi + cookie then walk the feed; the natural entry point for callers."""
    cookie = await bootstrap_cookie(fetch_json)
    wbi_key = await get_wbi_keys(fetch_json)
    return await fetch_posts(
        fetch_json, uid, wbi_key=wbi_key, cookie=cookie, pages=pages, cutoff=cutoff
    )


def filter_tall(posts: Sequence[DynamicPost], min_height: int) -> list[DynamicPost]:
    return [post for post in posts if post.tall_images(min_height)]
