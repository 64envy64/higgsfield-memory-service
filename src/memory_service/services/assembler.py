"""Tiered context assembler.

Three tiers with explicit budget allocation:

    Tier 1 — "Known facts about this user"        (up to ~40% of budget)
              Stable, high-confidence active facts/preferences.
              Gated ON only if QueryAnalyzer.profile_relevant is True.

    Tier 2 — "Relevant memories"                  (up to ~40% of budget)
              Memory candidates returned by retrievers (vector / FTS / graph).
              Gated ON if any retriever returned at least one candidate with
              score >= MIN_RELEVANCE, or any of the query's entities matched
              a stored entity for this scope.

    Tier 3 — "From recent conversations"          (remaining budget)
              Raw turn snippets from retrievers — provenance / freshness.

When both gates are closed, return empty (Invariant 3). Tier budgets are
upper bounds — unused budget flows down to the next tier, not back up,
so Tier 1 stays protected against turn-text pollution.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from memory_service.schemas.recall import Citation
from memory_service.services.retrievers import Candidate
from memory_service.util.tokens import approx_token_count, trim_to_tokens


@dataclass
class TierBudget:
    max_tokens: int
    tier1_pct: float = 0.4
    tier2_pct: float = 0.4
    # Tier 3 gets whatever's left (≈20% in the symmetric case).

    @property
    def tier1(self) -> int:
        return int(self.max_tokens * self.tier1_pct)

    @property
    def tier2(self) -> int:
        return int(self.max_tokens * self.tier2_pct)


@dataclass
class GateState:
    tier1_open: bool
    tier2_open: bool


def decide_gate(
    *,
    profile_relevant: bool,
    is_open_ended_about_user: bool,
    has_memory_signal: bool,
    has_turn_signal: bool,
    has_entity_match: bool,
) -> GateState:
    """Apply the gate rules (Invariant 3 — empty over wrong).

    Inputs:
        profile_relevant         — query is plausibly about the user.
        is_open_ended_about_user — intent ∈ {recent_context, exploratory} AND
                                   profile_relevant — i.e. "tell me about me",
                                   "what do you remember about me", "what's
                                   relevant from earlier". These are the only
                                   queries where dumping stable facts without
                                   a specific match is appropriate.
        has_memory_signal        — any structured memory matched. Strong signal.
        has_turn_signal          — any raw turn matched above a small score.
                                   Weak signal — turns are noisy.
        has_entity_match         — a named entity from the query exists in the
                                   user's knowledge graph. Very strong signal.

    Rules:
        entity_match            → both tiers open (concrete + relevant).
        memory_signal + profile → both tiers open.
        memory_signal           → tier 2 only (user wasn't the subject, but
                                  we found a structured hit).
        is_open_ended_about_user→ tier 1 only (general "remember me" dump).
        turn_signal             → tier 2 only (provenance, no profile dump).
        nothing                 → both closed → empty context (I3).

    Notably, `profile_relevant` *alone* never opens Tier 1: a query that
    mentions the user but is otherwise specific (e.g. "what does this user
    think about TypeScript?") must clear a real signal before we dump
    anything. Otherwise we'd inject unrelated profile facts as filler.
    """
    # Entity match is strong evidence FOR Tier 2 unconditionally. Tier 1 still
    # requires profile_relevant — "tell me about Biscuit" mentions a named
    # entity but isn't asking about its owner's job/location, so we don't dump
    # the owner's profile.
    if has_entity_match:
        return GateState(tier1_open=profile_relevant, tier2_open=True)
    if has_memory_signal and profile_relevant:
        return GateState(tier1_open=True, tier2_open=True)
    if has_memory_signal:
        return GateState(tier1_open=False, tier2_open=True)
    if is_open_ended_about_user:
        return GateState(tier1_open=True, tier2_open=False)
    if has_turn_signal:
        return GateState(tier1_open=False, tier2_open=True)
    return GateState(tier1_open=False, tier2_open=False)


def _format_stable_fact(row: dict, *, render_arc: bool = False) -> str:
    """Format one Tier 1 line.

    If `render_arc` is True AND a prior superseded memory exists, append
    "(previously: <prior_value>, until <date>)" instead of the legacy
    "(updated <date>)" suffix. This holds across all memory types — facts
    (Lives in Munich (previously Berlin)), preferences, and opinions — so the
    Tier 1 dump conveys "current state + last delta", which is the most useful
    context for the agent under exploratory / factoid_about_user intents.
    """
    val = (row.get("value") or "").strip()
    upd: datetime | None = row.get("updated_at")
    prior = row.get("prior") if render_arc else None

    if prior:
        prior_val = (prior.get("value") or "").strip()
        until_date = ""
        if prior.get("updated_at"):
            try:
                until_date = f", until {prior['updated_at'].date().isoformat()}"
            except AttributeError:
                pass
        if prior_val:
            return f"- {val} (previously: {prior_val}{until_date})"

    if upd:
        return f"- {val} (updated {upd.date().isoformat()})"
    return f"- {val}"


def _stable_fact_citation(row: dict, line: str) -> Citation | None:
    """Build a Citation pointing at the turn the fact was extracted from."""
    src = row.get("source_turn")
    if not src:
        return None
    snippet = line[2:] if line.startswith("- ") else line
    return Citation(turn_id=str(src), score=float(row.get("confidence", 0.0)), snippet=snippet)


def _format_memory_candidate(c: Candidate, *, render_arc: bool = False) -> str:
    """Format one Tier 2 line.

    v0.11: when the candidate's metadata carries a `prior` chain entry AND
    render_arc is True, surface it with "(previously: Y, until DATE)" just
    like Tier 1 does. Keeps the rendering symmetric across tiers — a memory
    that ends up in Tier 2 (because retrievers ranked it ahead of stable
    facts, or because it's not in the stable-facts set) gets the same
    historical context the agent would have seen in Tier 1.
    """
    val = c.content.strip()
    if render_arc:
        prior = (c.metadata or {}).get("prior")
        if prior:
            prior_val = (prior.get("value") or "").strip()
            until_date = ""
            upd = prior.get("updated_at")
            if upd:
                try:
                    until_date = f", until {upd.date().isoformat()}"
                except AttributeError:
                    pass
            if prior_val:
                return f"- {val} (previously: {prior_val}{until_date})"
    return f"- {val}"


def _format_turn_candidate(c: Candidate) -> str:
    snippet = c.content.strip().replace("\n", " ")
    snippet = trim_to_tokens(snippet, 160)
    if c.timestamp:
        try:
            d = c.timestamp.date().isoformat()
            return f"- [{d}] {snippet}"
        except AttributeError:
            pass
    return f"- {snippet}"


def assemble(
    *,
    stable_facts: list[dict],
    memory_candidates: list[Candidate],
    turn_candidates: list[Candidate],
    gate: GateState,
    budget: TierBudget,
    render_arc: bool = False,
) -> tuple[str, list[Citation]]:
    """Assemble the final context blob.

    `render_arc` — when True, opinions/preferences with a superseded prior are
    rendered as "currently X (previously Y, until DATE)". Should be gated on
    the analyzer's intent in the caller (exploratory / factoid_about_user)
    so simple factoid queries don't get bloated arc tails.
    """
    if not gate.tier1_open and not gate.tier2_open:
        return "", []

    sections: list[str] = []
    citations: list[Citation] = []
    used = 0

    def _fit_lines(
        header: str,
        items: Iterable[str],
        soft_cap: int,
        citation_pairs: list[tuple[Candidate, str]] | None = None,
    ) -> None:
        """Append a section, respecting (used + soft_cap) absolute ceiling and the global cap."""
        nonlocal used
        absolute_cap = min(used + soft_cap, budget.max_tokens)
        header_tokens = approx_token_count(header) + 1
        local: list[str] = []
        per_item_used = 0

        items_list = list(items)
        if not items_list:
            return

        # Reserve header tokens for the first line.
        for i, line in enumerate(items_list):
            line_tokens = approx_token_count(line) + 1
            overhead = header_tokens if i == 0 and not local else 0
            if used + overhead + per_item_used + line_tokens > absolute_cap:
                break
            local.append(line)
            per_item_used += line_tokens
            used += line_tokens + overhead
            if citation_pairs:
                cand, snippet = citation_pairs[i]
                # Both turn and memory candidates can produce a citation —
                # for memories we use source_turn (the turn the memory was
                # extracted from), so the consumer can always click through
                # to a real turn id. Memory citations make Tier-2-only
                # responses traceable, which they otherwise wouldn't be.
                citation_id = (
                    cand.id if cand.kind == "turn"
                    else (cand.source_turn or cand.id)
                )
                citations.append(
                    Citation(turn_id=citation_id, score=round(cand.score, 4), snippet=snippet)
                )

        if local:
            sections.append(header + "\n".join(local))

    # --- Tier 1: stable facts about the user
    if gate.tier1_open and stable_facts:
        lines = [_format_stable_fact(f, render_arc=render_arc) for f in stable_facts]
        absolute_cap = min(used + budget.tier1, budget.max_tokens)
        header = "## Known facts about this user\n"
        header_tokens = approx_token_count(header) + 1
        local: list[str] = []
        for f, line in zip(stable_facts, lines, strict=True):
            line_tokens = approx_token_count(line) + 1
            overhead = header_tokens if not local else 0
            if used + overhead + line_tokens > absolute_cap:
                break
            local.append(line)
            used += line_tokens + overhead
            cit = _stable_fact_citation(f, line)
            if cit is not None:
                citations.append(cit)
        if local:
            sections.append(header + "\n".join(local))

    # --- Tier 2: query-relevant memory candidates
    if gate.tier2_open and memory_candidates:
        # Cheap dedupe vs Tier 1: skip candidates whose value is already there verbatim.
        tier1_values = set()
        if gate.tier1_open and stable_facts:
            tier1_values = {(f.get("value") or "").strip() for f in stable_facts}
        kept = [c for c in memory_candidates if c.content.strip() not in tier1_values]
        if kept:
            lines = [_format_memory_candidate(c, render_arc=render_arc) for c in kept]
            pairs = [(c, c.content.strip()) for c in kept]
            _fit_lines("## Relevant memories\n", lines, budget.tier2, citation_pairs=pairs)

    # --- Tier 3: turn snippets — whatever budget remains
    if gate.tier2_open and turn_candidates:
        lines = [_format_turn_candidate(c) for c in turn_candidates]
        pairs = [(c, c.content.strip()) for c in turn_candidates]
        # Soft cap = remaining budget; no upper pct.
        remaining = max(0, budget.max_tokens - used)
        _fit_lines("## From recent conversations\n", lines, remaining, citation_pairs=pairs)

    if not sections:
        return "", []
    return "\n\n".join(sections), citations
