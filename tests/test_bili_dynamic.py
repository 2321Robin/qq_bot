"""Bilibili dynamics client tests (S9-GAMECAL-SYNC): signing, parsing, pagination."""

from __future__ import annotations

from datetime import date

import pytest

from qq_bot.services import bili_dynamic


def _opus_item(post_id: str, pub_ts: int, pics: list[dict], summary_text: str = "") -> dict:
    item = {
        "id_str": post_id,
        "modules": {
            "module_author": {"pub_ts": pub_ts},
            "module_dynamic": {
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "pics": pics,
                        "summary": {
                            "rich_text_nodes": [
                                {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": summary_text}
                            ]
                        },
                    },
                }
            },
        },
    }
    return item


class FakeFetcher:
    """Route-detail fake: canned payloads keyed by URL prefix, records calls."""

    def __init__(self, routes: dict[str, list[dict]]):
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, url: str, headers: dict | None = None) -> dict:
        self.calls.append(url)
        for prefix, payloads in self.routes.items():
            if url.startswith(prefix):
                return payloads.pop(0) if len(payloads) > 1 else payloads[0]
        raise AssertionError(f"unexpected url: {url}")


IMG_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f9a"
SUB_KEY = "0f9e8d7c6b5a4938271504a3b2c1d0e9"


def test_mixin_key_is_deterministic_and_truncated():
    key = bili_dynamic.mixin_key(IMG_KEY, SUB_KEY)
    assert key == bili_dynamic.mixin_key(IMG_KEY, SUB_KEY)
    assert len(key) == 32


def test_sign_feed_params_is_order_independent_and_adds_wts_rid():
    key = bili_dynamic.mixin_key(IMG_KEY, SUB_KEY)
    first = bili_dynamic.sign_feed_params({"a": "1", "b": "2"}, key, wts=1700000000)
    second = bili_dynamic.sign_feed_params({"b": "2", "a": "1"}, key, wts=1700000000)
    assert first == second
    assert first["wts"] == "1700000000"
    assert len(first["w_rid"]) == 32
    # Wbi 规范:wts 与最终串里都要剔除 !'()* 字符
    weird = bili_dynamic.sign_feed_params({"x": "a!'()*b"}, key, wts=1)
    assert weird["x"] == "ab"


def test_parse_feed_items_reads_opus_and_draw_pics():
    payload = {
        "code": 0,
        "data": {
            "items": [
                _opus_item(
                    "1",
                    1790000000,
                    [{"url": "http://i0.hdslb.com/a.jpg", "width": 1080, "height": 4000}],
                ),
                {
                    "id_str": "2",
                    "modules": {
                        "module_author": {"pub_ts": 1790000000},
                        "module_dynamic": {
                            "major": {
                                "type": "MAJOR_TYPE_DRAW",
                                "draw": {
                                    "items": [
                                        {
                                            "src": "http://i0.hdslb.com/b.png",
                                            "width": 900,
                                            "height": 3000,
                                        }
                                    ]
                                },
                            }
                        },
                    },
                },
                _opus_item("3", 1790000000, []),
            ]
        },
    }
    posts = bili_dynamic.parse_feed_items(payload)
    assert [post.id_str for post in posts] == ["1", "2"]
    assert posts[0].pub_date == date.fromtimestamp(1790000000)
    assert posts[0].images[0].height == 4000


def test_tall_images_filters_height_and_gif():
    post = bili_dynamic.DynamicPost(
        id_str="1",
        pub_date=date(2026, 9, 1),
        images=(
            bili_dynamic.DynamicImage("http://x/a.gif", 750, 3000),
            bili_dynamic.DynamicImage("http://x/b.jpg", 1080, 4000),
            bili_dynamic.DynamicImage("http://x/c.jpg", 1080, 900),
        ),
    )
    assert [img.url for img in post.tall_images(2000)] == ["http://x/b.jpg"]


