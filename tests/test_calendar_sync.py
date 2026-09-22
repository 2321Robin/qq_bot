"""Calendar sync pipeline tests (S9-GAMECAL-SYNC): scoring, ranking, merge, orchestration."""

from __future__ import annotations

import json
from datetime import date

from qq_bot.services import calendar_sync
from qq_bot.services.bili_dynamic import DynamicImage, DynamicPost
from qq_bot.services.calendar_sync import (
    CaptionedPost,
    KeywordStore,
    SyncReport,
    caption_score,
    merge_calendar_raw,
    normalize_extraction,
    rank_candidates,
    run_sync,
    write_json_atomic,
)

TODAY = date(2026, 9, 23)


def _post(post_id: str, height: int = 4000) -> DynamicPost:
    return DynamicPost(
        id_str=post_id,
        pub_date=TODAY,
        images=(DynamicImage(f"http://i0.hdslb.com/{post_id}.jpg", 1080, height),),
    )


# ---- 配文关键词库与打分 ----


def test_keyword_store_dedupes_and_caps():
    store = KeywordStore()
    store = store.with_caption("版本速览")
    store = store.with_caption("版本速览")
    assert store.valid_captions == ("版本速览",)
    capped = KeywordStore(
        valid_captions=tuple(f"c{i}" for i in range(calendar_sync.VALID_CAPTION_CAP))
    )
    assert capped.with_caption("new").valid_captions[0] == "new"
    assert len(capped.with_caption("new").valid_captions) == calendar_sync.VALID_CAPTION_CAP


def test_caption_score_prefers_schedule_over_merch():
    schedule = "原神7.1版本活动简述,活动时间9/24-10/12,限时活动一览"
    merch = "奈雪联名主题门店套餐介绍,周边预售"
    store = KeywordStore()
    assert caption_score(schedule, store) > 0
    assert caption_score(merch, store) < caption_score(schedule, store)


def test_caption_score_learns_from_valid_captions():
    store = KeywordStore().with_caption("星穹铁道4.5版本速览 活动时间一览")
    similar = "崩坏星穹铁道4.6版本速览 各活动时间汇总"
    unrelated = "角色生日贺图"
    assert caption_score(similar, store) > caption_score(unrelated, store)


def test_rank_candidates_sorts_and_caps_per_game():
    store = KeywordStore(valid_captions=("版本速览 活动一览",))
    captioned = [
        ("原神", CaptionedPost(_post("genshin-low", 6000), "角色展示立绘")),
        ("原神", CaptionedPost(_post("genshin-hi", 3000), "7.1版本速览 活动简述")),
        ("星穹铁道", CaptionedPost(_post("hsr-hi", 5000), "4.5版本速览 活动时间")),
        ("终末地", CaptionedPost(_post("endfield-tiny", 800), "赛季一览")),
    ]
    ranked = rank_candidates(captioned, store, min_height=2000, top_n=1)
    assert sorted(c.post_id for c in ranked) == ["genshin-hi", "hsr-hi"]
    assert ranked[0].score >= ranked[1].score


def test_keyword_store_roundtrip(tmp_path):
    path = tmp_path / "keywords.json"
    save = calendar_sync.save_keyword_store
    store = KeywordStore().with_caption("活动一览")
    save(path, store)
    assert calendar_sync.load_keyword_store(path) == store


def test_load_keyword_store_tolerates_missing_file(tmp_path):
    assert calendar_sync.load_keyword_store(tmp_path / "absent.json") == KeywordStore()


# ---- 提取规整 ----


def test_normalize_extraction_keeps_valid_events():
    raw = {
        "events": [
            {
                "game": "原神",
                "kind": "event",
                "title": "逐月节",
                "start": "2026-09-24",
                "end": "2026-10-12",
            },
            {"game": "原神", "kind": "version", "title": "7.1", "start": "2026-09-23", "end": None},
        ]
    }
    events, skipped = normalize_extraction(raw, game="原神", today=TODAY)
    assert not skipped
    assert [e["title"] for e in events] == ["逐月节", "7.1"]
    assert events[1]["end"] is None


def test_normalize_extraction_reclassifies_version_with_end():
    raw = {
        "events": [
            {"kind": "version", "title": "活动A", "start": "2026-09-24", "end": "2026-10-01"}
        ]
    }
    events, _ = normalize_extraction(raw, game="星穹铁道", today=TODAY)
    assert events[0]["kind"] == "event"
    assert events[0]["game"] == "星穹铁道"  # game 以账号为准


def test_normalize_extraction_skips_unusable_entries():
    raw = {
        "events": [
            {"kind": "event", "title": None, "start": "2026-09-24", "end": "2026-10-01"},
            {"kind": "event", "title": "无开始", "start": None, "end": "2026-10-01"},
            {"kind": "event", "title": "无结束", "start": "2026-09-24", "end": None},
            {"kind": "event", "title": "倒挂", "start": "2026-10-02", "end": "2026-09-24"},
            {"kind": "未知", "title": "怪类型", "start": "2026-09-24", "end": "2026-09-30"},
            "not-a-dict",
        ]
    }
    events, skipped = normalize_extraction(raw, game="原神", today=TODAY)
    assert not events
    assert len(skipped) == 6


