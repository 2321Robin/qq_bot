"""Evidence verifier and answer rendering tests (S2-EVID-04..09)."""

from __future__ import annotations

import asyncio

import pytest

from qq_bot.agent.evidence import (
    EvidenceStore,
    GroundingVerifier,
    SemanticVerifier,
    normalize_web_url,
    render_answer,
    verify_and_repair,
    verify_answer,
)
from qq_bot.agent.models import Claim, Evidence, GroundedAnswer, ToolResult


def _store() -> EvidenceStore:
    store = EvidenceStore()
    store.add(
        ToolResult(
            tool="lookup_pet",
            status="ok",
            evidence=(
                Evidence(
                    id="L1",
                    source_type="local",
                    title="TestPetA",
                    facts={"number": "001"},
                ),
            ),
        )
    )
    store.add(
        ToolResult(
            tool="search_web",
            status="ok",
            evidence=(
                Evidence(
                    id="W1",
                    source_type="web",
                    title="新闻",
                    url="https://example.com/news/1",
                    facts={"snippet": "合成新闻"},
                ),
            ),
        )
    )
    return store


def _factual(text: str, evidence_ids: tuple[str, ...] = ()) -> Claim:
    return Claim(text=text, evidence_ids=evidence_ids)


def test_store_round_trip_and_uniqueness() -> None:
    store = _store()
    assert store.has("L1")
    assert store.has("W1")
    assert not store.has("L2")
    assert store.get("L1") is not None
    with pytest.raises(ValueError, match="duplicate evidence id"):
        store.add(
            ToolResult(
                tool="lookup_pet",
                status="ok",
                evidence=(Evidence(id="L1", source_type="local", title="dup"),),
            )
        )


def test_normalize_web_url() -> None:
    assert normalize_web_url("HTTPS://Example.COM/Path?q=1#frag") == (
        "https://example.com/Path?q=1"
    )
    assert normalize_web_url("https://example.com./x") == "https://example.com/x"


def test_factual_claim_without_evidence_rejected() -> None:
    store = _store()
    verifier = GroundingVerifier(store)
    verdict, problems = verifier.check_claim(_factual("编号是 001"))
    assert verdict == "unsupported"
    assert any(problem.code == "no_evidence" for problem in problems)


def test_factual_claim_with_missing_evidence_id_rejected() -> None:
    store = _store()
    verifier = GroundingVerifier(store)
    verdict, problems = verifier.check_claim(_factual("编号是 001", ("L99",)))
    assert verdict == "unsupported"
    assert any(problem.code == "missing_evidence" for problem in problems)


def test_positive_claim_on_failed_tool_rejected() -> None:
    store = EvidenceStore()
    store.add(ToolResult(tool="lookup_pet", status="not_found", evidence=()))
    # not_found results carry no evidence, so any positive claim must fail
    verifier = GroundingVerifier(store)
    verdict, _ = verifier.check_claim(_factual("TestPetZ 存在", ("L1",)))
    assert verdict == "unsupported"


def test_conversational_claim_needs_no_evidence() -> None:
    store = _store()
    verifier = GroundingVerifier(store)
    claim = Claim(text="你好呀", kind="conversational")
    verdict, problems = verifier.check_claim(claim)
    assert verdict == "supported"
    assert problems == []


def test_truncated_evidence_without_warning_rejected() -> None:
    store = EvidenceStore()
    store.add(
        ToolResult(
            tool="search_web",
            status="ok",
            truncated=True,
            evidence=(
                Evidence(id="W1", source_type="web", title="长文", url="https://a.example/1"),
            ),
        )
    )
    verifier = GroundingVerifier(store)
    verdict, problems = verifier.check_claim(_factual("长文内容", ("W1",)))
    assert verdict == "unsupported"
    assert any(problem.code == "truncated_without_warning" for problem in problems)
    store2 = EvidenceStore()
    store2.add(
        ToolResult(
            tool="search_web",
            status="ok",
            truncated=True,
            warnings=("结果被截断",),
            evidence=(
                Evidence(id="W1", source_type="web", title="长文", url="https://a.example/1"),
            ),
        )
    )
    verifier2 = GroundingVerifier(store2)
    verdict2, _ = verifier2.check_claim(_factual("长文内容", ("W1",)))
    assert verdict2 == "supported"


def test_fabricated_url_rejected() -> None:
    store = _store()
    verifier = GroundingVerifier(store)
    problems = verifier.check_visible_urls("详情见 https://evil.example.com/steal")
    assert any(problem.code == "unattributed_url" for problem in problems)
    problems_ok = verifier.check_visible_urls("详情见 https://example.com/news/1")
    assert problems_ok == []


def test_verify_answer_with_semantic_verifier() -> None:
    class FailingSemantic(SemanticVerifier):
        async def verify(self, claim, evidence):
            return "unsupported"

    store = _store()
    answer = GroundedAnswer(claims=(_factual("编号是 001", ("L1",)),))
    verdicts, problems = asyncio.run(verify_answer(answer, store, FailingSemantic()))
    assert verdicts["编号是 001"] == "unsupported"
    assert any(problem.code == "semantic_unsupported" for problem in problems)


def test_repair_once_then_drop_unsupported_claims() -> None:
    store = _store()

    async def bad_repair(answer: GroundedAnswer, store: EvidenceStore) -> GroundedAnswer:
        # repair still produces an unsupported claim (no evidence reference)
        return GroundedAnswer(claims=(_factual("还是没引用证据"),))

    answer = GroundedAnswer(
        claims=(_factual("编号是 001", ("L1",)), _factual("无证据"), _factual("寒暄", ("L1",)))
    )
    repaired, problems = asyncio.run(verify_and_repair(answer, store, repair=bad_repair))
    assert all(claim.evidence_ids for claim in repaired.claims)
    assert any(problem.code == "no_evidence" for problem in problems)


