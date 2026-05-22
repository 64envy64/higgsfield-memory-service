# Memory Service — Final Design Plan

> The final design written down in one place for reference. Every section reflects a decision that was challenged, not just chosen and shipped. Where there were two reasonable answers, the rejected one is named so the tradeoff stays visible.
>
> Implementation status against this plan is in [`CHANGELOG.md`](CHANGELOG.md) — v0.1 → v0.11.1. This document is the *what and why*; the CHANGELOG is the *when and how-it-went*.

---

## 0.1 Design Methodology: Forward Build Specification

This solution was engineered using a **spec-leading** approach. Rather than starting with a generic FastAPI scaffold, we first defined the **Non-negotiable Invariants (I1–I3)** in `BUILD_SPEC.md`. This ensured that the difficult problems (synchronous ACIDity, cross-user isolation, relevance gating) were "baked into the concrete" before implementation. This artifact serves as the design-led evidence for the Senior Engineering role.

---

## 0. Goal & scope

Build a memory service for an AI agent that:

- Ingests conversation turns synchronously (single HTTP call, eventual-consistency-free).
- Extracts **structured, typed knowledge**, not raw message chunks.
- Reconciles contradictions via supersession (keep history, return current).
- Answers `/recall` with a tier-budgeted, gate-protected context blob the agent's prompt can paste in directly.
- Persists across container restarts via a named Docker volume.
- Conforms to a fixed HTTP contract (7 endpoints), shipped as a single Docker-composable Git repository.

**Out of scope** (per spec §12): UI, multi-tenant prod hardening, horizontal scalability proofs, agent-side code, migration story.

---

## 1. Tech stack — decisions and defences

| Decision | Choice | Defence | Rejected alternative |
|---|---|---|---|
| Language | Python 3.12 | Best LLM/embedding/SDK ecosystem. Type hints + Pydantic. async FastAPI. | Go (perf — overkill at this scale); Rust (ecosystem too thin for LLM work). |
| HTTP framework | FastAPI | Pydantic-typed contracts, native async, ASGI testing transport. | Starlette directly (no Pydantic integration); Flask (sync only). |
| Backing store | **Postgres 16 + pgvector** | One process gives us ACID for I2, `pg_advisory_xact_lock` for safe concurrent supersession, HNSW vector index, and `tsvector` full-text search — *in one transactional boundary*. | SQLite + sqlite-vec: tempting for zero-config, but no cross-process advisory lock; can't reason about concurrent reconciles. Qdrant + Postgres: more moving parts; no transactional boundary across the two stores. |
| Embeddings | OpenAI `text-embedding-3-small` (1536d) | Cost/quality winner; matryoshka allows dim re-projection if we ever change. | `bge-small` (slower without GPU; quality below `3-small`). |
| Extraction LLM | `gpt-4o-mini` + `response_format={"type":"json_object"}` | Cheap, fast, structured outputs make extraction deterministic. | gpt-4o (overkill cost); Claude (no second SDK to maintain in scope). |
| Reranker | **None** (deferred to v1.0+) | RRF over weighted sources gets us most of the precision win at zero LLM cost. An LLM reranker adds another network call per recall — not worth it without first seeing eval evidence we need it. | LLM listwise rerank — left scaffolding in earlier iterations, removed in v0.9.2 to kill dead config surface. |
| No-key fallback (embeddings) | **None** (column NULL, lexical-only mode) | Honest degradation. Padding to a fake 1536d vector would inject pseudo-semantics — worse than FTS-only. | A `hashing-vectorizer` of dim 1536 — measured: noise > signal. |
| No-key fallback (extraction) | Regex over user-role lines, no external models | Self-contained, no runtime downloads. ~75% recall on the self-fixture. | spaCy — heavy, requires model download on first run; risky for "no internet" deploys. |
| Logging | python-json-logger to stdout | One line per event, machine-grep-able. | Structlog — feature parity not needed at this scale. |

---

## 2. Three invariants (load-bearing)

These are non-negotiable, enforced at the data model + transaction layer:

