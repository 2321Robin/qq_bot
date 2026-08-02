"""Refresh ROCO pet data end to end: fetch, validate, gates, diff, publish.

Exit codes: 0 success, 1 gate failure / fetch error / verify inconsistency,
2 unexpected exception.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.datapipeline.publish import (  # noqa: E402
    BWIKI_INDEX_URL,
    RefreshArgs,
    run_refresh,
)

DEFAULT_DETAILS_DIR = ROOT / "data" / "roco_pet_details"
DEFAULT_MANIFEST_DIR = ROOT / "data" / "manifests"
DEFAULT_REPORTS_DIR = ROOT / "data" / "reports"
DEFAULT_QUARANTINE_DIR = ROOT / "data" / "quarantine"
DEFAULT_STAGING_DIR = ROOT / "data" / ".staging"
DEFAULT_INDEX_PATH = ROOT / "data" / "roco_search.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details-dir", type=Path, default=DEFAULT_DETAILS_DIR)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--quarantine-dir", type=Path, default=DEFAULT_QUARANTINE_DIR)
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--index-path", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--index-url", default=BWIKI_INDEX_URL, help="精灵索引页 URL")
    parser.add_argument("--offline", action="store_true", help="不抓取网络，全部视为未变化")
    parser.add_argument("--dry-run", action="store_true", help="不写正式目录/manifest/索引")
    parser.add_argument("--verify-only", action="store_true", help="只校验 manifest 与磁盘一致性")
    parser.add_argument("--force", action="store_true", help="忽略 304/内容哈希，全部重抓")
    parser.add_argument("--use-raw-pages", action="store_true", help="抓取 ?action=raw 模板页")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--no-cards", action="store_true", help="跳过图卡增量生成")
    parser.add_argument("--no-index", action="store_true", help="跳过搜索索引重建")
    parser.add_argument(
        "--prune-removed",
        action="store_true",
        help="删除索引页消失的本地文件",
    )
    parser.add_argument(
        "--allow-quarantine",
        action="store_true",
        help="放行非空 quarantine 的发布阻断（放行事实写入差异报告）",
    )
    parser.add_argument("--min-parser-version", type=int, default=0)
    args = parser.parse_args(argv)

    refresh_args = RefreshArgs(
        details_dir=args.details_dir,
        manifest_dir=args.manifest_dir,
        reports_dir=args.reports_dir,
        quarantine_dir=args.quarantine_dir,
        staging_dir=args.staging_dir,
        index_path=args.index_path,
        offline=args.offline,
        dry_run=args.dry_run,
        verify_only=args.verify_only,
        force=args.force,
        use_raw_pages=args.use_raw_pages,
        retries=args.retries,
        delay_seconds=args.delay_seconds,
        no_normalize=args.no_normalize,
        no_cards=args.no_cards,
        no_index=args.no_index,
        prune_removed=args.prune_removed,
        allow_quarantine=args.allow_quarantine,
        min_parser_version=args.min_parser_version,
        index_url=args.index_url,
    )
    return run_refresh(refresh_args)


if __name__ == "__main__":
    raise SystemExit(main())