def test_normalize_extraction_drops_stale_versions():
    raw = {"events": [{"kind": "version", "title": "4.4版本", "start": "2026-07-01", "end": None}]}
    events, skipped = normalize_extraction(raw, game="星穹铁道", today=TODAY)
    assert not events and skipped


# ---- 合并与落盘 ----


def test_merge_calendar_raw_adds_and_reports_conflicts():
    existing = {
        "schema_version": 1,
        "events": [
            {
                "game": "原神",
                "kind": "event",
                "title": "逐月节",
                "start": "2026-09-24",
                "end": "2026-10-12",
            },
        ],
    }
    candidates = [
        {
            "game": "原神",
            "kind": "event",
            "title": "逐月节",
            "start": "2026-09-24",
            "end": "2026-10-15",
        },
        {
            "game": "终末地",
            "kind": "event",
            "title": "错视轮换I",
            "start": "2026-09-24",
            "end": "2026-10-01",
        },
    ]
    outcome = merge_calendar_raw(existing, candidates)
    assert outcome.added_count == 1
    assert len(outcome.conflicts) == 1
    assert outcome.merged["events"][0]["end"] == "2026-10-12"  # 绝不静默覆盖


def test_merge_calendar_raw_strips_provenance_keys():
    existing = {
        "schema_version": 1,
        "events": [
            {
                "game": "原神",
                "kind": "version",
                "title": "旧",
                "start": "2026-01-01",
                "_source_caption": "x",
            },
        ],
    }
    outcome = merge_calendar_raw(
        existing,
        [
            {
                "game": "原神",
                "kind": "event",
                "title": "新",
                "start": "2026-09-24",
                "end": "2026-10-01",
                "_source_post_id": "9",
            }
        ],
    )
    assert all(not str(k).startswith("_") for e in outcome.merged["events"] for k in e)


def test_write_json_atomic_roundtrip(tmp_path):
    path = tmp_path / "nested" / "calendar.json"
    write_json_atomic(path, {"schema_version": 1, "events": []})
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema_version": 1, "events": []}
    assert not path.with_suffix(".json.tmp").exists()


# ---- 编排 ----


class FakePorts:
    def __init__(
        self,
        posts_by_game: dict[str, list[DynamicPost]],
        captions: dict[str, str],
        raw_events: list[dict],
    ):
        self.posts_by_game = posts_by_game
        self.captions = captions
        self.raw_events = raw_events
        self.caption_calls: list[str] = []

    async def fetch_posts(self, game: str, today: date):
        return self.posts_by_game.get(game, [])

    async def fetch_caption(self, dynamic_id: str) -> str:
        self.caption_calls.append(dynamic_id)
        return self.captions[dynamic_id]

    async def extract(self, game: str, image_urls, post_id: str) -> dict:
        return {"events": self.raw_events}

    async def sleep(self, seconds: float) -> None:
        return None


async def test_run_sync_happy_path():
    ports = FakePorts(
        posts_by_game={
            "原神": [_post("genshin-1"), _post("genshin-short", 900)],
            "终末地": [_post("endfield-1", 4500)],
        },
        captions={"genshin-1": "7.1版本速览 活动简述", "endfield-1": "赛季轮换一览"},
        raw_events=[
            {"kind": "event", "title": "逐月节", "start": "2026-09-24", "end": "2026-10-12"},
            {"kind": "event", "title": "无结束", "start": "2026-09-24", "end": None},
        ],
    )
    report = await run_sync(
        games=["原神", "终末地"],
        fetch_posts=ports.fetch_posts,
        fetch_caption=ports.fetch_caption,
        extract=ports.extract,
        store=KeywordStore(),
        existing_calendar={"schema_version": 1, "events": []},
        today=TODAY,
        min_height=2000,
        top_n=6,
        caption_delay_seconds=5,
        sleep=ports.sleep,
    )
    assert report.scanned_posts == 3
    assert report.captioned_posts == 2  # 矮图动态不取配文
    # extract 端口对每个游戏返回同一批原始事件,normalize 会按账号重打 game 标
    assert len(report.events) == 2
    assert {e["game"] for e in report.events} == {"原神", "终末地"}
    assert len(report.added) == 2
    assert any("无结束" in reason for reason in report.skipped)
    assert report.conflicts == ()


async def test_run_sync_without_candidates_reports_note():
    ports = FakePorts(posts_by_game={}, captions={}, raw_events=[])
    report = SyncReport()
    report = await run_sync(
        games=["原神"],
        fetch_posts=ports.fetch_posts,
        fetch_caption=ports.fetch_caption,
        extract=ports.extract,
        store=KeywordStore(),
        existing_calendar={"schema_version": 1, "events": []},
        today=TODAY,
        min_height=2000,
        top_n=6,
    )
    assert report.notes and not report.events