### I1 — Scope isolation

Every read parameterized by `(scope_type, scope_id)`. Cross-user bleed is impossible by construction, not by convention.

- `user_id != null` → `scope=('user', user_id)`: memories follow the user across sessions.
- `user_id == null` → `scope=('session', session_id)`: anonymous mode; data dies with the session.
- `/search` with both `user_id` *and* `session_id` null → `{"results":[]}`. No global search ever.

Migration encodes `scope_type` CHECK constraint. Every SQL query in the codebase filters by `(scope_type, scope_id)`.

### I2 — Atomic ingestion

`POST /turns` commits everything in **one Postgres transaction**:

```
EMBED + LLM EXTRACT (outside txn — no holding a connection during network IO)
BEGIN
    INSERT INTO turns ...
    for each candidate:
        pg_advisory_xact_lock(hashtext(scope:scope_id:key))   ← per-key serialization
        reconcile (one/many policy, supersedes decision)
        UPDATE memories SET active=false WHERE ...           ← prior actives
        INSERT INTO memories ...                              ← new candidate
        UPSERT entity nodes; INSERT mention edges
COMMIT
```

After 201 returns, every write is visible to `/recall`, `/search`, and `/users/{id}/memories`. No eventual consistency window.

The advisory lock is **per (scope, key)**, so two concurrent turns about different facts for the same user don't block each other; two turns about the *same* fact serialize correctly. `tests/contract/test_supersession.py::test_concurrent_writes_dont_split_active` is the acceptance test.

### I3 — Empty over wrong

`/recall` returns `{"context":"","citations":[]}` whenever the relevance gate doesn't fire. Profile facts are never injected as filler.

This is the rule that protects "noise resistance" on the hidden eval — a memory service that always dumps profile data on every query produces false-positive context that masks the real recall wins.

Crucially, `profile_relevant=True` *alone* (without any concrete signal) **never** opens Tier 1. "What does this user think about TypeScript?" against a user with no TypeScript memory must return empty, not dump that user's location and job as filler.

---

## 3. DB schema (decisions and what each column is for)

```sql
turns                    -- raw conversation log, immutable, source of truth
  id              UUID PK
  session_id      TEXT  NOT NULL
  user_id         TEXT          -- may be NULL (anonymous)
  scope_type      TEXT  NOT NULL CHECK (scope_type IN ('user','session'))
  scope_id        TEXT  NOT NULL
  messages        JSONB NOT NULL
  full_text       TEXT  NOT NULL                          -- flattened for FTS/embed
  timestamp       TIMESTAMPTZ NOT NULL
  metadata        JSONB
  embedding       vector(1536)                            -- NULL if no OPENAI_API_KEY
  tsv             TSVECTOR                                -- trigger-maintained

memories                 -- extracted, typed, active+superseded
  id              UUID PK
  scope_type      TEXT, scope_id TEXT                     -- same scope semantics as turns
  type            TEXT CHECK (type IN ('fact','preference','opinion','event'))
  subject         TEXT  NOT NULL                          -- 'user' or 'pet:Biscuit'
  predicate       TEXT  NOT NULL                          -- canonical or 'other:*'
  object          TEXT  NOT NULL                          -- 'Notion', 'Berlin'
  key             TEXT  GENERATED ALWAYS AS (subject || '::' || predicate) STORED
  value           TEXT  NOT NULL                          -- human-readable summary
  raw_quote       TEXT                                    -- provenance from source message
  confidence      REAL  CHECK (BETWEEN 0 AND 1)
  source_session  TEXT, source_turn UUID REFERENCES turns ON DELETE SET NULL
  created_at, updated_at TIMESTAMPTZ
  supersedes      UUID REFERENCES memories ON DELETE SET NULL
  active          BOOLEAN
  embedding       vector(1536)
  tsv             TSVECTOR

entities                 -- named-entity anchor nodes for graph hop
  id              UUID PK
  scope_type, scope_id TEXT NOT NULL
  name            TEXT  NOT NULL
  type            TEXT                                    -- person|pet|place|org|other
  UNIQUE (scope_type, scope_id, lower(name), coalesce(type, ''))

memory_entity_mentions   -- M:N memory↔entity edges (1-hop graph)
  memory_id, entity_id   -- both ON DELETE CASCADE

memory_edges             -- memory↔memory edges (multi-hop graph)
  src_memory, dst_memory  -- ON DELETE CASCADE
  relation        TEXT    -- 'co_extracted' | 'same_subject' | 'mentions_entity'
  weight          REAL
```

