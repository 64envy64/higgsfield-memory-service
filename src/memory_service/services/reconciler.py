"""Reconciler: decides supersession when a new candidate arrives.

Policy (no LLM required; works in lexical-only mode):

  multiplicity=one  (e.g. employer, lives_in)
    - If an existing active memory has the same key and a different object →
      mark it `active=false` and insert the new one with `supersedes = old.id`.
    - If the same key+object already exists active → INSERT a refresh row with
      same object (so we have a touched timestamp) but mark the prior inactive
      and chain via supersedes. (Could be a no-op; we choose to record for
      provenance and let `recompute_active` keep one active per key.)

  multiplicity=many (e.g. owns_pet, allergic_to, likes)
    - Coexist. Same key+object → skip (no-op, idempotent).
    - Different object → INSERT, no supersession.
    - Exception: if the candidate's raw_quote contains a correction marker
      ("actually", "sorry", "correction", "I meant", "not X — Y") AND there's
      a prior active memory with the same key that the correction targets,
      supersede the targeted one.

Concurrency: `pg_advisory_xact_lock(hash(scope || key))` serializes concurrent
reconciles on the same fact. Different facts (or different scopes) do not block.

After 201 returns from /turns, every supersession in this batch is visible to
/recall — the entire reconcile + insert happens inside the same transaction
that committed the raw turn (I2).
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

import asyncpg

from memory_service.extraction.models import MemoryCandidate
from memory_service.extraction.taxonomy import spec_for

logger = logging.getLogger(__name__)


_CORRECTION_MARKERS = re.compile(
    r"\b(actually|sorry|correction|i\s+meant|never\s+mind|scratch\s+that|"
    r"that\s+was\s+wrong|let\s+me\s+correct)\b",
    re.IGNORECASE,
)


@dataclass
class ReconcileDecision:
    insert: bool                                  # do we INSERT the new candidate at all?
    active: bool                                  # should the new row be active=true?
    supersedes: uuid.UUID | None                  # id of the row this one supersedes
    deactivate_ids: list[uuid.UUID]               # ids to mark active=false (others sharing key)


async def reconcile(
    conn: asyncpg.Connection,
    *,
    scope_type: str,
    scope_id: str,
    candidate: MemoryCandidate,
) -> ReconcileDecision:
    """Decide what to do with a single new candidate against current state.

    Caller must already be inside a transaction and hold the advisory lock
    for (scope, key) — use `acquire_lock` first.
    """
    key = candidate.key()
    spec = spec_for(candidate.predicate)

    existing = await conn.fetch(
        """
        SELECT id, object, value, raw_quote, active, confidence
        FROM memories
        WHERE scope_type = $1 AND scope_id = $2 AND key = $3
        ORDER BY created_at DESC
        """,
        scope_type, scope_id, key,
    )
    active_rows = [r for r in existing if r["active"]]

    new_obj_norm = candidate.object.strip().lower()
    is_correction = bool(_CORRECTION_MARKERS.search(candidate.raw_quote or ""))

    # ----------------------------------------- multiplicity=one
    if spec.multiplicity == "one":
        # If an active row exists with the same object — nothing meaningful changed.
        if any(r["object"].strip().lower() == new_obj_norm for r in active_rows):
            return ReconcileDecision(insert=False, active=False, supersedes=None, deactivate_ids=[])

        # Otherwise this candidate replaces whatever was active before.
        prior = active_rows[0] if active_rows else None
        return ReconcileDecision(
            insert=True,
            active=True,
            supersedes=prior["id"] if prior else None,
            deactivate_ids=[r["id"] for r in active_rows],
        )

    # ----------------------------------------- multiplicity=many
    # Idempotent: exact-object active already exists → skip.
    if any(r["object"].strip().lower() == new_obj_norm for r in active_rows):
        return ReconcileDecision(insert=False, active=False, supersedes=None, deactivate_ids=[])

    if is_correction:
        # Heuristic: if the candidate's raw_quote mentions any active row's object,
        # interpret as "correcting that prior one"; supersede it specifically.
        targets: list[asyncpg.Record] = []
        for r in active_rows:
            obj = r["object"].strip()
            if obj and obj.lower() in (candidate.raw_quote or "").lower():
                targets.append(r)
        if targets:
            return ReconcileDecision(
                insert=True,
                active=True,
                supersedes=targets[0]["id"],
                deactivate_ids=[t["id"] for t in targets],
            )
        # No specific target — supersede the most-recent active (best heuristic).
        if active_rows:
            return ReconcileDecision(
                insert=True,
                active=True,
                supersedes=active_rows[0]["id"],
                deactivate_ids=[active_rows[0]["id"]],
            )

    # Default for `many`: coexist, no supersession.
    return ReconcileDecision(
        insert=True, active=True, supersedes=None, deactivate_ids=[]
    )


async def acquire_lock(conn: asyncpg.Connection, *, scope_type: str, scope_id: str, key: str) -> None:
    """Per-(scope,key) advisory lock. Auto-released at transaction end."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"{scope_type}:{scope_id}:{key}",
    )


async def apply_decision(
    conn: asyncpg.Connection,
    decision: ReconcileDecision,
) -> None:
    """Mark prior rows inactive per the decision. The new INSERT is done by the caller."""
    if decision.deactivate_ids:
        await conn.execute(
            "UPDATE memories SET active = false WHERE id = ANY($1::uuid[])",
            decision.deactivate_ids,
        )
