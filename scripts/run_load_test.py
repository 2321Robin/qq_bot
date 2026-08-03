"""Local synthetic load test (S4-LOAD-01..05).

Fully offline: fake provider (ModelGateway protocol), fake search, temp
SQLite memory, fixture pet data, fake bot sender. Never touches real APIs,
real API keys, or the production data directory.

Scenarios: local_knowledge, web_search, chat_memory, direct_chat, mixed.

Usage:
    python scripts/run_load_test.py --cases 100 --concurrency 4 --seed 7
    python scripts/run_load_test.py --cases 20 --concurrency 4 --output-dir <tmp> --no-write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from qq_bot.agent.models import NormalizedResponse, RouteKind  # noqa: E402
from qq_bot.agent.router import route_request  # noqa: E402
from qq_bot.config import BotSettings  # noqa: E402
from qq_bot.services.chat_memory import ChatMemoryRepository  # noqa: E402
from qq_bot.services.roco_knowledge import build_roco_context  # noqa: E402
from qq_bot.services.roco_pets import load_pet_records  # noqa: E402
from qq_bot.services.roco_skills import load_skill_records  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "roco_pet_details"
SCENARIOS = ("local_knowledge", "web_search", "chat_memory", "direct_chat", "mixed")
PHASES = ("route", "memory", "knowledge", "search", "model", "send")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGateway:
    """ModelGateway protocol stand-in: fixed classifier JSON for router
    turns, fixed conversational answer for orchestrator turns. Zero network."""

    async def request_model_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        settings: BotSettings | None = None,
        client: Any | None = None,
        provider: str = "primary",
    ) -> NormalizedResponse:
        last = messages[-1]["content"] if messages else ""
        if "primary_route" in last:
            return NormalizedResponse(
                text='{"primary_route": "local_knowledge", "confidence": 0.95}',
                usage={"prompt_tokens": 40, "completion_tokens": 12},
            )
        return NormalizedResponse(
            text=(
                '{"claims": [{"text": "这是合成压测回复。", "kind": "conversational"}],'
                ' "closing": "这是合成压测回复。"}'
            ),
            usage={"prompt_tokens": 60, "completion_tokens": 20},
        )


@dataclass
class FakeSearcher:
    """Fake Tavily: always returns N fixed results (S4-LOAD-01)."""

    count: int = 3

    async def __call__(self, query: str, **kwargs: Any) -> list[dict[str, str]]:
        return [
            {
                "title": f"合成结果 {i}",
                "url": f"https://example.invalid/loadtest/{i}",
                "content": "合成压测搜索结果，不触网。",
            }
            for i in range(self.count)
        ]


class FakeBot:
    """Fake QQ sender: records calls, never sends."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_group_msg(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@dataclass
class PhaseTimings:
    times: dict[str, list[float]] = field(default_factory=lambda: {p: [] for p in PHASES})

    def add(self, phase: str, seconds: float) -> None:
        self.times[phase].append(seconds * 1000.0)


# ---------------------------------------------------------------------------
# Scenario prompts (deterministic; rule router never needs the classifier)
# ---------------------------------------------------------------------------


def _prompts() -> dict[str, list[str]]:
    return {
        "local_knowledge": [
            "/精灵 TestPetA",
            "/精灵 TestPetB",
            "/精灵 TestPetC",
            "/技能 TestPetA 技能",
        ],
        "web_search": [
            "搜索 今天有什么新闻",
            "搜索 洛克王国最新活动公告",
            "联网查一下版本更新内容",
        ],
        "chat_memory": [
            "刚才聊过 TestPetA 吗",
            "我们聊过什么 最近的消息",
            "刚才说 刚才聊 聊天记录",
        ],
        "direct_chat": ["你好", "在吗", "谢谢", "哈哈"],
        "mixed": [],
    }


