"""Extraction quality self-eval.

After ingesting the fixture, query /users/{id}/memories for each user and
measure how many of the fixture's expected facts surface in the structured
memories. This is the v0.3 number; recall is still scored separately by
test_recall_quality.

A fact is considered extracted if any active or historical memory's `value`,
`object`, or `raw_quote` contains the expected substring (case-insensitive).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"
CONVERSATIONS = json.loads((FIXTURE_DIR / "conversations.json").read_text(encoding="utf-8"))
PROBES = json.loads((FIXTURE_DIR / "probes.json").read_text(encoding="utf-8"))


@pytest.fixture
async def ingested(client):
    user_ids = sorted({c["user_id"] for c in CONVERSATIONS})
    for u in user_ids:
        await client.delete(f"/users/{u}")
    for c in CONVERSATIONS:
        r = await client.post("/turns", json={
            "session_id": c["session_id"],
            "user_id": c["user_id"],
            "messages": c["messages"],
            "timestamp": c["timestamp"],
            "metadata": c.get("metadata", {}),
        })
        assert r.status_code == 201, r.text
    try:
        yield client
    finally:
        for u in user_ids:
            await client.delete(f"/users/{u}")


def _memory_haystack(memories: list[dict]) -> str:
    parts: list[str] = []
    for m in memories:
        parts.append(str(m.get("value") or ""))
        parts.append(str(m.get("object") or ""))
        parts.append(str(m.get("raw_quote") or ""))
        parts.append(str(m.get("subject") or ""))
        parts.append(str(m.get("predicate") or ""))
    return "\n".join(parts).lower()


@pytest.mark.asyncio
async def test_extraction_quality_baseline(ingested) -> None:
    client = ingested

    # Collect memories per user.
    user_ids = sorted({c["user_id"] for c in CONVERSATIONS})
    haystacks: dict[str, str] = {}
    counts: dict[str, int] = {}
    sample_memories: dict[str, list[dict]] = {}
    for u in user_ids:
        r = await client.get(f"/users/{u}/memories")
        assert r.status_code == 200
        memories = r.json()["memories"]
        haystacks[u] = _memory_haystack(memories)
        counts[u] = len(memories)
        sample_memories[u] = memories[:8]

    report = {
        "total_probes": 0,
        "extraction_hits": 0,
        "per_category": {},
        "per_probe": [],
        "memory_counts": counts,
    }

    for p in PROBES:
        # Skip probes where there's no expected fact to extract (noise-resistance only).
        if not p.get("expected_any"):
            continue
        report["total_probes"] += 1
        cat = p.get("category", "uncategorized")
        report["per_category"].setdefault(cat, {"total": 0, "hits": 0})
        report["per_category"][cat]["total"] += 1

        hay = haystacks.get(p["user_id"], "")
        hit = any(s.lower() in hay for s in p["expected_any"])
        if hit:
            report["extraction_hits"] += 1
            report["per_category"][cat]["hits"] += 1
        report["per_probe"].append({
            "id": p["id"],
            "user_id": p["user_id"],
            "category": cat,
            "expected_any": p["expected_any"],
            "hit": hit,
        })

    print("\n===== EXTRACTION QUALITY REPORT =====")
    print(json.dumps(report, indent=2))
    print("\n--- sample memories per user (first 8) ---")
    for u, mems in sample_memories.items():
        print(f"\n[{u}] ({counts[u]} total)")
        for m in mems:
            print(f"  - {m.get('type')}/{m.get('predicate')} :: {m.get('value')!r} (active={m.get('active')}, conf={m.get('confidence')})")

    # Like the recall test, we don't gate on a threshold here — we report.
    assert report["total_probes"] > 0
