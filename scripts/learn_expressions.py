"""表达/黑话学习离线脚本（S10-HUMAN-P1）。

用法：
  python scripts/learn_expressions.py --group 12345 --days 7
  python scripts/learn_expressions.py --all --max-batches 3

与 /表达学习 命令共用 run_learning_round 与配额（scope=expression_learn）。
每个群输出一行 JSON（group_id + RoundSummary 各字段）。

退出码：0 成功（insufficient_material / quota_denied 属正常结局）；
1 表达学习未开启或参数错误；2 至少一个群内部出错（status=error）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from qq_bot.config import BotSettings, get_settings  # noqa: E402
from qq_bot.services.ai_client import request_completion  # noqa: E402
from qq_bot.services.chat_memory import ChatMemoryRepository  # noqa: E402
from qq_bot.services.expression_learner import run_learning_round  # noqa: E402

if TYPE_CHECKING:
    from qq_bot.services.quota import QuotaService


class _ArgumentParser(argparse.ArgumentParser):
    """argparse 的参数错误默认退出码是 2，与“部分群失败”的 2 冲突，改为 1。"""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _build_quota(settings: BotSettings, repository: ChatMemoryRepository) -> "QuotaService | None":
    """镜像 runtime.py 的 QuotaService 构造（quota_usage 与消息同库）；
    构造失败则退回 None——配额是刹车，不是运行前提。"""
    if not settings.quota_enabled:
        return None
    try:
        from qq_bot.services.quota import QuotaService

        return QuotaService(settings, repository)
    except Exception:
        print("配额服务构造失败，本轮不限批次数（quota=None）", file=sys.stderr)
        return None


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.expression_learn_enabled:
        print("表达学习未开启（EXPRESSION_LEARN_ENABLED=false）")
        return 1
    updates: dict[str, int] = {}
    if args.days is not None:
        updates["expression_learn_lookback_days"] = args.days
    if args.max_batches is not None:
        updates["expression_learn_max_batches"] = args.max_batches
    if updates:
        settings = settings.model_copy(update=updates)
    repository = ChatMemoryRepository(
        settings.chat_memory_path, retention_days=settings.chat_memory_retention_days
    )
    await repository.open()
    try:
        if args.all:
            groups = await repository.distinct_group_ids(
                days=settings.expression_learn_lookback_days
            )
            if not groups:
                print(
                    f"最近 {settings.expression_learn_lookback_days} 天窗口内没有群消息，无事可做"
                )
                return 0
        else:
            groups = list(args.group)
        quota = _build_quota(settings, repository)
        exit_code = 0
        for group_id in groups:
            try:
                summary = await run_learning_round(
                    group_id=group_id,
                    memory_store=repository,
                    settings=settings,
                    complete=request_completion,
                    quota=quota,
                )
            except Exception as exc:
                # 单群意外失败不阻断其余群；round 自身的可预期失败走 status=error 汇总
                print(
                    json.dumps(
                        {"group_id": group_id, "status": "error", "error": type(exc).__name__},
                        ensure_ascii=False,
                    )
                )
                exit_code = 2
                continue
            print(json.dumps({"group_id": group_id, **asdict(summary)}, ensure_ascii=False))
            if summary.status == "error":
                exit_code = 2
        return exit_code
    finally:
        await repository.close()


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=int, action="append", default=[], help="群号，可重复")
    parser.add_argument("--all", action="store_true", help="学习 chat_messages 中全部群")
    parser.add_argument("--days", type=int, default=None, help="覆盖回看天数")
    parser.add_argument("--max-batches", type=int, default=None, help="覆盖单轮最大批次数")
    args = parser.parse_args(argv)
    if not args.all and not args.group:
        parser.error("需要 --group <id>（可重复）或 --all")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