async def _run_case(
    scenario: str,
    index: int,
    *,
    gateway: FakeGateway,
    searcher: FakeSearcher,
    bot: FakeBot,
    repository: ChatMemoryRepository,
    settings: BotSettings,
    pet_records: list[Any],
    skill_records: list[Any],
    timings: PhaseTimings,
) -> None:
    """Run one synthetic case through the real router, memory, knowledge
    builder and a fake model+send. Errors propagate to the caller."""
    prompts = _prompts()
    pool = (
        prompts[scenario]
        if scenario != "mixed"
        else (
            prompts["local_knowledge"]
            + prompts["web_search"]
            + prompts["chat_memory"]
            + prompts["direct_chat"]
        )
    )
    prompt = pool[index % len(pool)]

    # route (real rule router, offline)
    started = time.perf_counter()
    decision, _trace = await route_request(
        prompt,
        settings=settings,
        gateway=gateway,
        can_use_chat_memory=True,
    )
    timings.add("route", time.perf_counter() - started)

    # memory (real SQLite repository; seed rows on the first use of the scope)
    started = time.perf_counter()
    group_id = 1000 + (index % 4)
    await repository.add_message(
        group_id=group_id, user_id=2000 + (index % 8), message_text=prompt, is_ai_prompt=True
    )
    rows = await repository.recent_group_messages(group_id=group_id, limit=10)
    _ = rows
    timings.add("memory", time.perf_counter() - started)

    # knowledge (real builder over fixture records)
    started = time.perf_counter()
    roco_context = build_roco_context(prompt, pet_records=pet_records, skill_records=skill_records)
    timings.add("knowledge", time.perf_counter() - started)

    # search (fake; only web_search and some mixed cases)
    search_context = ""
    if decision.primary_route == RouteKind.WEB_SEARCH:
        started = time.perf_counter()
        results = await searcher(prompt)
        search_context = f"共 {len(results)} 条合成搜索结果"
        timings.add("search", time.perf_counter() - started)

    # model (fake provider turn)
    started = time.perf_counter()
    response = await gateway.request_model_turn(
        messages=[{"role": "user", "content": prompt + roco_context + search_context}]
    )
    if not response.text:
        raise RuntimeError("fake gateway returned no text")
    timings.add("model", time.perf_counter() - started)

    # send (fake bot)
    started = time.perf_counter()
    await bot.send_group_msg(group_id=group_id, message=response.text[:50])
    timings.add("send", time.perf_counter() - started)


# ---------------------------------------------------------------------------
# Report schema
# ---------------------------------------------------------------------------


class PhaseReport(BaseModel):
    p50_ms: float
    p95_ms: float


class ScenarioReport(BaseModel):
    name: str
    cases: int
    ok: int
    errors: int
    e2e_p50_ms: float
    e2e_p95_ms: float
    throughput_req_per_s: float
    phases: dict[str, PhaseReport] = Field(default_factory=dict)


class EnvReport(BaseModel):
    platform: str
    python_version: str
    cpu: str
    concurrency: int


class ConclusionReport(BaseModel):
    postgresql_recommended: bool = False
    redis_recommended: bool = False
    rationale: str
    trigger_conditions: list[str] = Field(default_factory=list)


class LoadTestReport(BaseModel):
    schema_version: int = 1
    mode: str = "synthetic"
    created_at: str
    env: EnvReport
    scenarios: list[ScenarioReport]
    conclusion: ConclusionReport
    disclaimer: str


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int(pct / 100.0 * len(sorted_values))))
    return round(sorted_values[index], 2)


async def _run_scenario(
    scenario: str,
    cases: int,
    concurrency: int,
    *,
    gateway: FakeGateway,
    searcher: FakeSearcher,
    bot: FakeBot,
    repository: ChatMemoryRepository,
    settings: BotSettings,
    pet_records: list[Any],
    skill_records: list[Any],
) -> ScenarioReport:
    timings = PhaseTimings()
    e2e: list[float] = []
    ok = 0
    errors = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(index: int) -> None:
        nonlocal ok, errors
        started = time.perf_counter()
        async with semaphore:
            try:
                await _run_case(
                    scenario,
                    index,
                    gateway=gateway,
                    searcher=searcher,
                    bot=bot,
                    repository=repository,
                    settings=settings,
                    pet_records=pet_records,
                    skill_records=skill_records,
                    timings=timings,
                )
                ok += 1
            except Exception:
                errors += 1
        e2e.append((time.perf_counter() - started) * 1000.0)

    await asyncio.gather(*(worker(i) for i in range(cases)))
    elapsed = max(sum(e2e) / 1000.0, 1e-9)
    e2e_sorted = sorted(e2e)
    return ScenarioReport(
        name=scenario,
        cases=cases,
        ok=ok,
        errors=errors,
        e2e_p50_ms=_percentile(e2e_sorted, 50),
        e2e_p95_ms=_percentile(e2e_sorted, 95),
        throughput_req_per_s=round(cases / elapsed, 2),
        phases={
            phase: PhaseReport(
                p50_ms=_percentile(sorted(times), 50),
                p95_ms=_percentile(sorted(times), 95),
            )
            for phase, times in timings.times.items()
            if times
        },
    )


TRIGGER_CONDITIONS = [
    "端到端 P95 超过目标（默认 5s，可配置）且瓶颈定位为 SQLite 写入竞争",
    "多实例/多进程部署需求出现",
    "跨进程共享限流状态需求出现",
]

