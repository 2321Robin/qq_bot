"""Token budget tests (S2-TOKEN-01..08).

Counting runs against real tiktoken where the model is known and against
the conservative estimate otherwise; estimation is always flagged.
"""

from __future__ import annotations

from qq_bot.agent.models import Evidence
from qq_bot.agent.token_budget import (
    BudgetManager,
    BudgetPlan,
    ESTIMATE_FACTOR,
)
from qq_bot.config import BotSettings

SETTINGS = BotSettings(
    ai_context_window_tokens=128000,
    ai_output_reserve_tokens=2048,
    ai_token_safety_margin=1024,
)


def _evidence(title: str, body: str, eid: str = "L1") -> Evidence:
    return Evidence(
        id=eid,
        source_type="local",
        title=title,
        facts={"body": body},
        url=None,
    )


def _manager(**overrides) -> BudgetManager:
    settings = BotSettings(**{**SETTINGS.model_dump(), **overrides})
    return BudgetManager(settings)


def _fake_tiktoken(monkeypatch, *, encoding_name: str = "cl100k_base") -> None:
    """Inject a deterministic tiktoken stand-in so counting tests never
    depend on the network (the real tiktoken downloads its BPE file on
    first use, S2-TOKEN-03)."""

    class _Encoding:
        def __init__(self, name: str) -> None:
            self.name = name

        def encode(self, text: str, **kwargs):
            return list(text)  # 1 token per character, deterministic

    class _FakeTiktoken:
        def encoding_for_model(self, model: str):
            return _Encoding(encoding_name)

        def get_encoding(self, name: str):
            return _Encoding(name)

    import qq_bot.agent.token_budget as token_budget

    monkeypatch.setattr(token_budget, "_tiktoken", lambda: _FakeTiktoken())
    monkeypatch.setattr(
        token_budget,
        "_resolve_encoding",
        lambda *, model=None, encoding=None: encoding or encoding_name,
    )


def _allocate(
    manager: BudgetManager,
    *,
    system: str = "系统策略",
    question: str = "问题",
    schemas: list[dict] | None = None,
    local: list[Evidence] | None = None,
    web: list[Evidence] | None = None,
    recent: list[str] | None = None,
    summaries: list[str] | None = None,
    preferences: str | None = None,
) -> BudgetPlan:
    return manager.allocate(
        system=system,
        question=question,
        tool_schemas=schemas or [],
        local_evidence=local or [],
        web_evidence=web or [],
        recent_messages=recent or [],
        summaries=summaries or [],
        preferences=preferences,
    )


# ---------------------------------------------------------------------------
# Counting (S2-TOKEN-03)
# ---------------------------------------------------------------------------


def test_count_tokens_known_model_uses_tiktoken(monkeypatch) -> None:
    _fake_tiktoken(monkeypatch, encoding_name="o200k_base")
    manager = _manager(ai_model="gpt-4o-mini")
    count = manager.count_tokens("你好，洛克王国")

    assert count.estimated is False
    assert count.encoding == "o200k_base"
    assert count.tokens > 0


def test_count_tokens_explicit_encoding(monkeypatch) -> None:
    _fake_tiktoken(monkeypatch, encoding_name="cl100k_base")
    manager = _manager()
    count = manager.count_tokens("hello world", encoding="cl100k_base")

    assert count.estimated is False
    assert count.encoding == "cl100k_base"
    assert count.tokens > 0


def test_count_tokens_unknown_model_is_estimated() -> None:
    manager = _manager(ai_model="custom-unknown-model")
    count = manager.count_tokens("一些中文内容")

    assert count.estimated is True
    assert count.encoding is None
    assert count.tokens == int(len("一些中文内容") * ESTIMATE_FACTOR)


def test_count_tokens_falls_back_when_tiktoken_unavailable(monkeypatch) -> None:
    import qq_bot.agent.token_budget as tb

    monkeypatch.setattr(tb, "_tiktoken", lambda: None)
    manager = _manager(ai_model="gpt-4o-mini")
    count = manager.count_tokens("text")

    assert count.estimated is True
    assert count.tokens == int(len("text") * ESTIMATE_FACTOR)


# ---------------------------------------------------------------------------
# Budget formula (S2-TOKEN-01)
# ---------------------------------------------------------------------------


def test_total_budget_formula() -> None:
    manager = _manager()
    plan = _allocate(manager, local=[_evidence("t", "x" * 50)])

    assert plan.total_budget == 128000 - 2048 - 1024
    assert plan.insufficient is False
    assert plan.used_tokens <= plan.total_budget


def test_fixed_content_never_dropped_and_insufficient_fails() -> None:
    manager = _manager(
        ai_context_window_tokens=4000,
        ai_output_reserve_tokens=2048,
        ai_token_safety_margin=1024,
    )
    plan = _allocate(manager, system="安全策略" * 500, question="问题" * 500, schemas=[])

    assert plan.insufficient is True
    assert plan.reason == "fixed_content_exceeds_budget"
    assert plan.kept_local == ()
    assert plan.kept_recent == ()


