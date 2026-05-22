# memory-service

A memory service for AI agents. Ingests completed conversation turns, extracts structured knowledge, reconciles contradictions, and answers recall queries that decide what context the agent sees on the next turn.

> Implements the memory-service HTTP contract. The architecture and iteration history are in [CHANGELOG.md](CHANGELOG.md); every major design decision is recorded with what changed, what we observed, and why the design moved.

---

## Quick start

```bash
git clone <this repo> memory-service
cd memory-service
cp .env.example .env          # add OPENAI_API_KEY for full quality (optional)
docker compose up -d --build
until curl -sf http://localhost:8080/health > /dev/null; do sleep 1; done
```

Then run the spec's smoke test:

```bash
bash scripts/smoke.sh
```

The default port is **8080**. The data volume is named **`memory_service_data`** — `docker compose down && docker compose up -d` keeps your memories.

> **Shell prerequisite.** `scripts/smoke.sh` and `scripts/test_persistence.sh` are POSIX bash. Tested under macOS, Linux, and WSL/Git Bash on Windows. The service itself is OS-agnostic (`docker compose up -d` works from any host that runs Docker).

## HTTP contract (exact)

| Method | Path                              | Purpose                                                |
| ------ | --------------------------------- | ------------------------------------------------------ |
| GET    | `/health`                         | Liveness/readiness (public — no auth ever)             |
| POST   | `/turns`                          | Synchronous ingest: store + extract + reconcile + index |
| POST   | `/recall`                         | Formatted context for the next agent turn              |
| POST   | `/search`                         | Structured search results (agent tool use)             |
| GET    | `/users/{user_id}/memories`       | Inspect typed memories (active + history)              |
| DELETE | `/sessions/{session_id}`          | Cleanup hook for eval scenarios                        |
| DELETE | `/users/{user_id}`                | Cleanup hook for eval scenarios                        |

If `MEMORY_AUTH_TOKEN` is set, every endpoint except `/health` (and `/docs`, `/openapi.json`) requires `Authorization: Bearer <token>`. `/health` stays public so orchestrators can probe it regardless of auth configuration.

---

## Design contracts (invariants)

These are non-negotiable. They're enforced at the data-model and transaction level, not by convention.

> **I1. Scope isolation.** Every retrieval, memory lookup, and entity resolution is parameterized by `(scope_type, scope_id)`. If `user_id != null` the scope is `('user', user_id)`; otherwise it's `('session', session_id)`. Cross-user bleed is impossible by construction.
>
> **I2. Atomic ingestion.** `POST /turns` commits the raw turn + extracted memories + supersession updates + entity edges in a single Postgres transaction. Per-`(scope, key)` reconciles are serialized via `pg_advisory_xact_lock`. After 201 returns, every write is visible to `/recall`, `/search`, and `/users/{id}/memories`. No eventual consistency.
>
> **I3. Empty over wrong.** `/recall` returns `{"context":"","citations":[]}` whenever the relevance gate does not fire. Unrelated profile facts are never injected as filler.