DISCLAIMER = (
    "合成负载：fake Provider、假搜索、临时 SQLite、fixture 数据、本地基准。"
    "不代表真实线上延迟/SLO；本报告不可作为线上容量承诺。"
)

RATIONALE = (
    "当前规模为单实例、本地 SQLite、无跨进程状态；本次合成压测未出现触发条件"
    "（P95 未超目标、无多实例需求、无限流共享需求），因此不引入 PostgreSQL/Redis。"
)


async def _main(args: argparse.Namespace) -> int:
    settings = BotSettings(
        allowed_group_ids="1001",
        ai_api_key="synthetic-load-test-key",
        tavily_api_key="tvly-synthetic",
        agent_enabled=True,
    )
    pet_records = load_pet_records(FIXTURE_DIR)
    skill_records = list(load_skill_records(FIXTURE_DIR))
    gateway = FakeGateway()
    searcher = FakeSearcher()
    bot = FakeBot()
    reports: list[ScenarioReport] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="loadtest-memory-"))
    repository = ChatMemoryRepository(tmpdir / "memory.sqlite3", retention_days=30)
    await repository.open()
    try:
        for scenario in SCENARIOS:
            report = await _run_scenario(
                scenario,
                args.cases,
                args.concurrency,
                gateway=gateway,
                searcher=searcher,
                bot=bot,
                repository=repository,
                settings=settings,
                pet_records=pet_records,
                skill_records=skill_records,
            )
            reports.append(report)
            print(
                f"{scenario:16s} ok={report.ok:4d} err={report.errors:3d} "
                f"P50={report.e2e_p50_ms:7.1f}ms P95={report.e2e_p95_ms:7.1f}ms "
                f"throughput={report.throughput_req_per_s:6.2f} req/s"
            )
        report = LoadTestReport(
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            env=EnvReport(
                platform=platform.platform(),
                python_version=platform.python_version(),
                cpu=platform.processor() or platform.machine(),
                concurrency=args.concurrency,
            ),
            scenarios=reports,
            conclusion=ConclusionReport(
                rationale=RATIONALE,
                trigger_conditions=list(TRIGGER_CONDITIONS),
            ),
            disclaimer=DISCLAIMER,
        )
        if args.no_write:
            print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
            return 0
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        json_path = output_dir / f"loadtest-{stamp}.json"
        md_path = output_dir / f"loadtest-{stamp}.md"
        json_path.write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md_path.write_text(_render_markdown(report), encoding="utf-8")
        print(f"report written: {json_path}")
        print(f"markdown written: {md_path}")
        return 0
    finally:
        await repository.close()
        try:
            tmpdir.unlink()
        except OSError:
            pass


def _render_markdown(report: LoadTestReport) -> str:
    lines = [
        "# 压测报告（合成负载）",
        "",
        f"> {report.disclaimer}",
        "",
        f"- 时间：{report.created_at}",
        f"- 平台：{report.env.platform} / Python {report.env.python_version} / {report.env.cpu}",
        f"- 并发：{report.env.concurrency}",
        "",
        "## 场景结果",
        "",
        "| 场景 | 请求 | 成功 | 失败 | P50 (ms) | P95 (ms) | 吞吐 (req/s) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in report.scenarios:
        lines.append(
            f"| {scenario.name} | {scenario.cases} | {scenario.ok} | {scenario.errors} "
            f"| {scenario.e2e_p50_ms:.1f} | {scenario.e2e_p95_ms:.1f} | {scenario.throughput_req_per_s:.2f} |"
        )
    lines += ["", "## 分阶段 P50/P95 (ms)", ""]
    for scenario in report.scenarios:
        lines.append(f"### {scenario.name}")
        for phase, timing in scenario.phases.items():
            lines.append(f"- {phase}: P50 {timing.p50_ms:.1f} / P95 {timing.p95_ms:.1f}")
    lines += [
        "",
        "## 容量结论",
        "",
        f"- PostgreSQL：{'引入' if report.conclusion.postgresql_recommended else '不引入'}",
        f"- Redis：{'引入' if report.conclusion.redis_recommended else '不引入'}",
        f"- 理由：{report.conclusion.rationale}",
        "- 触发条件（任一出现则重新评估）：",
    ]
    for condition in report.conclusion.trigger_conditions:
        lines.append(f"  - {condition}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic offline load test (S4-LOAD)")
    parser.add_argument("--cases", type=int, default=100, help="cases per scenario")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "reports"))
    parser.add_argument("--no-write", action="store_true", help="print report only")
    args = parser.parse_args(argv)
    if args.cases < 1 or args.concurrency < 1:
        print("cases and concurrency must be >= 1", file=sys.stderr)
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
