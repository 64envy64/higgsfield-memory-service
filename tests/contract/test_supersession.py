"""Supersession contract tests (v0.5).

These exercise the reconciler / recall pipeline end-to-end:
 - A new fact about the same key replaces the previous active one.
 - /users/{id}/memories returns the full chain (active + superseded).
 - /recall surfaces only the current fact, not the stale one.
"""
from __future__ import annotations

import pytest


async def _post_turn(client, user_id: str, session_id: str, text: str, ts: str) -> str:
    r = await client.post(
        "/turns",
        json={
            "session_id": session_id,
            "user_id": user_id,
            "messages": [
                {"role": "user", "content": text},
                {"role": "assistant", "content": "Noted."},
            ],
            "timestamp": ts,
            "metadata": {},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_employer_supersession(client, rand_user) -> None:
    await client.delete(f"/users/{rand_user}")

    await _post_turn(
        client, rand_user, "s1", "I work at Stripe.", "2025-01-01T00:00:00Z"
    )
    await _post_turn(
        client, rand_user, "s2", "I joined Notion last month.", "2025-02-01T00:00:00Z"
    )

    r = await client.get(f"/users/{rand_user}/memories")
    assert r.status_code == 200
    mems = r.json()["memories"]
    employer_mems = [m for m in mems if m["predicate"] == "employer"]
    assert len(employer_mems) >= 2, f"expected ≥2 employer memories, got {employer_mems}"

    active = [m for m in employer_mems if m["active"]]
    inactive = [m for m in employer_mems if not m["active"]]
    assert len(active) == 1, f"expected exactly 1 active employer, got {active}"
    assert active[0]["object"].lower() == "notion"
    assert len(inactive) >= 1
    # The new active row points back at a superseded row.
    assert active[0]["supersedes"] is not None

    # Recall should mention Notion, not Stripe (as current).
    r = await client.post(
        "/recall",
        json={
            "query": "Where does the user work right now?",
            "session_id": "probe",
            "user_id": rand_user,
            "max_tokens": 256,
        },
    )
    ctx = r.json()["context"].lower()
    assert "notion" in ctx
    # The Known-facts section must NOT present Stripe as a current employer.
    # Two valid shapes:
    #   1. Stripe doesn't appear at all (older assembler behavior).
    #   2. Stripe appears only inside an arc parenthetical:
    #         "- works at notion (previously: works at stripe, until …)"
    #      which is v0.10 arc surfacing — the prior is *labeled* as historical.
    # What we reject: "- works at stripe" on its own line as a current fact.
    known_section = ctx.split("## from recent conversations")[0].split("## relevant memories")[0]
    if "stripe" in known_section:
        # Strip out everything inside any "(previously: …)" parenthetical.
        import re as _re
        stripped = _re.sub(r"\(previously:[^)]*\)", "", known_section)
        assert "stripe" not in stripped, (
            f"stripe leaked into known facts as a *current* fact:\n{known_section}"
        )

    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_many_multiplicity_coexists(client, rand_user) -> None:
    """Pets are `many` — adding a second pet doesn't supersede the first."""
    await client.delete(f"/users/{rand_user}")

    await _post_turn(
        client, rand_user, "s1", "My dog Biscuit is the best.", "2025-01-01T00:00:00Z"
    )
    await _post_turn(
        client, rand_user, "s2", "My cat Mochi is also great.", "2025-01-02T00:00:00Z"
    )

    r = await client.get(f"/users/{rand_user}/memories")
    pet_mems = [m for m in r.json()["memories"] if m["predicate"] == "owns_pet"]
    actives = [m for m in pet_mems if m["active"]]
    objs = sorted(m["object"].lower() for m in actives)
    # Both pets should be active (multiplicity=many).
    assert "biscuit" in objs and "mochi" in objs, f"got {objs}"

    await client.delete(f"/users/{rand_user}")


@pytest.mark.asyncio
async def test_concurrent_writes_dont_split_active(client, rand_user) -> None:
    """Advisory-lock acceptance test: two simultaneous turns for the same fact
    must not both end up active. Whichever lands last wins, the other is superseded.
    """
    import asyncio

    await client.delete(f"/users/{rand_user}")

    async def post(city: str, ts: str, sess: str) -> None:
        await _post_turn(client, rand_user, sess, f"I moved to {city}.", ts)

    # Two concurrent /turns hitting the same key.
    await asyncio.gather(
        post("Berlin", "2025-03-01T00:00:00Z", "c1"),
        post("Lisbon", "2025-03-01T00:00:01Z", "c2"),
    )

    r = await client.get(f"/users/{rand_user}/memories")
    location_mems = [m for m in r.json()["memories"] if m["predicate"] == "lives_in"]
    actives = [m for m in location_mems if m["active"]]
    assert len(actives) == 1, f"split-brain: more than one active location: {actives}"

    await client.delete(f"/users/{rand_user}")