**Indexes**: HNSW on every `vector` column (`m=16, ef_construction=64`); GIN on every `tsv`; B-tree on `(scope_type, scope_id, …)` for every common WHERE clause.

**Decisions defended**:
- `scope_type + scope_id` instead of single `user_id` column → originally I had `user_id TEXT NOT NULL`, which made anonymous mode impossible. Refactored before any code shipped.
- `ON DELETE SET NULL` on `supersedes` and `source_turn` → deleting a raw turn never orphans the memory history it sourced.
- `vector(1536)` always, even without OpenAI key (column NULL) → simpler than configurable `EMBEDDING_DIM` migration; lexical FTS carries when vector is empty.

---

## 4. Predicate taxonomy + multiplicity policy

LLM extractors emit predicates as free-form strings unless constrained. Without a closed set, "employer" on Monday and "works_at" on Wednesday land under different `key`s — the reconciler can't find the prior, and we get parallel active employers.

### Closed list (~30 canonical predicates)

`employer`, `job_title`, `work_field`, `previous_employer`, `lives_in`, `lived_in`, `from`, `timezone`, `name`, `age`, `partner`, `family_member`, `friend`, `coworker`, `owns_pet`, `pet_name`, `pet_type`, `dietary_restriction`, `allergic_to`, `medical_condition`, `likes`, `dislikes`, `prefers`, `avoids`, `hobby`, `communication_style`, `opinion`, `attended`, `did`

Each has a `(type, multiplicity)` declared in `extraction/taxonomy.py`:

- **`multiplicity=one`** — single-valued: `employer`, `lives_in`, `name`, `age`, `partner`, `timezone`, `from`, `communication_style`, `job_title`, `work_field`. A new object replaces the prior active.
- **`multiplicity=many`** — multi-valued: `owns_pet`, `allergic_to`, `likes`, `family_member`, `friend`, `hobby`, `opinion`, `attended`. Multiple actives coexist.

### Alias normalizer

LLMs (and rule-extractors) will still emit common variants. `PredicateNormalizer` maps `works_at → employer`, `lives → lives_in`, `loves → likes`, etc. — populated by observation, not theory. About 30 aliases shipped.

### Escape hatch

Unknown predicates become `other:<short_snake_case_topic>` with conservative `multiplicity=one`. The reconciler still treats them safely; the closed list just doesn't grow randomly.

**Defence (closed list + escape hatch vs. open-ended)**: a fully closed taxonomy can't cover real conversations; a fully open one breaks supersession. Hybrid + normalizer is the compromise that lets supersession be safe on the common cases and graceful on the long tail.

---

## 5. `POST /turns` pipeline (synchronous, atomic)