def _bulk_evidence(count: int, body_chars: int = 3000) -> list[Evidence]:
    return [_evidence(f"宠物{i}", "内容" * (body_chars // 2), f"L{i + 1}") for i in range(count)]


# ---------------------------------------------------------------------------
# Per-source quotas (S2-TOKEN-04)
# ---------------------------------------------------------------------------


def test_evidence_over_quota_drops_whole_units_from_tail() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    units = _bulk_evidence(10)
    plan = _allocate(manager, local=units)

    local_alloc = next(a for a in plan.allocations if a.source == "local_evidence")
    assert local_alloc.dropped_units >= 1
    assert local_alloc.reason == "quota_exceeded"
    assert len(plan.kept_local) < len(units)
    # dropped units are whole — the kept evidence is never truncated mid-unit
    assert all(isinstance(unit, Evidence) for unit in plan.kept_local)
    # the first (most recent) unit survives
    assert plan.kept_local[0].id == "L1"


def test_recent_messages_drop_oldest_first() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    messages = [f"旧消息{i}" + "长" * 1000 for i in range(10)]
    plan = _allocate(manager, recent=messages)

    kept = plan.kept_recent
    assert len(kept) < len(messages)
    assert kept == tuple(messages[: len(kept)])  # newest-first: head survives


def test_web_quota_only_when_web_evidence_present() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    web_only = _allocate(
        manager,
        web=[
            Evidence(
                id="W1",
                source_type="web",
                title="网页",
                facts={"body": "网页"},
                url="https://example.com/x",
            )
        ],
    )
    assert any(a.source == "web_evidence" for a in web_only.allocations)

    local_only = _allocate(manager, local=[_evidence("L1", "本地", "L1")])
    assert not any(a.source == "web_evidence" for a in local_only.allocations)


def test_preference_shortened_to_cap() -> None:
    manager = _manager(agent_budget_preference_max_tokens=256)
    long_pref = "偏好" * 1000
    plan = _allocate(manager, preferences=long_pref)

    pref_alloc = next(a for a in plan.allocations if a.source == "preferences")
    assert pref_alloc.dropped_units == 1
    assert plan.kept_preference is not None
    assert len(plan.kept_preference) < len(long_pref)
    assert manager.count_tokens(plan.kept_preference).tokens <= 256


def test_unicode_never_split_across_unit_boundary() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    # a single unit containing astral-plane characters is kept or dropped whole
    emoji_unit = _evidence("表情", "😀😀😀😀" * 40, "L1")
    plan = _allocate(manager, local=[emoji_unit])

    if plan.kept_local:
        text = plan.kept_local[0].facts["body"]
        assert "😀" in text and "�" not in text
    else:
        assert any(a.dropped_units >= 1 for a in plan.allocations)


# ---------------------------------------------------------------------------
# Yield (S2-TOKEN-04)
# ---------------------------------------------------------------------------


def test_unused_low_priority_quota_yields_to_local_evidence() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    local_units = _bulk_evidence(12)
    tiny_recent = ["最近一条"]

    without_yield = _allocate(manager, local=local_units)
    with_yield = _allocate(manager, local=local_units, recent=tiny_recent)

    # the tiny recent list leaves its whole quota unused -> local keeps more
    assert len(with_yield.kept_local) > len(without_yield.kept_local)


# ---------------------------------------------------------------------------
# Diagnostics (S2-TOKEN-08)
# ---------------------------------------------------------------------------


def test_plan_diagnostics_never_contain_raw_content() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    plan = _allocate(
        manager,
        system="绝密系统策略文本",
        question="用户的原始问题内容",
        local=[_evidence("宠物A", "证据正文内容", "L1")],
    )

    assert plan.insufficient is False
    # diagnostic fields only — kept content is returned to the orchestrator
    # separately and is not part of the diagnostic record (S2-TOKEN-08)
    serialized = str(plan.allocations)
    assert "绝密系统策略文本" not in serialized
    assert "用户的原始问题内容" not in serialized
    assert "证据正文内容" not in serialized


def test_plan_reports_per_source_counts_and_estimated_flags() -> None:
    manager = _manager(ai_model="custom-model", ai_context_window_tokens=60000)
    plan = _allocate(manager, local=[_evidence("A", "内容", "L1")])

    local_alloc = next(a for a in plan.allocations if a.source == "local_evidence")
    assert local_alloc.tokens > 0
    assert local_alloc.estimated is True  # unknown model -> estimated
    assert plan.fixed_tokens > 0
    assert plan.fixed_estimated is True


def test_empty_sources_get_no_allocation() -> None:
    manager = _manager(ai_context_window_tokens=60000)
    plan = _allocate(manager)

    assert plan.allocations == ()
    assert plan.kept_local == ()
    assert plan.kept_recent == ()
    assert plan.kept_preference is None
    assert plan.insufficient is False


def test_orchestrator_protocol_shape() -> None:
    """The plan exposes the attributes the orchestrator's seam reads."""
    manager = _manager(ai_context_window_tokens=60000)
    plan = _allocate(manager)
    assert hasattr(plan, "insufficient")
    assert plan.insufficient is False
