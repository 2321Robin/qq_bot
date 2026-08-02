"""Evidence store, grounding verifier and answer rendering (S2-EVID-01..09).

The grounding checks are deterministic and cannot be disabled; semantic
verification (optional, ``ai_semantic_verifier_enabled``) runs after them.
Repair is allowed exactly once; claims still unsupported afterwards are
dropped. Failure reporting is deliberately structural — category, tool,
evidence ids and counts — never the draft text (S2-EVID-09).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit

from qq_bot.agent.models import (
    Claim,
    Evidence,
    GroundedAnswer,
    ToolResult,
)
from qq_bot.config import BotSettings

Verdict = Literal["supported", "unsupported", "insufficient"]


class SemanticVerifier:
    """Protocol: one semantic verdict per claim against its evidence."""

    async def verify(self, claim: Claim, evidence: Evidence) -> Verdict:  # pragma: no cover
        raise NotImplementedError


_VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "unsupported", "insufficient"]}
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

_VERIFIER_SYSTEM_PROMPT = (
    "你是事实核验器。判断“声明”是否被“证据”充分支持。"
    '只能输出严格 JSON：{"verdict": "supported" | "unsupported" | "insufficient"}。'
    "证据不足以判定时输出 insufficient；证据与声明矛盾时输出 unsupported。"
)


class ModelSemanticVerifier(SemanticVerifier):
    """Gateway-backed semantic verifier (S2-EVID-07, Task 5 plan). Uses
    ``ai_verifier_model`` when set, else the shared ``ai_model``. Any model
    or parsing failure degrades to ``insufficient`` — never ``supported``."""

    def __init__(self, gateway: Any, settings: BotSettings) -> None:
        self._gateway = gateway
        self._settings = settings

    async def verify(self, claim: Claim, evidence: Evidence) -> Verdict:
        try:
            response = await self._gateway.request_model_turn(
                messages=[
                    {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"声明：{claim.text}\n"
                            f"证据（{evidence.title or '无标题'}）："
                            f"{_evidence_text_for_verify(evidence)}"
                        ),
                    },
                ],
                tools=None,
                tool_choice=None,
                response_format={"type": "json_object", "schema": _VERIFIER_SCHEMA},
                settings=self._settings,
            )
        except Exception:
            return "insufficient"
        text = response.text if response is not None else None
        if not text:
            return "insufficient"
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return "insufficient"
        verdict = payload.get("verdict") if isinstance(payload, dict) else None
        if verdict not in ("supported", "unsupported", "insufficient"):
            return "insufficient"
        return verdict


def _evidence_text_for_verify(evidence: Evidence) -> str:
    return json.dumps(evidence.model_dump(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class EvidenceEntry:
    evidence: Evidence
    tool: str
    tool_status: str
    warnings: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True)
class Problem:
    """Structural failure detail — no claim text, no draft content."""

    code: str
    tool: str | None = None
    evidence_id: str | None = None
    count: int = 1


def normalize_web_url(url: str) -> str:
    """Case-fold scheme/host, drop fragment and trailing dot in host. Used for
    byte-equality of visible URLs against Web evidence (S2-EVID-05)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path or ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{host}{path}{query}"


class EvidenceStore:
    """Per-request evidence; ids are unique within the request and the prefix
    must match the source type (L/W/M namespaces)."""

    def __init__(self) -> None:
        self._entries: dict[str, EvidenceEntry] = {}

    def add(self, tool_result: ToolResult) -> None:
        for evidence in tool_result.evidence:
            if evidence.id in self._entries:
                raise ValueError(f"duplicate evidence id in request: {evidence.id}")
            self._entries[evidence.id] = EvidenceEntry(
                evidence=evidence,
                tool=tool_result.tool,
                tool_status=tool_result.status,
                warnings=tool_result.warnings,
                truncated=tool_result.truncated,
            )

    def get(self, evidence_id: str) -> Evidence | None:
        entry = self._entries.get(evidence_id)
        return entry.evidence if entry is not None else None

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self._entries

    def evidence(self) -> tuple[Evidence, ...]:
        """All evidence added so far, in insertion order (S2-EVID-01)."""
        return tuple(entry.evidence for entry in self._entries.values())

    def web_urls(self) -> list[str]:
        urls: list[str] = []
        for entry in self._entries.values():
            if entry.evidence.source_type == "web" and entry.evidence.url:
                urls.append(entry.evidence.url)
        return urls