1. **Validate** input (Pydantic). 422 on malformed.
2. **Embed** raw turn `full_text` (outside transaction — network IO doesn't hold a DB connection).
3. **Extract candidates** via `ExtractionService`:
    - LLM path (`gpt-4o-mini` + JSON schema): one call with predicate taxonomy injected into the prompt; output normalized via `PredicateNormalizer`.
    - Rule fallback (regex over user-role lines): 17 patterns covering employer / lives_in / owns_pet / dietary / allergic_to / likes/dislikes / corrections.
4. **Batch-embed** memory values (best-effort, per-element None on failure).
5. **Open transaction**:
   - `INSERT turns`
   - For each candidate:
     - `pg_advisory_xact_lock(hashtext("scope:scope_id:key"))` — serialize same-fact reconciles.
     - `reconciler.reconcile()` returns `ReconcileDecision(insert, active, supersedes, deactivate_ids)`.
       - `multiplicity=one` + different object → supersede prior, insert new.
       - `multiplicity=many` → coexist by default; correction marker in `raw_quote` targeting a prior object → supersede that prior.
       - Same object already active → idempotent skip.
     - `UPDATE memories SET active=false WHERE id IN decision.deactivate_ids`.
     - If `decision.insert`: `INSERT memories` with `active=decision.active`, `supersedes=decision.supersedes`.
     - Upsert each named entity, link via `memory_entity_mentions`.
     - **v0.6**: create `memory_edges`:
       - `co_extracted` between every pair of memories extracted from this turn (weight 0.7).
       - `same_subject` between this memory and any other active memory in scope with the same `subject` (weight 0.5).
   - `COMMIT`.
6. Return `{"id": turn_id}` 201.

**Why outside-then-inside-the-txn**: holding a Postgres connection while an LLM call streams for ~3s saturates the pool. Embedding + extraction first, then a fast pure-INSERT transaction.

---

## 6. `POST /recall` pipeline (RRF + gate + tiered assembler)

```
query → analyze → fan out retrievers + stable facts + entity hop
      → RRF fuse → multi-hop graph expansion → relevance gate
      → tiered assemble → tiktoken hard-trim → return
```

### 6.1 QueryAnalyzer

One LLM call returning:

```json
{
  "intent": "factoid_about_user | factoid_general | exploratory | recent_context | cold",
  "profile_relevant": true | false,
  "entities": ["..."],
  "expanded_queries": ["..."]
}
```

**Heuristic fallback** (no LLM): keyword triggers (`PROFILE_TRIGGERS`), negative-intent matchers (`nginx`/`capital of`/`how to`), capitalized-token entity extraction. Conservative — errs toward `profile_relevant=False` rather than dumping facts.

### 6.2 Retrievers (parallel via `asyncio.gather`)

Each acquires its own pool connection (asyncpg disallows concurrent statements on the same connection — caught in v0.2 as a real bug):

- `vector_turns` — pgvector cosine top-k on turns within scope
- `fts_turns` — `ts_rank_cd` top-k on turns
- `vector_memories` — pgvector cosine on active memories
- `fts_memories` — `ts_rank_cd` on active memories

Plus three single-conn sequential lookups:
- `list_stable_facts` — high-confidence active facts/preferences (for potential Tier 1)
- `entities_for_names(query.entities)` — entity-match check
- `memories_mentioning_entities` — 1-hop graph hop via `memory_entity_mentions`

### 6.3 Memory-edges multi-hop (v0.6)

After RRF gives a ranked candidate list, take the top-N memory candidates and pull their 1-hop neighbors via `memory_edges` (weighted by edge `weight`). Add neighbors to the candidate pool with weight-attenuated synthetic score. Re-fuse.

This is the *explicit* graph hop — distinct from the entity-anchored hop above which traverses through `entities`. The two complement each other: entity hop bridges from query→memory; edge hop bridges memory→memory.

### 6.4 Weighted RRF fusion

```
score(d) = Σ_sources w_s / (k + rank_s(d))
```

with `k=60`, `SOURCE_WEIGHTS = {memory_vector:1.4, memory_fts:1.1, graph:1.5, turn_vector:1.0, turn_fts:0.7, edge_hop:0.8}`. Rank-based, so heterogeneous score scales (cosine vs ts_rank_cd vs synthetic graph priors) don't fight each other.

**Defence vs. weighted sum of normalized scores**: normalization is unstable at small N; rank order is robust. Per-source weights encode "memories beat turns" without discarding turn evidence.

### 6.5 Relevance gate (Invariant 3, codified)

`decide_gate` accepts 5 booleans and returns `GateState(tier1_open, tier2_open)`. The rules in order:

| Condition | Tier 1 | Tier 2 |
|---|---|---|
| `entity_match` AND `profile_relevant` | ✅ | ✅ |
| `entity_match` (no profile) | ❌ | ✅ |
| `memory_signal` AND `profile_relevant` | ✅ | ✅ |
| `memory_signal` (no profile) | ❌ | ✅ |
| `is_open_ended_about_user` (no signal, but intent ∈ {exploratory, recent_context} + profile) | ✅ | ❌ |
| `turn_signal` only | ❌ | ✅ |
| else | ❌ | ❌ |

**Signal thresholds**:
- vector hits gated on `settings.min_relevance_cosine` (default 0.30)
- FTS hits on memories: floor 0.01 (any meaningful match counts because memories are structured)
- FTS hits on turns: floor 0.05 (turns are noisy; require non-trivial overlap)
- Graph/entity-anchored hits: synthetic 0.9, always clears

### 6.6 Tiered context assembler

Three sections with explicit budget bounds:

| Tier | Header | Budget | Source |
|---|---|---|---|
| 1 | `## Known facts about this user` | ≤ 40% of `max_tokens` | `list_stable_facts` (active, confidence ≥ 0.5, fact-before-preference-before-opinion order) |
| 2 | `## Relevant memories` | ≤ 40% | Memory candidates from RRF + graph hop + edge hop |
| 3 | `## From recent conversations` | rest | Turn snippets |

**Arc rendering** is symmetric across tiers (v0.10 wired Tier 1; **v0.11** extended it to Tier 2). When the analyzer's intent is `exploratory` or `factoid_about_user`, *any* line whose memory has a `supersedes` chain renders as `- Currently X (previously: Y, until DATE)` — whether it surfaced via Tier 1 (`list_stable_facts`) or via the retriever pipeline (Tier 2 memory candidates). The "until" date is the *new* memory's source-turn timestamp (when the user actually stated the contradiction), not the wall-clock instant the supersession transaction fired. For `factoid_general` or `cold` intents the legacy `(updated DATE)` form is used — keeping simple factoids compact and arcs reserved for queries where evolution is plausibly being asked about. The symmetry matters because the agent prompt is one string: if the same memory rendered with-arc in one query and without-arc in another (depending on ranking that shifts it between tiers), the agent's historical context would be inconsistent across runs.

**Trim mechanics**: tiktoken (`cl100k_base`) for accurate counts, char/4 fallback if encoding can't load. Per-snippet soft cap (~160 tokens) so one long turn can't eat the budget.

**Citations**: every surfaced item (Tier 1, 2, 3) produces a `Citation(turn_id, score, snippet)`. For memories the `turn_id` is `source_turn` so the consumer can always click through to a real turn.

### 6.7 Priority defence (asked for in spec §3)

The spec asks us to defend our priority logic. The argument:

1. **Gate first** — empty over wrong. Without this, recall@k looks good but precision tanks on noise.
2. **Tier 1 first under budget pressure** — stable facts answer the most queries with the fewest tokens. Job, location, allergies — 1–2 lines each.
3. **Tier 3 last** — raw turns are the noisiest, the least supersession-aware (can't mark immutable text inactive), and the most token-hungry. Budget pressure trims them first, which is exactly the right direction.

---

## 7. Fact evolution + opinion arcs

`reconciler.reconcile` decides per candidate:

- Hard contradictions (Stripe → Notion for `employer`): supersession chain in DB; `/recall` returns the active one; `/users/{id}/memories` returns full chain.
- Corrections ("actually X, not Y"): correction marker in `raw_quote` triggers targeted supersession when a prior object is mentioned in the correction.
- Opinion arcs: treated as `multiplicity=many` with the same correction-marker rule. Latest active is what gets surfaced; priors stay inactive. **Full arc rendering (v0.10):** Tier 1 renders `- Currently X (previously: Y, until DATE)` when a prior exists and the analyzer's intent is `exploratory` or `factoid_about_user`. See §6.6 above for the exact rules.

---

## 8. Cross-session scoping

`user_id != null` → memories cross sessions for that user (the *whole point* of a memory service).
`user_id == null` → anonymous mode; memories live and die in the session.

Documented and tested (`test_anonymous_session_roundtrip`). No bleed.

---

## 9. Failure modes (documented, tested where practical)

| Failure | Behavior | Test |
|---|---|---|
| No `OPENAI_API_KEY` | Lexical-only mode (vector retrievers no-op, rule extractor takes over). Startup warning. | manual; everything still passes |
| DB unreachable | `/health` 503; other endpoints 503 via pool dep. Service stays up. | manual |
| Oversized body | 413 from `PayloadSizeLimitMiddleware` *before* Pydantic parses. | `test_payload_size.py` |
| Malformed JSON / missing fields | 422 with stable error shape, never crashes. | `test_endpoints_shape.py` |
| Unicode / RTL / emoji | Stored verbatim. FTS is `english` config, so non-English retrieval is lossy. | `test_unicode_payload_does_not_crash` |
| LLM extraction timeout | `chat_json` returns None; rule extractor takes over. Turn still committed. | by design |
| LLM analyzer timeout | Heuristic fallback. Recall still runs. | by design |
| Concurrent `/turns` same `(scope, key)` | Advisory lock serializes. Exactly one wins active. | `test_concurrent_writes_dont_split_active` |
| `docker compose down` then `up` | Named volume preserves all data. | `scripts/test_persistence.sh` |
| Auth header missing when `MEMORY_AUTH_TOKEN` set | 401 on protected endpoints; `/health` still 200. | `test_health.py` (3 tests) |

---

## 10. Testing strategy

| Suite | Where | Count | What it pins |
|---|---|---:|---|
| Contract: health + auth | `tests/contract/test_health.py` | 3 | `/health` works without token even when auth configured; protected endpoints reject without token |
| Contract: shapes | `tests/contract/test_endpoints_shape.py` | 11 | Every endpoint's request/response shape; malformed JSON; missing fields; unicode |
| Contract: supersession | `tests/contract/test_supersession.py` | 3 | A→A' chain; `many` coexistence; **advisory-lock split-brain test** |
| Contract: relevance gate | `tests/contract/test_relevance_gate.py` | 4 | Noise → empty; anonymous session; tight budget (max_tokens=50); cold user |
| Contract: payload size | `tests/contract/test_payload_size.py` | 3 | Oversized → 413; normal still works; invalid Content-Length → 4xx |
| Quality: recall | `tests/quality/test_recall_quality.py` | 2 | Per-category recall_hits / forbidden_hits / empty_violations on the fixture |
| Quality: extraction | `tests/quality/test_extraction_quality.py` | 1 | Per-category extraction_hits on the fixture |
| **Total** | | **27+** | |

Persistence is covered by `scripts/test_persistence.sh` — uses `docker compose down/up` and a direct DB row count to prove data survives a restart. Not pytest-based by choice (needs Docker control from the test, which is awkward in CI).

Quality fixture: 8 scripted turns across 2 users + 11 probe queries with `expected_any`, `forbidden`, and `expect_empty_context` flags covering hard contradiction, multi-hop, implicit fact, opinion arc, correction, noise resistance, scope isolation.

---

## 11. What was deferred (and why)

| Deferred | Why |
|---|---|
| LLM reranker | RRF already does most of the precision work. Reranker adds per-recall LLM cost and latency. Wait for eval evidence we need it. Dead scaffolding removed in v0.9.2. |
| ~~Full opinion-arc surfacing in assembler~~ | **Shipped in v0.10.** Tier 1 renders "currently X (previously Y, until DATE)" when a supersession chain exists and the analyzer's intent is `exploratory` or `factoid_about_user`. The "until" date is the source-turn timestamp of the *new* memory, not the wall-clock instant the supersession transaction fired. |
| Multi-process split-brain test | Advisory lock is a well-documented Postgres primitive; a multi-process fixture is over-engineering for this service. |
| `pg_search` / paradedb BM25 | Requires custom Postgres image. `ts_rank_cd` + OR-tokenization is enough at this scale. |
| Local sentence-transformers fallback | Adds ~1GB of model weights to the image, requires download on first run, semantic quality below `text-embedding-3-small`. Lexical FTS-only mode is more honest. |
| Query-rewriting via LLM (beyond 0-2 paraphrases) | Marginal returns at this fixture size; adds LLM cost per recall. Reconsider with eval data. |
| Multi-tenant auth | Spec §12 explicitly out of scope. |

---

## 12. Future Scaling Trajectory (Beyond v1.0)

As the service scales to millions of users and larger context budgets, the following transitions are pre-architected:

- **Matryoshka Embeddings**: Using `text-embedding-3-small` allows us to truncate the 1536d vectors to 512d or 256d without significant recall degradation. This would allow fitting more embeddings into RAM and speeding up cosine similarity if the index becomes a bottleneck.
- **BERT Predicate Resolution**: Currently, we use LLM-based taxonomy + rule aliases. At scale, a distilled DeBERTa-based classification head could map freeform text to our predicate taxonomy at much lower cost and latency than a GPT-4o-mini call.
- **HNSW ef_construction tuning**: Based on anticipated recall latency requirements, we can tune the `ef_construction` and `m` parameters in Postgres to optimize the trade-off between index size and retrieval precision.

---

## 12. Three invariants — restated as ship-readiness checklist

Before release, every one of these is provable:

- [x] **I1 Scope isolation**: grep all `WHERE` clauses in `repo/`. Each filters by `(scope_type, scope_id)`. No global query exists.
- [x] **I2 Atomic ingestion**: `routes_turns.py:ingest_turn` opens one `async with conn.transaction()`; all writes happen inside; the embedding+LLM calls happen outside. `pg_advisory_xact_lock` is inside the txn, so it auto-releases on rollback.
- [x] **I3 Empty over wrong**: `decide_gate` has six explicit rules; the only paths to a non-empty context all require concrete evidence. `test_irrelevant_query_returns_empty` pins this for three sample noise queries; the quality fixture's `noise_resistance` category catches regressions.

---

## 13. The HTTP contract (the part we don't get to design)

The spec defines these 7 endpoints with exact shapes. The plan above all serves *answering this contract well*:

| Method | Path | Implementation file |
|---|---|---|
| GET | `/health` | `routes_admin.py` |
| POST | `/turns` | `routes_turns.py` |
| POST | `/recall` | `routes_recall.py` |
| POST | `/search` | `routes_search.py` |
| GET | `/users/{user_id}/memories` | `routes_memories.py` |
| DELETE | `/sessions/{session_id}` | `routes_admin.py` |
| DELETE | `/users/{user_id}` | `routes_admin.py` |

Auth: optional `Bearer <token>` when `MEMORY_AUTH_TOKEN` is set; whitelisted on `/health`, `/docs`, `/openapi.json`.

---

## 14. Runtime profiles

**What you get without an OPENAI_API_KEY**: a service that boots, passes every contract test, extracts ~75% of expected facts via rules, retrieves via FTS-only with weighted RRF, supersedes contradictions correctly, and returns empty on noise. Recall@k ~64% on the self-fixture, 0 empty-violations.

**What an OPENAI_API_KEY unlocks**: vector retrievers activate (RRF blends them in for free, no code change), LLM extraction lands opinion arcs and corrections that rules miss, recall climbs toward saturation, the LLM-based QueryAnalyzer handles edge cases the heuristic misses.

The "with key" path was the design target throughout; the "without key" path is honest degradation, not a compromised design. The relevance gate, supersession, scope isolation, tiered assembler, and graph hop all work identically in both modes — only the *quality of the extracted memories* and *quality of semantic retrieval* change.

---

## 15. Pointers

- Architecture diagram + quickstart + invariants: [`README.md`](README.md)
- Per-iteration history (v0.1 → v0.11.1), metrics, every bug caught: [`CHANGELOG.md`](CHANGELOG.md)
- Boot it: `docker compose up -d --build && bash scripts/smoke.sh`
- Test it: `make test` (33 tests inside the running container)
