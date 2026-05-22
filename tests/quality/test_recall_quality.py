"""Recall quality self-eval.

Runs the fixture conversations + probes through the live service.

Metric per probe:
  * If `expected_any` non-empty: recall = 1 iff any expected token appears in the
    returned context (case-insensitive substring).
  * If `forbidden` matches in context: precision_penalty +1 (counted separately).
  * If `expect_empty_context` is true: probe passes iff context is empty AND no
    citations are returned (noise resistance + scope isolation).

The aggregate score is intentionally reported rather than threshold-gated:
the fixture is the iteration loop, while deterministic contract tests pin the
must-not-regress behaviours such as supersession, isolation, budget, and arcs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"
CONVERSATIONS = json.loads((FIXTURE_DIR / "conversations.json").read_text(encoding="utf-8"))
PROBES = json.loads((FIXTURE_DIR / "probes.json").read_text(encoding="utf-8"))


@pytest.fixture
async def ingested(client):
    """Ingest the full fixture once per test; clean up users at the end."""
    user_ids = sorted({c["user_id"] for c in CONVERSATIONS})
    # Clean before, in case a previous run left data.
    for u in user_ids:
        await client.delete(f"/users/{u}")

    for c in CONVERSATIONS:
        payload = {
            "session_id": c["session_id"],
            "user_id": c["user_id"],
            "messages": c["messages"],
            "timestamp": c["timestamp"],
            "metadata": c.get("metadata", {}),
        }
        r = await client.post("/turns", json=payload)
        assert r.status_code == 201, f"ingest failed for {c['session_id']}: {r.text}"
    try:
        yield client
    finally:
        for u in user_ids:
            await client.delete(f"/users/{u}")


@pytest.mark.asyncio
async def test_recall_quality_baseline(ingested) -> None:
    client = ingested
    report = {
        "total": 0,
        "recall_hits": 0,
        "forbidden_hits": 0,
        "empty_violations": 0,
        "per_category": {},
        "per_probe": [],
    }

    for p in PROBES:
        report["total"] += 1
        cat = p.get("category", "uncategorized")
        report["per_category"].setdefault(cat, {"total": 0, "recall_hits": 0, "forbidden_hits": 0, "empty_violations": 0})
        report["per_category"][cat]["total"] += 1

        r = await client.post(
            "/recall",
            json={
                "query": p["query"],
                "session_id": p["session_id"],
                "user_id": p["user_id"],
                "max_tokens": 512,
            },
        )
        assert r.status_code == 200
        body = r.json()
        ctx = (body.get("context") or "").lower()
        cites = body.get("citations") or []

        recall_hit = False
        if p.get("expected_any"):
            recall_hit = any(s.lower() in ctx for s in p["expected_any"])
            if recall_hit:
                report["recall_hits"] += 1
                report["per_category"][cat]["recall_hits"] += 1

        forbidden_hit = False
        for f in p.get("forbidden", []):
            if f.lower() in ctx:
                forbidden_hit = True
                report["forbidden_hits"] += 1
                report["per_category"][cat]["forbidden_hits"] += 1
                break

        empty_violation = False
        if p.get("expect_empty_context"):
            if ctx.strip() != "" or cites:
                empty_violation = True
                report["empty_violations"] += 1
                report["per_category"][cat]["empty_violations"] += 1

        report["per_probe"].append({
            "id": p["id"],
            "category": cat,
            "recall_hit": recall_hit,
            "forbidden_hit": forbidden_hit,
            "empty_violation": empty_violation,
            "context_chars": len(body.get("context") or ""),
            "n_citations": len(cites),
        })

    # Print the report so it shows up in pytest -s logs.
    print("\n===== RECALL QUALITY REPORT =====")
    print(json.dumps(report, indent=2))

    # Also dump to a stable location so CI / CHANGELOG capture is easy.
    out = Path("/tmp/recall_quality_report.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Keep the quality fixture as a measurement harness; contract tests enforce
    # specific behaviours that must not regress.
    assert report["total"] == len(PROBES)


@pytest.mark.asyncio
async def test_users_memories_shape_after_ingest(ingested) -> None:
    """After ingest, /users/{id}/memories returns inspectable structured memories."""
    client = ingested
    r = await client.get("/users/fx-alice/memories")
    assert r.status_code == 200
    body = r.json()
    assert "memories" in body and isinstance(body["memories"], list)
    assert body["memories"], "fixture ingest should produce structured memories"


def test_fixture_well_formed() -> None:
    """Cheap structural check so a malformed fixture fails loudly instead of skewing scores."""
    for c in CONVERSATIONS:
        assert {"user_id", "session_id", "timestamp", "messages"} <= set(c)
        assert isinstance(c["messages"], list) and len(c["messages"]) >= 1
    for p in PROBES:
        assert {"id", "user_id", "session_id", "query", "category"} <= set(p)
        if not p.get("expect_empty_context"):
            assert p.get("expected_any") or p.get("forbidden"), p["id"]