def test_parse_caption_prefers_desc_then_opus_summary():
    desc_payload = {
        "code": 0,
        "data": {"item": {"modules": {"module_dynamic": {"desc": "版本速览一图流"}}}},
    }
    assert bili_dynamic.parse_caption(desc_payload) == "版本速览一图流"
    summary_payload = {
        "code": 0,
        "data": {
            "item": {
                "modules": {
                    "module_dynamic": {
                        "desc": None,
                        "major": {
                            "type": "MAJOR_TYPE_OPUS",
                            "opus": {
                                "summary": {
                                    "rich_text_nodes": [
                                        {
                                            "type": "RICH_TEXT_NODE_TYPE_TOPIC",
                                            "orig_text": "#原神#",
                                            "text": "#原神#",
                                        },
                                        {
                                            "type": "RICH_TEXT_NODE_TYPE_TEXT",
                                            "text": "本期活动简述",
                                        },
                                    ]
                                }
                            },
                        },
                    }
                }
            }
        },
    }
    assert bili_dynamic.parse_caption(summary_payload) == "#原神#本期活动简述"


async def test_fetch_posts_paginates_until_no_offset():
    nav_payload = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "http://x/img/a1b2c3d4e5f60718293a4b5c6d7e8f9a.jpg",
                "sub_url": "http://x/img/0f9e8d7c6b5a4938271504a3b2c1d0e9.jpg",
            }
        },
    }
    spi_payload = {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}
    fetcher = FakeFetcher(
        {
            bili_dynamic.SPI_URL: [spi_payload],
            bili_dynamic.NAV_URL: [nav_payload],
            bili_dynamic.FEED_URL: [
                {
                    "code": 0,
                    "data": {
                        "items": [
                            _opus_item(
                                "1",
                                1790000000,
                                [
                                    {
                                        "url": "http://i0.hdslb.com/a.jpg",
                                        "width": 1080,
                                        "height": 4000,
                                    }
                                ],
                            )
                        ],
                        "offset": "next",
                        "has_more": True,
                    },
                },
                {"code": 0, "data": {"items": [], "offset": "", "has_more": False}},
            ],
        }
    )
    posts = await bili_dynamic.fetch_account_posts(fetcher, "123", pages=3, cutoff=date(2026, 8, 1))
    assert [post.id_str for post in posts] == ["1"]
    feed_calls = [url for url in fetcher.calls if url.startswith(bili_dynamic.FEED_URL)]
    # 第 1 页(无 offset)→ 第 2 页带 offset=next → 空页重试两次后返回
    assert len(feed_calls) == 4
    assert "w_rid=" in feed_calls[0]
    assert "offset" not in feed_calls[0]
    assert "offset=next" in feed_calls[1]


async def test_fetch_posts_raises_on_error_code():
    nav_payload = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "http://x/img/a1b2c3d4e5f60718293a4b5c6d7e8f9a.jpg",
                "sub_url": "http://x/img/0f9e8d7c6b5a4938271504a3b2c1d0e9.jpg",
            }
        },
    }
    fetcher = FakeFetcher(
        {
            bili_dynamic.SPI_URL: [{"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}],
            bili_dynamic.NAV_URL: [nav_payload],
            bili_dynamic.FEED_URL: [
                {"code": -352, "message": "-352"},
            ],
        }
    )
    with pytest.raises(bili_dynamic.BiliDynamicError):
        await bili_dynamic.fetch_account_posts(fetcher, "123", pages=1, cutoff=date(2026, 8, 1))


async def test_fetch_posts_retries_soft_empty_page(monkeypatch):
    # 观测到的软风控形态:code=0 但 items 为空;重试后恢复
    monkeypatch.setattr(bili_dynamic.asyncio, "sleep", _noop_sleep)
    nav_payload = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "http://x/img/a1b2c3d4e5f60718293a4b5c6d7e8f9a.jpg",
                "sub_url": "http://x/img/0f9e8d7c6b5a4938271504a3b2c1d0e9.jpg",
            }
        },
    }
    fetcher = FakeFetcher(
        {
            bili_dynamic.SPI_URL: [{"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}],
            bili_dynamic.NAV_URL: [nav_payload],
            bili_dynamic.FEED_URL: [
                {"code": 0, "data": {"items": [], "offset": "", "has_more": True}},
                {
                    "code": 0,
                    "data": {
                        "items": [
                            _opus_item(
                                "1",
                                1790000000,
                                [
                                    {
                                        "url": "http://i0.hdslb.com/a.jpg",
                                        "width": 1080,
                                        "height": 4000,
                                    }
                                ],
                            )
                        ],
                        "offset": "",
                        "has_more": False,
                    },
                },
            ],
        }
    )
    posts = await bili_dynamic.fetch_account_posts(fetcher, "123", pages=1, cutoff=date(2026, 8, 1))
    assert [post.id_str for post in posts] == ["1"]


async def _noop_sleep(_seconds: float) -> None:
    return None
