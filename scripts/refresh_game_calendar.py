"""Refresh the game calendar from Bilibili official dynamics (S9-GAMECAL-SYNC).

Local, manually triggered tool — the user decides when to run it; nothing here
is scheduled and the server deployment never calls it. Fetches official-account
dynamics, ranks tall-image posts by caption (seed keywords + a learned store of
captions that previously produced applied events), extracts event schedules
with the DeepSeek-compatible vision endpoint, and merges them into
``data/game_calendar.json``.

By default the run only writes a proposal file (``--proposed-path``); pass
``--apply`` to atomically update the real calendar after passing the schema
validator, and record the captions of posts whose events were added into the
keyword store (speeding up future runs).

Exit codes: 0 ok (including "nothing new"), 1 configuration or validation
problem, 2 unexpected exception.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.config import BotSettings, get_settings  # noqa: E402
from qq_bot.services import bili_dynamic, vision_client  # noqa: E402
from qq_bot.services.calendar_sync import (  # noqa: E402
    build_extraction_prompt,
    event_key,
    load_keyword_store,
    merge_calendar_raw,
    run_sync,
    save_keyword_store,
    write_json_atomic,
)
from qq_bot.services.game_calendar import parse_game_calendar  # noqa: E402

ACCOUNTS = {
    "原神": "401742377",
    "星穹铁道": "1340190821",
    "明日方舟": "161775300",
    "终末地": "1265652806",
}
DEFAULT_CALENDAR_PATH = ROOT / "data" / "game_calendar.json"
DEFAULT_PROPOSED_PATH = ROOT / "data" / "game_calendar.proposed.json"
DEFAULT_STATE_PATH = ROOT / "data" / "calendar_sync_keywords.json"
DEFAULT_WORK_DIR = ROOT / "data" / "calendar_sync_images"
MAX_SLICE_HEIGHT = 2400


def _load_calendar_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "events": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"calendar file {path} is not a JSON object")
    return raw


async def _fetch_json(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


async def _download_image(client: httpx.AsyncClient, url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    response = await client.get(
        url,
        headers={"User-Agent": bili_dynamic.BILI_UA, "Referer": "https://www.bilibili.com/"},
    )
    response.raise_for_status()
    dest.write_bytes(response.content)
    return dest


def _slice_tall_image(path: Path) -> list[Path]:
    image = Image.open(path)
    width, height = image.size
    if height <= MAX_SLICE_HEIGHT:
        return [path]
    slices: list[Path] = []
    step = MAX_SLICE_HEIGHT
    overlap = 60
    for index in range((height + step - 1) // step):
        box = (0, index * step, width, min((index + 1) * step + overlap, height))
        out = path.with_name(f"{path.stem}_s{index}{path.suffix}")
        image.crop(box).save(out)
        slices.append(out)
    return slices


def _build_extraction_port(client: httpx.AsyncClient, work_dir: Path, settings: BotSettings):
    """Download + slice each candidate image, one vision call per slice, events merged."""

    async def extract(game: str, image_urls: list[str], post_id: str) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for index, url in enumerate(image_urls):
            ext = ".png" if ".png" in url else ".jpg"
            raw_path = await _download_image(
                client, url, work_dir / f"{game}_{post_id}_{index}{ext}"
            )
            for slice_path in _slice_tall_image(raw_path):
                payload = await vision_client.request_vision_json(
                    settings, build_extraction_prompt(game, date.today()), [slice_path]
                )
                events.extend(payload.get("events") or [])
        return {"events": events}

    return extract


def _format_event(event: dict[str, Any]) -> str:
    end = f" ~ {event['end']}" if event.get("end") else ""
    return f"[{event['game']}] {event['kind']} 「{event['title']}」 {event['start']}{end}"


def _print_report(report, *, proposed_path: Path, calendar_path: Path, applied: bool) -> None:
    print(
        f"扫描动态 {report.scanned_posts} 条,配文 {report.captioned_posts} 条,"
        f"候选图 {len(report.candidates)} 张"
    )
    for candidate in report.candidates:
        print(
            f"  候选 [{candidate.game}] score={candidate.score:.2f} h={candidate.image_height} "
            f"post={candidate.post_id} 配文={candidate.caption[:40]!r}"
        )
    print(
        f"提取事件 {len(report.events)} 条;合并新增 {len(report.added)} 条,冲突 {len(report.conflicts)} 条"
    )
    for reason in report.skipped:
        print(f"  跳过: {reason}")
    for conflict in report.conflicts:
        print(f"  冲突(保留已有): {conflict}")
    for note in report.notes:
        print(f"  备注: {note}")
    for event in report.added:
        print(f"  新增 {_format_event(event)}")
    target = calendar_path if applied else proposed_path
    mode = "已写入" if applied else "提案(未写入正式日历,加 --apply 生效)"
    print(f"{mode}: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", default=",".join(ACCOUNTS), help="逗号分隔的游戏名,默认全部")
    parser.add_argument("--pages", type=int, default=4, help="每个账号最多翻多少页动态")
    parser.add_argument("--cutoff-days", type=int, default=45, help="只回看最近 N 天")
    parser.add_argument("--min-height", type=int, default=2000, help="候选图最小高度")
    parser.add_argument("--top", type=int, default=6, help="每个游戏最多取多少张候选图")
    parser.add_argument("--delay-seconds", type=float, default=1.0, help="配文请求间隔(风控礼貌)")
    parser.add_argument("--apply", action="store_true", help="写入正式日历并沉淀配文;默认只出提案")
    parser.add_argument("--calendar-path", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--proposed-path", type=Path, default=DEFAULT_PROPOSED_PATH)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    args = parser.parse_args(argv)

    games = [g.strip() for g in args.games.split(",") if g.strip()]
    unknown = [g for g in games if g not in ACCOUNTS]
    if unknown:
        print(f"未知游戏: {unknown};支持: {list(ACCOUNTS)}")
        return 1

    settings = get_settings()
    if not settings.ai_vision_model.strip():
        print("AI_VISION_MODEL 未配置:先在 .env 里填 DeepSeek 视觉模型名")
        return 1

    store = load_keyword_store(args.state_path)
    existing = _load_calendar_raw(args.calendar_path)
    cutoff = date.today() - timedelta(days=args.cutoff_days)

    async def _run() -> Any:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:

            async def fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
                return await _fetch_json(client, url, headers=headers)

            cookie = await bili_dynamic.bootstrap_cookie(fetch_json)
            wbi_key = await bili_dynamic.get_wbi_keys(fetch_json)

            async def fetch_posts(game: str, today: date):
                return await bili_dynamic.fetch_posts(
                    fetch_json,
                    ACCOUNTS[game],
                    wbi_key=wbi_key,
                    cookie=cookie,
                    pages=args.pages,
                    cutoff=cutoff,
                )

            caption_cache: dict[str, str] = {}

            async def fetch_caption(dynamic_id: str) -> str:
                if dynamic_id not in caption_cache:
                    caption_cache[dynamic_id] = await bili_dynamic.fetch_caption(
                        fetch_json, dynamic_id, wbi_key=wbi_key, cookie=cookie
                    )
                    if args.delay_seconds > 0:
                        await asyncio.sleep(args.delay_seconds)
                return caption_cache[dynamic_id]

            extract = _build_extraction_port(client, args.work_dir, settings)
            return await run_sync(
                games=games,
                fetch_posts=fetch_posts,
                fetch_caption=fetch_caption,
                extract=extract,
                store=store,
                existing_calendar=existing,
                today=date.today(),
                min_height=args.min_height,
                top_n=args.top,
                caption_delay_seconds=0,
            )

    try:
        report = asyncio.run(_run())
    except (bili_dynamic.BiliDynamicError, vision_client.VisionError) as exc:
        print(f"同步失败: {exc}")
        return 1

    outcome = merge_calendar_raw(existing, report.events)
    if args.apply and report.added:
        try:
            parse_game_calendar(outcome.merged)
        except Exception as exc:  # noqa: BLE001 - 带病日历绝不落盘,报给人工
            print(f"合并结果未通过日历校验器,拒绝写入: {exc}")
            write_json_atomic(args.proposed_path, outcome.merged)
            print(f"合并提案已写到 {args.proposed_path} 供人工检查")
            return 1
        captions_by_key = {
            event_key(event): str(event.get("_source_caption") or "") for event in report.events
        }
        for event in report.added:
            caption = captions_by_key.get(event_key(event), "")
            if caption:
                store = store.with_caption(caption)
        save_keyword_store(args.state_path, store)
        write_json_atomic(args.calendar_path, outcome.merged)
    write_json_atomic(args.proposed_path, outcome.merged)
    _print_report(
        report,
        proposed_path=args.proposed_path,
        calendar_path=args.calendar_path,
        applied=args.apply,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