class GroundingVerifier:
    """Deterministic, non-disableable grounding checks (S2-EVID-04..06)."""

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    def check_claim(self, claim: Claim) -> tuple[Verdict, list[Problem]]:
        if claim.kind == "conversational":
            return "supported", []
        if not claim.evidence_ids:
            return "unsupported", [Problem(code="no_evidence", count=1)]
        problems: list[Problem] = []
        for evidence_id in claim.evidence_ids:
            entry = self.store._entries.get(evidence_id)
            if entry is None:
                problems.append(Problem(code="missing_evidence", evidence_id=evidence_id, count=1))
                continue
            if entry.tool_status in ("not_found", "denied", "unavailable"):
                problems.append(
                    Problem(
                        code="positive_claim_on_failed_tool",
                        tool=entry.tool,
                        evidence_id=evidence_id,
                        count=1,
                    )
                )
            if entry.truncated and not entry.warnings:
                problems.append(
                    Problem(
                        code="truncated_without_warning",
                        tool=entry.tool,
                        evidence_id=evidence_id,
                        count=1,
                    )
                )
        if problems:
            return "unsupported", problems
        return "supported", []

    def check_visible_urls(self, text: str) -> list[Problem]:
        """Every visible URL in rendered text must byte-equal a normalized Web
        evidence URL; model-generated or user-copied URLs are rejected."""
        allowed = {normalize_web_url(url) for url in self.store.web_urls()}
        problems: list[Problem] = []
        for match in re.finditer(r"https?://[^\s]+", text):
            if normalize_web_url(match.group(0)) not in allowed:
                problems.append(Problem(code="unattributed_url", count=1))
        return problems


async def verify_answer(
    answer: GroundedAnswer,
    store: EvidenceStore,
    semantic: SemanticVerifier | None = None,
) -> tuple[dict[str, Verdict], list[Problem]]:
    """Grounding check for every factual claim, then optional semantic
    verification of grounded claims. Returns per-claim verdicts and the full
    structural problem list."""
    verifier = GroundingVerifier(store)
    verdicts: dict[str, Verdict] = {}
    problems: list[Problem] = []
    for claim in answer.claims:
        verdict, claim_problems = verifier.check_claim(claim)
        if claim_problems:
            verdicts[claim.text] = "unsupported"
            problems.extend(claim_problems)
            continue
        if semantic is not None and claim.kind == "factual":
            evidence = [store.get(eid) for eid in claim.evidence_ids]
            evidence = [e for e in evidence if e is not None]
            semantic_verdict: Verdict = "supported"
            for item in evidence:
                if item is not None:
                    semantic_verdict = await semantic.verify(claim, item)
                    if semantic_verdict != "supported":
                        break
            verdicts[claim.text] = semantic_verdict
            if semantic_verdict != "supported":
                problems.append(
                    Problem(
                        code="semantic_" + semantic_verdict,
                        evidence_id=claim.evidence_ids[0] if claim.evidence_ids else None,
                        count=1,
                    )
                )
        else:
            verdicts[claim.text] = "supported"
    problems.extend(verifier.check_visible_urls("\n".join(claim.text for claim in answer.claims)))
    return verdicts, problems


async def verify_and_repair(
    answer: GroundedAnswer,
    store: EvidenceStore,
    semantic: SemanticVerifier | None = None,
    *,
    repair: Callable[[GroundedAnswer, EvidenceStore], Awaitable[GroundedAnswer]] | None = None,
    max_repairs: int = 1,
) -> tuple[GroundedAnswer, list[Problem]]:
    """Verify, allow at most one repair pass, then drop unsupported claims.
    When no claims remain the caller must treat the outcome as a safe
    failure (verification_failed), never send an unverified draft."""
    current = answer
    verdicts: dict[str, Verdict] = {}
    problems: list[Problem] = []
    for attempt in range(max_repairs + 1):
        verdicts, problems = await verify_answer(current, store, semantic)
        unsupported = {text for text, verdict in verdicts.items() if verdict == "unsupported"}
        if not unsupported:
            return current, problems
        if attempt >= max_repairs or repair is None:
            break
        current = await repair(current, store)
    kept = tuple(claim for claim in current.claims if verdicts.get(claim.text) != "unsupported")
    return GroundedAnswer(claims=kept, closing=current.closing), problems


MAX_WEB_URLS_IN_RENDER = 3


def render_answer(answer: GroundedAnswer, store: EvidenceStore) -> str:
    """Natural short text. Local sources are labelled "本地图鉴" (never a
    fabricated URL); Web sources show at most three verifier-accepted URLs.
    Never emits internal JSON, tool arguments, confidence, or system policy."""
    parts: list[str] = []
    shown_urls: set[str] = set()
    for claim in answer.claims:
        if claim.kind == "conversational" or not claim.evidence_ids:
            parts.append(claim.text)
            continue
        sources: list[str] = []
        for evidence_id in claim.evidence_ids:
            evidence = store.get(evidence_id)
            if evidence is None:
                continue
            if evidence.source_type == "web" and evidence.url:
                if evidence.url not in shown_urls and len(shown_urls) < MAX_WEB_URLS_IN_RENDER:
                    sources.append(f"来源：{evidence.url}")
                    shown_urls.add(evidence.url)
            elif evidence.source_type == "local":
                sources.append("来源：本地图鉴")
                break
        if sources:
            parts.append(f"{claim.text}；{'；'.join(sources)}")
        else:
            parts.append(claim.text)
    rendered = "\n".join(parts)
    if answer.closing:
        rendered = f"{rendered}\n{answer.closing}" if rendered else answer.closing
    return rendered


def run_repair_once(
    answer: GroundedAnswer,
    store: EvidenceStore,
    rewrite: Callable[[GroundedAnswer, EvidenceStore], Awaitable[GroundedAnswer]],
) -> Awaitable[GroundedAnswer]:
    """Single repair attempt used by the orchestrator (S2-EVID-07)."""
    return rewrite(answer, store)
