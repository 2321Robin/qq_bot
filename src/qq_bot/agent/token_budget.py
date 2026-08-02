"""Token budget manager (S2-TOKEN-01..08, S2-CONFIG-02).

Known model encodings count with tiktoken; unknown models fall back to a
conservative estimate flagged ``estimated=True`` — estimated tokens are
never presented as provider usage. Fixed content (system policy, current
question, allowed tool schemas, output reserve) is never dropped; when the
minimum necessary still overflows the budget, ``allocate`` reports
``insufficient`` and the model is not called (S2-TOKEN-05/07). Shrinking
drops whole units (messages, evidence) on structural boundaries first, then
shortens the preference field — never mid-JSON, mid-URL, mid-evidence-id
or mid-Unicode (S2-TOKEN-06). Diagnostics carry counts and reasons only,
never raw content (S2-TOKEN-08).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from qq_bot.agent.models import Evidence
from qq_bot.config import BotSettings

ESTIMATE_FACTOR = 1.5
_REASON_QUOTA = "quota_exceeded"
_REASON_FIXED = "fixed_content_exceeds_budget"
_REASON_PREFERENCE = "preference_shortened"

# Yield order: when a lower-priority source leaves quota unused, the slack
# goes to higher-priority sources that had to drop units (S2-TOKEN-04).
_SOURCE_PRIORITY = (
    "local_evidence",
    "web_evidence",
    "summaries",
    "recent_messages",
    "preferences",
)


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    estimated: bool
    encoding: str | None = None


@dataclass(frozen=True)
class SourceAllocation:
    """Diagnostic per-source budget result — counts and reasons only,
    never the content itself (S2-TOKEN-08)."""

    source: str
    tokens: int
    estimated: bool
    dropped_units: int
    reason: str = ""


@dataclass(frozen=True)
class BudgetPlan:
    """Full allocation result. ``kept_*`` carry the surviving content for
    the orchestrator to put into the context; the diagnostic fields never
    contain raw content. ``insufficient`` means the model must not be
    called (S2-TOKEN-07)."""

    insufficient: bool
    total_budget: int
    fixed_tokens: int
    fixed_estimated: bool
    allocations: tuple[SourceAllocation, ...] = ()
    reason: str = ""
    kept_local: tuple[Evidence, ...] = ()
    kept_web: tuple[Evidence, ...] = ()
    kept_recent: tuple[str, ...] = ()
    kept_summaries: tuple[str, ...] = ()
    kept_preference: str | None = None

    @property
    def used_tokens(self) -> int:
        return self.fixed_tokens + sum(alloc.tokens for alloc in self.allocations)


def _tiktoken():
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken
    except ImportError:
        return None


def _encoding_for_model(model: str) -> str | None:
    tiktoken = _tiktoken()
    if tiktoken is None:
        return None
    try:
        return tiktoken.encoding_for_model(model).name
    except Exception:
        return None


def _resolve_encoding(*, model: str | None, encoding: str | None) -> str | None:
    if encoding:
        return encoding
    if model:
        return _encoding_for_model(model)
    return None


class BudgetManager:
    """Token budgeter. Constructor takes the settings (and, in tests, a
    fake model name / encoding); counting resolves per call so a changed
    ``ai_model`` is respected."""

    def __init__(self, settings: BotSettings, *, model: str | None = None) -> None:
        self._settings = settings
        self._model = model

    def count_tokens(
        self,
        text: str,
        *,
        model: str | None = None,
        encoding: str | None = None,
    ) -> TokenCount:
        """Count tokens with a known tiktoken encoding, or estimate
        conservatively and flag ``estimated`` (S2-TOKEN-03)."""
        resolved = _resolve_encoding(
            model=model or self._model or self._settings.ai_model, encoding=encoding
        )
        tiktoken = _tiktoken()
        if resolved is not None and tiktoken is not None:
            try:
                enc = tiktoken.get_encoding(resolved)
                count = len(enc.encode(text, disallowed_special=()))
                return TokenCount(tokens=count, estimated=False, encoding=resolved)
            except Exception:
                pass
        return TokenCount(tokens=int(len(text) * ESTIMATE_FACTOR), estimated=True, encoding=None)

    def _total_budget(self) -> int:
        return (
            self._settings.ai_context_window_tokens
            - self._settings.ai_output_reserve_tokens
            - self._settings.ai_token_safety_margin
        )

    def allocate(
        self,
        *,
        system: str,
        question: str,
        tool_schemas: list[dict[str, Any]],
        local_evidence: list[Evidence],
        web_evidence: list[Evidence],
        recent_messages: list[str],
        summaries: list[str],
        preferences: str | None,
    ) -> BudgetPlan:
        """Distribute the input budget across sources (S2-TOKEN-01..06)."""
        budget = self._total_budget()

        fixed_texts = {
            "system": system,
            "question": question,
            "tool_schemas": json.dumps(tool_schemas, ensure_ascii=False, sort_keys=True),
        }
        fixed_counts = {key: self.count_tokens(text) for key, text in fixed_texts.items()}
        fixed_tokens = sum(count.tokens for count in fixed_counts.values())
        fixed_estimated = any(count.estimated for count in fixed_counts.values())
        if fixed_tokens > budget:
            return BudgetPlan(
                insufficient=True,
                total_budget=budget,
                fixed_tokens=fixed_tokens,
                fixed_estimated=fixed_estimated,
                reason=_REASON_FIXED,
            )

        remaining = budget - fixed_tokens
        quotas: dict[str, int] = {}
        if local_evidence:
            quotas["local_evidence"] = int(remaining * self._settings.agent_budget_local_ratio)
        if web_evidence:
            quotas["web_evidence"] = int(remaining * self._settings.agent_budget_web_ratio)
        if recent_messages:
            quotas["recent_messages"] = int(remaining * self._settings.agent_budget_recent_ratio)
        if summaries:
            quotas["summaries"] = int(remaining * self._settings.agent_budget_summary_ratio)
        if preferences:
            quotas["preferences"] = min(
                self._settings.agent_budget_preference_max_tokens, remaining
            )

        fitted, dropped_tokens = self._fit_sources(
            quotas,
            local_evidence=local_evidence,
            web_evidence=web_evidence,
            recent_messages=recent_messages,
            summaries=summaries,
            preferences=preferences,
        )
        self._yield_slack(quotas, fitted, dropped_tokens)

        allocations = tuple(
            SourceAllocation(
                source=source,
                tokens=fitted[source].tokens,
                estimated=fitted[source].estimated,
                dropped_units=fitted[source].dropped_units,
                reason=_REASON_QUOTA if fitted[source].dropped_units else "",
            )
            for source in _SOURCE_PRIORITY
            if source in fitted
        )
        return BudgetPlan(
            insufficient=False,
            total_budget=budget,
            fixed_tokens=fixed_tokens,
            fixed_estimated=fixed_estimated,
            allocations=allocations,
            kept_local=tuple(fitted["local_evidence"].kept) if "local_evidence" in fitted else (),
            kept_web=tuple(fitted["web_evidence"].kept) if "web_evidence" in fitted else (),
            kept_recent=(
                tuple(fitted["recent_messages"].kept) if "recent_messages" in fitted else ()
            ),
            kept_summaries=(tuple(fitted["summaries"].kept) if "summaries" in fitted else ()),
            kept_preference=(
                fitted["preferences"].kept[0]
                if "preferences" in fitted and fitted["preferences"].kept
                else None
            ),
        )

    # -- internal fitting ---------------------------------------------------

    def _fit_sources(self, quotas: dict[str, int], **content) -> tuple[dict, dict]:
        fitted: dict[str, _Fitted] = {}
        dropped_tokens: dict[str, list[tuple[int, Any]]] = {}
        if "local_evidence" in quotas:
            local, dropped = self._fit_evidence(content["local_evidence"], quotas["local_evidence"])
            fitted["local_evidence"] = local
            dropped_tokens["local_evidence"] = dropped
        if "web_evidence" in quotas:
            web, dropped = self._fit_evidence(content["web_evidence"], quotas["web_evidence"])
            fitted["web_evidence"] = web
            dropped_tokens["web_evidence"] = dropped
        if "recent_messages" in quotas:
            recent, dropped = self._fit_messages(
                content["recent_messages"], quotas["recent_messages"]
            )
            fitted["recent_messages"] = recent
            dropped_tokens["recent_messages"] = dropped
        if "summaries" in quotas:
            summaries, dropped = self._fit_messages(content["summaries"], quotas["summaries"])
            fitted["summaries"] = summaries
            dropped_tokens["summaries"] = dropped
        if "preferences" in quotas:
            pref, dropped = self._fit_preference(content["preferences"], quotas["preferences"])
            fitted["preferences"] = pref
            dropped_tokens["preferences"] = dropped
        return fitted, dropped_tokens

    def _yield_slack(
        self, quotas: dict[str, int], fitted: dict[str, _Fitted], dropped: dict[str, list]
    ) -> None:
        """Give unused quota from lower-priority sources to higher-priority
        ones that dropped units (S2-TOKEN-04)."""
        slack = 0
        for source in reversed(_SOURCE_PRIORITY):
            if source not in fitted:
                continue
            slack += max(0, quotas[source] - fitted[source].tokens)
        for source in _SOURCE_PRIORITY:
            if source not in fitted:
                continue
            restored = 0
            while slack > 0 and dropped[source]:
                unit_tokens, unit = dropped[source].pop(0)
                if unit_tokens <= slack:
                    fitted[source].restore(unit, unit_tokens)
                    slack -= unit_tokens
                    restored += 1
            if restored:
                fitted[source].dropped_units -= restored
            if slack <= 0:
                break

    # -- per-source fitting ------------------------------------------------

    def _fit_evidence(self, units: list[Evidence], quota: int) -> tuple[_Fitted, list]:
        texts = [self._evidence_text(unit) for unit in units]
        counts = [self.count_tokens(text) for text in texts]
        return self._fit_units(units, counts, quota)

    def _fit_messages(self, messages: list[str], quota: int) -> tuple[_Fitted, list]:
        counts = [self.count_tokens(text) for text in messages]
        return self._fit_units(messages, counts, quota)

    def _fit_preference(self, preference: str, quota: int) -> tuple[_Fitted, list]:
        count = self.count_tokens(preference)
        if count.tokens <= quota:
            return _Fitted([preference], count.tokens, count.estimated), []
        # shorten on a character boundary until under cap (plain user text —
        # no JSON/URL/evidence structure to preserve, S2-TOKEN-06)
        text = preference
        while text:
            text = text[:-1]
            if self.count_tokens(text).tokens <= quota:
                break
        kept = _PreferenceFitted([text], self.count_tokens(text).tokens, count.estimated, 1)
        return kept, [(count.tokens - kept.tokens, preference)]

    @staticmethod
    def _fit_units(
        counted: list[Any], counts: list[TokenCount], quota: int
    ) -> tuple[_Fitted, list]:
        """Keep whole units from the head (newest-first input; the tail —
        older messages, later evidence — is what drops first)."""
        kept: list[Any] = []
        kept_tokens = 0
        estimated = False
        dropped: list[tuple[int, Any]] = []
        for unit, count in zip(counted, counts):
            estimated = estimated or count.estimated
            if kept_tokens + count.tokens <= quota:
                kept.append(unit)
                kept_tokens += count.tokens
            else:
                dropped.append((count.tokens, unit))
        return _Fitted(kept, kept_tokens, estimated, len(dropped)), dropped

    @staticmethod
    def _evidence_text(evidence: Evidence) -> str:
        return json.dumps(evidence.model_dump(), ensure_ascii=False, sort_keys=True)


class _Fitted:
    """Mutable accumulator for one source's surviving units."""

    def __init__(
        self, kept: list[Any], tokens: int, estimated: bool, dropped_units: int = 0
    ) -> None:
        self.kept = list(kept)
        self.tokens = tokens
        self.estimated = estimated
        self.dropped_units = dropped_units

    def restore(self, unit: Any, unit_tokens: int) -> None:
        self.kept.append(unit)
        self.tokens += unit_tokens


class _PreferenceFitted(_Fitted):
    """Restoring a shortened preference replaces the shortened text."""

    def restore(self, unit: Any, unit_tokens: int) -> None:
        self.kept.clear()
        self.kept.append(unit)
        self.tokens = unit_tokens