def test_repair_success_keeps_all_claims() -> None:
    store = _store()

    async def good_repair(answer: GroundedAnswer, store: EvidenceStore) -> GroundedAnswer:
        return GroundedAnswer(
            claims=(_factual("编号是 001", ("L1",)), _factual("来源可靠", ("L1",)))
        )

    answer = GroundedAnswer(claims=(_factual("无证据"),))
    repaired, problems = asyncio.run(verify_and_repair(answer, store, repair=good_repair))
    assert len(repaired.claims) == 2
    assert problems == []


def test_verify_answer_checks_visible_urls() -> None:
    store = _store()
    answer = GroundedAnswer(claims=(_factual("详情 https://evil.example.com/x", ("L1",)),))
    _, problems = asyncio.run(verify_answer(answer, store))
    assert any(problem.code == "unattributed_url" for problem in problems)


def test_render_answer_local_uses_label_not_url() -> None:
    store = _store()
    answer = GroundedAnswer(
        claims=(_factual("TestPetA 的编号是 001", ("L1",)),),
        closing="还需要别的吗？",
    )
    rendered = render_answer(answer, store)
    assert "001" in rendered
    assert "本地图鉴" in rendered
    assert "example.com" not in rendered
    assert rendered.endswith("还需要别的吗？")


def test_render_answer_web_urls_capped_at_three() -> None:
    store = EvidenceStore()
    for index in range(5):
        store.add(
            ToolResult(
                tool="search_web",
                status="ok",
                evidence=(
                    Evidence(
                        id=f"W{index + 1}",
                        source_type="web",
                        title=f"来源{index}",
                        url=f"https://example.com/news/{index + 1}",
                    ),
                ),
            )
        )
    answer = GroundedAnswer(
        claims=(
            _factual("第一条", ("W1", "W2")),
            _factual("第二条", ("W3",)),
            _factual("第三条", ("W4",)),
            _factual("第四条", ("W5",)),
        )
    )
    rendered = render_answer(answer, store)
    url_count = rendered.count("来源：https://")
    assert url_count <= 3


def test_render_answer_never_emits_internal_fields() -> None:
    store = _store()
    answer = GroundedAnswer(claims=(_factual("编号是 001", ("L1",)),))
    rendered = render_answer(answer, store)
    for leaked in ("confidence", "allowed_tools", "lookup_pet", "arguments", "RouteDecision"):
        assert leaked not in rendered


def test_problem_summary_has_no_draft_text() -> None:
    store = _store()
    answer = GroundedAnswer(claims=(_factual("机密草稿内容", ("L99",)),))
    _, problems = asyncio.run(verify_answer(answer, store))
    serialized = str([problem for problem in problems])
    assert "机密草稿内容" not in serialized
    assert all(hasattr(problem, "code") for problem in problems)


# ---------------------------------------------------------------------------
# ModelSemanticVerifier (Task 13; completes Task 5's gateway-backed verifier)
# ---------------------------------------------------------------------------


class FakeGateway:
    def __init__(self, text: str | None = None, *, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def request_model_turn(
        self,
        *,
        messages,
        tools=None,
        tool_choice=None,
        response_format=None,
        settings=None,
        client=None,
        provider="primary",
    ):
        self.calls.append(
            {"messages": messages, "response_format": response_format, "settings": settings}
        )
        if self.error is not None:
            raise self.error
        return type("R", (), {"text": self.text})()


def _verifier(gateway, settings=None):
    from qq_bot.agent.evidence import ModelSemanticVerifier
    from qq_bot.config import BotSettings

    return ModelSemanticVerifier(
        gateway, settings or BotSettings(ai_api_key="test-secret", ai_model="test-model")
    )


@pytest.mark.asyncio
async def test_model_verifier_returns_model_verdict() -> None:
    gateway = FakeGateway('{"verdict": "supported"}')
    verdict = await _verifier(gateway).verify(
        Claim(text="声明"), Evidence(id="L1", source_type="local")
    )
    assert verdict == "supported"
    assert gateway.calls[0]["response_format"] == {
        "type": "json_object",
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["supported", "unsupported", "insufficient"]}
            },
            "required": ["verdict"],
            "additionalProperties": False,
        },
    }


@pytest.mark.asyncio
async def test_model_verifier_degrades_to_insufficient_on_failure() -> None:
    failing = FakeGateway(error=RuntimeError("boom"))
    assert (
        await _verifier(failing).verify(Claim(text="x"), Evidence(id="L1", source_type="local"))
        == "insufficient"
    )

    empty = FakeGateway(text=None)
    assert (
        await _verifier(empty).verify(Claim(text="x"), Evidence(id="L1", source_type="local"))
        == "insufficient"
    )

    garbage = FakeGateway(text="not json")
    assert (
        await _verifier(garbage).verify(Claim(text="x"), Evidence(id="L1", source_type="local"))
        == "insufficient"
    )

    wrong_key = FakeGateway(text='{"verdict": "maybe"}')
    assert (
        await _verifier(wrong_key).verify(Claim(text="x"), Evidence(id="L1", source_type="local"))
        == "insufficient"
    )

    wrong_shape = FakeGateway(text='{"other": 1}')
    assert (
        await _verifier(wrong_shape).verify(Claim(text="x"), Evidence(id="L1", source_type="local"))
        == "insufficient"
    )