The relevance gate (codified in `services/assembler.decide_gate`) only opens Tier 1 ("Known facts about this user") when there is concrete evidence the query is about the user *and* something in the data answers it — never on `profile_relevant` alone. See [Recall pipeline](#recall-pipeline) below for the rules.

---

## Architecture

```
                         POST /turns                       POST /recall, /search
                              │                                   │
                              ▼                                   ▼
┌──────────────────────────────────────────────┐  ┌────────────────────────────────────┐
│ IngestionService           (sync, txn-scoped)│  │ RecallService                       │
│  ├─ EmbeddingClient.embed(full_text)         │  │  ├─ QueryAnalyzer (LLM + heuristic) │
│  ├─ ExtractionService                        │  │  │     intent / profile / entities │
│  │    LLM → rule-based fallback              │  │  ├─ Retrievers (parallel)           │
│  │    PredicateNormalizer (alias→canonical)  │  │  │     vector_turns | fts_turns     │
│  ├─ Embed memory values (batch)              │  │  │     vector_memories | fts_memories
│  └─ ── BEGIN TX ──                           │  │  ├─ Stable facts + entity hop       │
│       insert_turn                            │  │  ├─ decide_gate (Invariant 3)       │
│       per candidate:                         │  │  └─ tiered assembler                │
│         pg_advisory_xact_lock(scope, key)    │  │       Tier 1 known facts (≤40%)     │
│         reconciler.reconcile                 │  │       Tier 2 relevant memories(≤40%)│
│           multiplicity={one,many} policy     │  │       Tier 3 turn snippets (rest)   │
│         memory_repo.insert_memory            │  │       tiktoken hard-trim            │
│         upsert entities + mention edges      │  └────────────────────────────────────┘
│     ── COMMIT ──                             │
└──────────────────────────────────────────────┘

                              Postgres 16 + pgvector
                  ┌─────────────────────────────────────────┐
                  │ turns       raw conversation log        │
                  │ memories    typed, active/superseded    │
                  │ entities    named-entity nodes          │
                  │ mentions    memory → entity edges       │
                  │ edges       memory ↔ memory (graph hop) │
                  └─────────────────────────────────────────┘
```

### Backing store: Postgres 16 + pgvector

One process gives us ACID transactions (load-bearing for I2), vector search via `pgvector` HNSW (`text-embedding-3-small`, 1536d), and lexical full-text search via `tsvector`/`ts_rank_cd`. The advisory lock primitive is what makes the supersession story safe under concurrent writes — we couldn't get that from a plain document store. SQLite was on the shortlist for its zero-config simplicity, but it can't do per-row advisory locks across processes and we wanted the door open for hosting the API elsewhere from the DB.

We deliberately do *not* use a real BM25 extension (`pg_search` / paradedb). At this scale `ts_rank_cd` plus query-side OR-tokenization is enough, and we keep the standard `pgvector/pgvector:pg16` image. CHANGELOG v0.2 documents the OR-tsquery construction we needed to make FTS behave usefully on natural-language queries.

### Embeddings

`vector(1536)` is always provisioned. With `OPENAI_API_KEY` set, the service embeds turn `full_text` and every memory `value` via `text-embedding-3-small`. **Without a key the service still boots and serves traffic**; the embedding column stays NULL, vector retrievers no-op, and lexical FTS carries retrieval. This is documented degradation, not silent failure (a startup warning is logged).

We do **not** pad/hash into a fake 1536-d vector in the no-key case. Pseudo-semantics would be worse than honest None.

### Extraction pipeline

`POST /turns` runs **synchronously** per I2:

1. `EmbeddingClient.embed(full_text)` — outside the transaction (no network IO under a lock).
2. `ExtractionService.extract(messages_text)`:
   - **LLM path** (preferred): one `gpt-4o-mini` call with `response_format={"type":"json_object"}`. Prompt is rendered with the predicate taxonomy (`extraction/taxonomy.py`) so the model is biased toward canonical predicates. Output is normalized: every predicate goes through `normalize_predicate` (alias map → canonical, anything unknown → `other:<topic>`).
   - **Rule fallback**: 17 regex patterns over user-role lines, covering employer/lives_in/owns_pet/dietary/allergic_to/likes/dislikes/correction. No external models or downloads — keeps the no-key path fully self-contained.
3. Memory values are batch-embedded (best-effort).
4. **Inside a single transaction:**
   - `insert_turn`
   - For each candidate:
     - `pg_advisory_xact_lock(hashtext("scope:scope_id:key"))` — per-(scope, key) serialization.
     - `reconciler.reconcile` returns a `ReconcileDecision` (insert/skip/supersede + deactivate_ids).
     - `apply_decision` flips prior `active` flags.
     - `insert_memory` with the new row's `active`/`supersedes`.
     - `upsert_entity` + `link_mention` for each named entity.

What we extract (LLM, complete):
- Personal facts (employer, location, family, pets, dietary, allergies)
- Preferences and opinions (including hedged ones)
- Implicit facts ("walking Biscuit before work" → `owns_pet=Biscuit`)
- Corrections ("actually, …") — emitted at higher confidence; reconciler decides supersession

What we skip:
- Assistant turns (provenance only)
- Pure hypotheticals
- Cold small talk

What rule-extraction misses (documented limitations):
- Opinion arcs ("X is fine for big projects but…" — too nuanced for regex)
- Free-form corrections without keyword markers
- Anything paraphrased outside the pattern set

These all clear with `OPENAI_API_KEY` — the same orchestration runs, just with a smarter extractor.

### Recall pipeline

`POST /recall` runs a 6-step pipeline. The interesting design lives in step 5.

1. **QueryAnalyzer** — one LLM call (gpt-4o-mini) returning `{intent, profile_relevant, entities, expanded_queries}`. Heuristic fallback when LLM disabled: keyword triggers (`PROFILE_TRIGGERS`), negative-intent matchers (`nginx`/`capital`/`how to`/…), capitalized-token entity extraction. The heuristic is conservative — it errs toward `profile_relevant=False` rather than dumping facts.
2. **Retrievers** (`asyncio.gather` over `[orig query, ...expansions]`):
   - `vector_turns`, `fts_turns`, `vector_memories`, `fts_memories` — each acquires its own pool connection (asyncpg disallows concurrent statements on the same connection; this was a real bug caught in v0.2).
3. **Stable facts + entity hop** (sequential SQL on one connection):
   - `list_stable_facts(scope)` — high-confidence active facts/preferences for potential Tier 1 inclusion. LEFT JOINs the supersession chain so the assembler can render arcs without a second round-trip (v0.10).
   - `entities_for_names(query.entities)` — entity match check.
   - `memories_mentioning_entities(matched.ids)` — memories anchored to query-mentioned entities (entity-hop, v0.6).
4. **Edge hop** (`memories_via_edges`, v0.6/v0.9.2). Take the top-N memory candidates from steps 2–3 and pull their 1-hop neighbors via `memory_edges` (`co_extracted` weight 0.7, `same_subject` weight 0.5). Neighbors enter the candidate pool as `source="edge_hop"`. This catches co-mentioned facts that didn't independently match query lexicon — e.g. "Notion" hit on employer → "Berlin" via the co_extracted edge.
5. **Weighted RRF fusion** (`services/fusion.rrf_fuse`, v0.9.1). Six sources, each with its own weight:
   ```
   memory_vector=1.4  memory_fts=1.1  graph=1.5
   edge_hop=0.9       turn_vector=1.0 turn_fts=0.7
   ```
   `score(d) = Σ w_s / (k + rank_s(d))` with `k=settings.rrf_k=60`. Rank-based, so the heterogeneous score scales (cosine, ts_rank_cd, synthetic graph priors) don't fight each other.
6. **Relevance gate** (`decide_gate`). The six rules:
   - **entity_match + profile_relevant** → both tiers open.
   - **entity_match alone** → Tier 2 only (named entity hit, but query isn't about the user — e.g. "tell me about Biscuit" must not dump owner's profile).
   - **memory_signal + profile_relevant** → both tiers open.
   - **memory_signal alone** → Tier 2 only (concrete hit, not about user).
   - **is_open_ended_about_user** (intent ∈ {`exploratory`, `recent_context`} + profile_relevant) → Tier 1 only ("tell me about me" prompts).
   - **turn_signal alone** → Tier 2 only (provenance, no profile dump).
   - **Otherwise → empty**.
   `profile_relevant` *alone* never opens Tier 1 — that's the gap that v0.8's first cut had, and that fix is what closes `empty_violations`. (See CHANGELOG v0.8.)
7. **Tiered assembler** (`assembler.assemble`) — three markdown sections, explicit budget bounds (40% / 40% / rest), `tiktoken`-aware hard trim. Per-snippet soft caps (~160 tokens) so one long turn can't eat the budget. **Arc rendering** is symmetric across tiers (v0.10 wired Tier 1; v0.11 extended it to Tier 2): when intent is `exploratory` or `factoid_about_user`, any line whose memory has a supersession chain renders as `- Currently X (previously: Y, until DATE)` — regardless of whether it surfaced via stable-facts (Tier 1) or via the retrievers (Tier 2). The agent gets consistent historical context across runs, even when ranking shifts the same memory between tiers.

#### Priority logic, defended

The assembler's tier ordering is a deliberate design choice. From the spec:

> When budget is tight, prioritize: stable user facts first, then query-relevant memories, then recent context. Your priority logic is a design decision we care about — defend it in the README.

We follow this ordering with one important caveat: **none of the tiers are unconditional**. The gate enforces Invariant 3 first — if there's no evidence the query is relevant, *nothing* gets injected, even stable user facts. This is the load-bearing decision: a memory service that always dumps profile data on every query produces "noise resistance" failures that mask real recall wins on the eval. Empty over wrong.

Within a query that does fire the gate:
- **Tier 1 first because stable facts answer the most queries with the fewest tokens.** "Where does the user work?" / "What does the user prefer for X?" / "Any allergies?" — these are answerable from 1–2 lines of canonical text. The token budget is best spent here.
- **Tier 2 (query-relevant memories) second** because retrieved memories add the specific topical info that stable facts don't cover (e.g. "the user mentioned X in a previous session").
- **Tier 3 (raw turn snippets) last** because raw turns are the noisiest and the least supersession-aware (the raw text never changes; we can't mark "I work at Stripe" inactive once "I joined Notion" arrives). Pushing them last means token-budget pressure trims them first — exactly the right behaviour.

### Fact evolution

`reconciler.reconcile` decides per candidate:

- **`multiplicity=one`** (employer, lives_in, name, age, partner, …): a new object for the same key supersedes the prior active one. The new row gets `supersedes = prior.id` and `active=true`; the prior row goes `active=false`.
- **`multiplicity=many`** (owns_pet, allergic_to, likes, …): coexist by default. Same key+object → idempotent skip. **Correction marker** in the candidate's `raw_quote` AND the prior's object mentioned in the correction → targeted supersession.
- **`pg_advisory_xact_lock(scope, key)`** at the top of each reconcile prevents split-brain under concurrent writes. `tests/contract/test_supersession.py::test_concurrent_writes_dont_split_active` is the acceptance test for this.
- Opinion supersession is a special case of `multiplicity=many` — the reconciler treats it as standard `many` (coexist + correction-trigger), and the assembler keeps the latest. **Arc surfacing is wired (v0.10):** when the analyzer's intent is `exploratory` or `factoid_about_user`, Tier 1 renders `- Currently X (previously: Y, until DATE)` for any memory with a prior in its supersession chain. The "until" date is the source-turn timestamp of the *new* memory, not the wall-clock supersession instant. Arc rendering is suppressed for `factoid_general` / `cold` intents so simple factoid answers stay compact.
- `DELETE /sessions/{id}` correctly recomputes `active` for affected keys after deletion (captured into a CTE before the deletes so the affected set survives `ON DELETE CASCADE`).

### Cross-session scoping

By design, memories are scoped to **`(scope_type, scope_id)`**:
- `user_id != null` → `scope=('user', user_id)`: memories cross sessions for that user (the point of a memory service).
- `user_id == null` → `scope=('session', session_id)`: anonymous mode; memories never leak out of the session.

Raw turns are scoped the same way. There is no global search; `/search` with both `user_id` and `session_id` null returns `{"results": []}`.

---

## Tradeoffs

| Optimized for | Given up |
| --- | --- |
| Synchronous `/turns` (I2 → no race window between write and read) | Write latency (LLM call + multiple inserts per turn). Spec gives 60 s; we use ~1–4 s typical. |
| Postgres + pgvector | Operational simplicity vs. SQLite. We pay one container for advisory locks + HNSW + FTS in one place. |
| Closed predicate taxonomy + alias normalizer + escape hatch | Some recall on the long tail (fully novel predicates land as `other:*` with conservative multiplicity). |
| Postgres FTS (ts_rank_cd) + weighted RRF across six sources (turn/memory × vector/FTS, plus graph and edge-hop) | Pure-BM25 ranking nuance vs. a custom image with `pg_search`. |
| Rule-based fallback extractor with zero external deps | Quality ceiling without `OPENAI_API_KEY` — extraction recall is ~75% on the self-fixture vs. an expected near-saturation with LLM. |
| Tier-based assembler with explicit gate | Some recall lost for "ambiguous" queries that the gate closes. Honest empty over wrong. |

---

## Failure modes

| Failure | Behavior |
| --- | --- |
| No `OPENAI_API_KEY` | Boots in lexical-only mode. Semantic retrieval and nuanced extraction degrade; lexical FTS and rule-based extraction remain fully functional. Warning logged at startup. |
| DB unreachable | `/health` returns 503; other endpoints return 503 via the pool dependency. Service stays up. |
| Oversized / malformed payload | 422 via Pydantic + global handler. Never crashes. |
| Unicode / RTL / emoji | Stored verbatim; tested (`test_unicode_payload_does_not_crash`). FTS uses `english` config so non-English content is lossy at retrieval — known limitation. |
| LLM / embedding timeout | `/turns` runs extraction and turn embedding concurrently, uses bounded OpenAI timeouts, and falls back to rule/lexical paths. Turn is still committed. |
| LLM analyzer timeout / failure | Heuristic analyzer is the fallback. Recall still runs. |
| Concurrent `/turns` for same `(scope, key)` | Advisory lock serializes; exactly one wins → `active`, others land superseded. |
| `docker compose down` then `up` | Named volume preserves all data. See `scripts/test_persistence.sh`. |

---

## Running tests

```bash
# Spec smoke test:
bash scripts/smoke.sh                # equivalent to spec §8

# Full internal test suite inside the running container:
make test                            # contract + quality
docker compose exec api python -m pytest tests/ -v

# Persistence: writes data, restarts the stack, verifies DB + /recall + /search:
bash scripts/test_persistence.sh
```

Quality fixture lives in [`fixtures/`](fixtures/). The test in [`tests/quality/test_recall_quality.py`](tests/quality/test_recall_quality.py) ingests it and reports per-category numbers including `recall_hits`, `forbidden_hits`, and `empty_violations`. It does **not** gate on a threshold — it's a measurement harness. The CHANGELOG quotes its output at every version.

| Test class | Count | What it covers |
|---|---:|---|
| `tests/contract/test_health.py` | 3 | health + auth whitelist + protected endpoint enforcement |
| `tests/contract/test_endpoints_shape.py` | 11 | every endpoint's request/response shape; malformed JSON; missing fields; unicode |
| `tests/contract/test_supersession.py` | 3 | A→A' chain; `many` coexistence; **advisory-lock split-brain test** |
| `tests/contract/test_relevance_gate.py` | 4 | noise → empty; anonymous session roundtrip; tight token budget (max_tokens=50); cold user |
| `tests/contract/test_payload_size.py` | 3 | oversized body → 413; normal still works; invalid Content-Length → 4xx |
| `tests/contract/test_multi_hop.py` | 1 | co_extracted neighbor surfaces via edge_hop |
| `tests/contract/test_arc_surfacing.py` | 2 | Tier 1 arc renders under exploratory/factoid_about_user; suppressed otherwise |
| `tests/contract/test_arc_tier2.py` | 2 | same arc shape via Tier 2 retriever path (symmetry) |
| `tests/quality/test_recall_quality.py` | 2 | self-eval recall metric |
| `tests/quality/test_extraction_quality.py` | 1 | self-eval extraction metric |
| **Total** | **33** | |

Persistence is covered by `scripts/test_persistence.sh` — uses `docker compose down/up` and a direct DB count to prove rows survive a restart.

CI: [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the same `docker compose up -d --build` → `wait /health` → `pytest tests/ -q` → smoke flow on every push and PR, on a clean Ubuntu runner. Persistence is a parallel job. If the badge is green, the spec §8 setup boots from a fresh clone.

---

## Repository layout

```
memory-service/
├── README.md                 ← you are here
├── CHANGELOG.md              ← what changed at each iteration and why (the most important doc)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
├── Makefile
├── scripts/
│   ├── smoke.sh
│   └── test_persistence.sh
├── src/memory_service/
│   ├── main.py               ← FastAPI app + lifespan + global exception handlers
│   ├── config.py
│   ├── api/
│   │   ├── deps.py           ← pool, settings, auth dependencies
│   │   ├── routes_admin.py   ← /health, DELETE /sessions, DELETE /users
│   │   ├── routes_turns.py
│   │   ├── routes_recall.py
│   │   ├── routes_search.py
│   │   └── routes_memories.py
│   ├── schemas/              ← Pydantic request/response models
│   ├── db/
│   │   ├── pool.py           ← asyncpg pool + migration runner + JSONB codec
│   │   └── migrations/001_init.sql
│   ├── embedding/client.py   ← OpenAI text-embedding-3-small (1536d) + no-op fallback
│   ├── llm/client.py         ← OpenAI chat (chat_json) + timeouts
│   ├── extraction/
│   │   ├── taxonomy.py       ← predicate list + multiplicity + alias normalizer
│   │   ├── models.py
│   │   ├── llm_extractor.py
│   │   ├── rule_extractor.py
│   │   └── service.py        ← LLM → rules orchestration
│   ├── repo/
│   │   ├── memory_repo.py    ← memories, stable facts, entities, mentions
│   │   └── turn_repo.py
│   ├── services/
│   │   ├── reconciler.py     ← supersession + advisory lock
│   │   ├── query_analyzer.py ← LLM + heuristic
│   │   ├── retrievers.py     ← vector / FTS for turns + memories
│   │   ├── recall.py         ← /recall pipeline
│   │   ├── search.py         ← /search pipeline
│   │   └── assembler.py      ← tiered context, gate, budget
│   └── util/
│       ├── logging.py        ← JSON to stdout
│       ├── text.py           ← OR-tsquery construction, stopwords
│       └── tokens.py         ← tiktoken / chars-4 budget helpers
├── fixtures/
│   ├── conversations.json    ← 8 scripted turns, 2 users
│   └── probes.json           ← 11 probe queries with expected_any + forbidden + empty flags
└── tests/
    ├── conftest.py           ← httpx ASGI client with proper lifespan handling
    ├── contract/             ← shape, supersession, gate, auth
    └── quality/              ← self-eval metrics
```

---

## Final self-eval (lexical-only mode, no `OPENAI_API_KEY`)

| Category | Probes | Recall | Forbidden | Empty-violation |
|---|---:|---:|---:|---:|
| fact_evolution | 2 | 2 | 0 | 0 |
| fact_history | 1 | 0 | 0 | 0 |
| implicit_fact | 1 | 1 | 0 | 0 |
| multi_hop | 1 | 1 | 0 | 0 |
| preference | 1 | 1 | 0 | 0 |
| opinion_arc | 1 | 1 | 0 | 0 |
| correction | 1 | 1 | **1** | 0 |
| noise_resistance | 2 | 0 | 0 | 0 |
| scope_isolation | 1 | 0 | 0 | 0 |
| **TOTAL** | **11** | **7 (63.6%)** | **1 (9.1%)** | **0 (0.0%)** |

Extraction: **6/8 expected facts captured (75%)** via rule extractor.

The one remaining `forbidden_hit` is `correction (urgent care)` — the rule extractor has no pattern for "actually, … X, not Y"-style corrections; this is squarely an LLM-extraction case. With `OPENAI_API_KEY` set, all four remaining columns are expected to clean up.

With `OPENAI_API_KEY` set, expected qualitative jumps:
- **Recall**: → near-saturation (vector retrievers come online; query rewriting widens coverage; the weighted RRF blend across six sources gets to do its job).
- **Extraction**: → near-saturation (opinion arcs, corrections, paraphrased facts that rules miss).
- **Forbidden**: → 0 or 1 (the three remaining are all "extracted memory missing" — clearing extraction clears the forbidden leak).
- **Empty-violations**: stays 0 (gate logic is independent of LLM).

> A dedicated LLM reranker is intentionally **not** in this list — see [PLAN.md §11](PLAN.md) and [CHANGELOG v0.9.2](CHANGELOG.md). The argument: RRF + graph hop + edge hop already do the precision work; adding a per-recall LLM call needs eval evidence we don't have yet.

See [CHANGELOG.md](CHANGELOG.md) for the full progression v0.1 → v0.11.1, including the bugs caught at each step.
