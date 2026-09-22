"""Game-calendar sync: Bilibili schedule images -> calendar events (S9-GAMECAL-SYNC).

Pipeline: fetch official-account dynamics -> keep tall-image posts -> score
them by caption (seed keywords + learned bigram overlap against captions that
previously produced applied events) -> download the top images -> vision
extraction -> normalize -> merge into ``data/game_calendar.json``.

The user triggers runs manually from a local checkout; nothing here is wired
into the scheduler, and every network dependency is injected as a callable so
tests stay offline. Merge semantics follow S6-GAME-01: existing events are
never silently overwritten (same ``(game, title, start)`` with a different end
is reported as a conflict), and expired events are simply left in place — the
rule engine already hides them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from qq_bot.services.bili_dynamic import CaptionedPost

SEED_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "版本速览",
    "活动一览",
    "活动简述",
    "内容速览",
    "维护通知",
    "版本更新公告",
    "活动预告",
    "活动时间",
    "开启时间",
    "限时活动",
    "签到",
    "赛季",
    "版本更新后",
    "祈愿预告",
    "前瞻",
)
SEED_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "角色展示",
    "立绘",
    "漫画",
    "联名",
    "周边",
    "套餐",
    "门店",
    "预售",
    "激励计划",
    "同人",
    "手办",
)
VALID_CAPTION_CAP = 200
BGRAM_JACCARD_WEIGHT = 4.0
KEYWORD_HIT_WEIGHT = 2.0


class CalendarSyncError(RuntimeError):
    """Calendar sync inputs or outputs are unusable."""


# ---- 配文关键词库:有效动态的配文会沉淀下来,让后续筛选更快 ----


@dataclass(frozen=True)
class KeywordStore:
    valid_captions: tuple[str, ...] = ()

    def with_caption(self, caption: str) -> "KeywordStore":
        text = caption.strip()
        if not text or text in self.valid_captions:
            return self
        captions = (text, *self.valid_captions)[:VALID_CAPTION_CAP]
        return replace(self, valid_captions=captions)


def load_keyword_store(path: Path) -> KeywordStore:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return KeywordStore()
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarSyncError(f"cannot read keyword store {path}: {exc}") from exc
    captions = raw.get("valid_captions") if isinstance(raw, dict) else None
    if not isinstance(captions, list) or not all(isinstance(c, str) for c in captions):
        raise CalendarSyncError(f"keyword store {path} is malformed")
    return KeywordStore(valid_captions=tuple(captions))


def save_keyword_store(path: Path, store: KeywordStore) -> None:
    write_json_atomic(path, {"valid_captions": list(store.valid_captions)})


def _bigrams(text: str) -> set[str]:
    stripped = "".join(text.split())
    return (
        {stripped[i : i + 2] for i in range(len(stripped) - 1)} if len(stripped) > 1 else {stripped}
    )


def caption_score(caption: str, store: KeywordStore) -> float:
    """Higher = more likely a schedule image; negatives push merch/comics down."""
    score = 0.0
    for keyword in SEED_POSITIVE_KEYWORDS:
        if keyword in caption:
            score += KEYWORD_HIT_WEIGHT
    for keyword in SEED_NEGATIVE_KEYWORDS:
        if keyword in caption:
            score -= KEYWORD_HIT_WEIGHT
    caption_grams = _bigrams(caption)
    for valid in store.valid_captions:
        valid_grams = _bigrams(valid)
        if caption_grams and valid_grams:
            jaccard = len(caption_grams & valid_grams) / len(caption_grams | valid_grams)
            score += jaccard * BGRAM_JACCARD_WEIGHT
    return score


# ---- 候选排序 ----


@dataclass(frozen=True)
class Candidate:
    game: str
    post_id: str
    caption: str
    image_url: str
    image_height: int
    score: float


def rank_candidates(
    captioned: Sequence[tuple[str, CaptionedPost]],
    store: KeywordStore,
    *,
    min_height: int,
    top_n: int,
) -> list[Candidate]:
    entries: list[Candidate] = []
    for game, item in captioned:
        for image in item.post.tall_images(min_height):
            entries.append(
                Candidate(
                    game=game,
                    post_id=item.post.id_str,
                    caption=item.caption,
                    image_url=image.url,
                    image_height=image.height,
                    score=caption_score(item.caption, store),
                )
            )
    entries.sort(key=lambda c: (c.score, c.image_height), reverse=True)
    ranked: list[Candidate] = []
    per_game: dict[str, int] = {}
    for entry in entries:
        if per_game.get(entry.game, 0) >= top_n:
            continue
        per_game[entry.game] = per_game.get(entry.game, 0) + 1
        ranked.append(entry)
    return ranked


# ---- 视觉提取:prompt 契约与输出规整 ----


def build_extraction_prompt(game: str, today: date) -> str:
    return (
        f"你是游戏活动日程提取助手。下面是B站账号「{game}」官方动态里的一张图片"
        "（可能是长图切片之一）。请只提取图中明确的**游戏内活动/版本日程信息**，"
        "忽略角色展示、周边联名、线下活动、玩家创作。\n"
        f"今天是 {today.isoformat()}：图中出现「版本更新后」「维护后」等相对日期时，"
        "以此锚点推算；推算不出就输出 null，禁止编造。\n"
        f"game 字段固定为「{game}」。\n"
        '输出 JSON：{"events": [{"game": str, "kind": "version"|"event", '
        '"title": str, "start": "YYYY-MM-DD"|null, "end": "YYYY-MM-DD"|null}]}\n'
        "规则：version=版本更新（无 end）；event=活动（必须有 end，没有就输出 null）；"
        '标题不确定输出 null；没有日程就输出 {"events": []}。'
    )


def normalize_extraction(
    raw: object, *, game: str, today: date
) -> tuple[list[dict[str, Any]], list[str]]:
    """LLM JSON -> calendar-schema event dicts; unusable entries come back with reasons."""
    if not isinstance(raw, dict):
        return [], ["extraction is not a JSON object"]
    events = raw.get("events")
    if not isinstance(events, list):
        return [], ["extraction has no events list"]
    normalized: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in events:
        if not isinstance(entry, dict):
            skipped.append("non-object event entry")
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            skipped.append("event without title")
            continue
        kind = entry.get("kind")
        if kind not in ("version", "event"):
            skipped.append(f"unknown kind for {title.strip()!r}")
            continue
        start = _parse_iso(entry.get("start"))
        if start is None:
            skipped.append(f"event without usable start: {title.strip()!r}")
            continue
        end = _parse_iso(entry.get("end"))
        if kind == "version":
            # 校验器不允许 version 带 end；带结束日的一律按活动理解
            if end is not None:
                kind = "event"
            elif start != today and start < today:
                skipped.append(f"stale version event: {title.strip()!r} ({start})")
                continue
        else:
            if end is None:
                # 校验器要求 event 必须有 end；缺失交给确认流程人工补齐而不是丢弃
                skipped.append(f"event without end needs manual fill: {title.strip()!r}")
                continue
            if end < start:
                skipped.append(f"end before start: {title.strip()!r}")
                continue
        normalized.append(
            {
                "game": game,
                "kind": kind,
                "title": title.strip(),
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
            }
        )
    return normalized, skipped


def _parse_iso(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


# ---- 合并与落盘 ----


@dataclass(frozen=True)
class MergeOutcome:
    merged: dict[str, Any]
    added: tuple[dict[str, Any], ...]
    conflicts: tuple[str, ...]

    @property
    def added_count(self) -> int:
        return len(self.added)


def event_key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (str(event.get("game")), str(event.get("title")), str(event.get("start")))


def _clean_event(event: dict[str, Any]) -> dict[str, Any]:
    """Drop internal provenance keys (``_source_*``) before anything is persisted."""
    return {k: v for k, v in event.items() if not k.startswith("_")}


def merge_calendar_raw(
    existing: dict[str, Any], candidates: Sequence[dict[str, Any]]
) -> MergeOutcome:
    events = [_clean_event(e) for e in existing.get("events", []) if isinstance(e, dict)]
    known = {event_key(e): e for e in events}
    added: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for candidate in candidates:
        candidate = _clean_event(candidate)
        key = event_key(candidate)
        current = known.get(key)
        if current is None:
            known[key] = candidate
            added.append(candidate)
        elif current.get("end") != candidate.get("end"):
            conflicts.append(
                f"{key[0]}「{key[1]}」{key[2]}: 已有 end={current.get('end')}, 提取 end={candidate.get('end')}"
            )
    merged = dict(existing)
    merged["schema_version"] = existing.get("schema_version", 1)
    merged["events"] = list(known.values())
    return MergeOutcome(merged=merged, added=tuple(added), conflicts=tuple(conflicts))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---- 编排 ----


class PostFetcherPort(Protocol):
    async def __call__(self, uid: str, cutoff: date) -> list[Any]: ...  # -> list[DynamicPost]


class CaptionFetcherPort(Protocol):
    async def __call__(self, dynamic_id: str) -> str: ...


class ExtractPort(Protocol):
    async def __call__(
        self, game: str, image_urls: Sequence[str], post_id: str
    ) -> dict[str, Any]: ...


@dataclass
class SyncReport:
    scanned_posts: int = 0
    captioned_posts: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    added: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[str, ...] = ()
    applied: bool = False
    notes: list[str] = field(default_factory=list)


async def run_sync(
    *,
    games: Sequence[str],
    fetch_posts: PostFetcherPort,
    fetch_caption: CaptionFetcherPort,
    extract: ExtractPort,
    store: KeywordStore,
    existing_calendar: dict[str, Any],
    today: date,
    min_height: int,
    top_n: int,
    caption_delay_seconds: float = 0.0,
    sleep: Any = None,
) -> SyncReport:
    """Fetch -> caption -> rank -> extract -> merge. Network enters only via the ports."""
    import asyncio

    pause = sleep or asyncio.sleep
    report = SyncReport()
    captioned: list[tuple[str, CaptionedPost]] = []
    for game in games:
        posts = await fetch_posts(game, today)
        report.scanned_posts += len(posts)
        tall = [p for p in posts if p.tall_images(min_height)]
        for post in tall:
            caption = await fetch_caption(post.id_str)
            report.captioned_posts += 1
            captioned.append((game, CaptionedPost(post=post, caption=caption)))
            if caption_delay_seconds > 0:
                await pause(caption_delay_seconds)

    report.candidates = rank_candidates(captioned, store, min_height=min_height, top_n=top_n)
    if not report.candidates:
        report.notes.append("没有候选动态：检查官方账号是否有新日程图或放宽 min-height/top-n")
        return report

    candidates: list[dict[str, Any]] = []
    for candidate in report.candidates:
        try:
            raw = await extract(candidate.game, [candidate.image_url], candidate.post_id)
        except Exception as exc:  # noqa: BLE001 - 单张失败不阻塞整轮
            report.skipped.append(f"{candidate.game} {candidate.post_id}: extraction failed: {exc}")
            continue
        events, skipped = normalize_extraction(raw, game=candidate.game, today=today)
        report.skipped.extend(skipped)
        for event in events:
            event["_source_post_id"] = candidate.post_id
            event["_source_caption"] = candidate.caption
        candidates.extend(events)
    report.events = candidates
    if not candidates:
        report.notes.append("候选图里没有提取到可入日历的事件")
        return report

    outcome = merge_calendar_raw(existing_calendar, candidates)
    report.added = outcome.added
    report.conflicts = outcome.conflicts
    return report
