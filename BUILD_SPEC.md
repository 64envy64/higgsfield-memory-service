# BUILD_SPEC — Memory Service (forward build specification)

> Read alongside [`TASK.md`](TASK.md). TASK is **what was asked**; this file is **how we answer it**. It merges the original plan with the P0/P1 argue deltas, and pins every load-bearing detail (taxonomy, prompts, SQL, regexes, thresholds) so a coding agent doesn't drift.
>
> **Methodology: Forward Build Specification.** This document was authored *before* the implementation phase. By pinning invariants (I1–I3) and technical schemas here, we ensured the resulting system is "designed by engineering constraints" rather than emerging from ad-hoc prompting. If the code deviates from this spec, the code is considered to have "drifted" and must be corrected to match this design source of truth.

---

## 0. How to use this doc

You are a coding agent. Before any change:

1. Re-read the relevant section here.
2. If you need to deviate from this spec (new pattern, new predicate, new gate rule, new SQL), **update this file first** with rationale, then ship code that matches.
3. CHANGELOG records the iteration step; this file records the canonical design.

The doc is organized **build-order** (foundations → ingest → recall → tests → ops), so an agent rebuilding from scratch can read top-to-bottom and ship.

---

## 1. Goal & non-negotiables

Build a memory service for an AI agent that:

- Ingests conversation turns **synchronously** — one HTTP call, no eventual consistency.
- Extracts **structured, typed knowledge**, not raw message chunks.
- Reconciles contradictions via **supersession** (keep history, surface current).
- Answers `/recall` with **tier-budgeted, gate-protected** context.
- Persists across container restarts via a named Docker volume.
- Conforms to the 7-endpoint HTTP contract in [`TASK.md`](TASK.md) §3.
- Boots with `docker compose up` on a clean machine — no manual setup.

**Out of scope** (TASK.md §12): UI, multi-tenant prod hardening, horizontal scalability proofs, agent-side code, schema migrations.

---

## 2. Three invariants (load-bearing — enforced at data model + transaction layer)

### I1 — Scope isolation

Every read is parameterized by `(scope_type, scope_id)`. Cross-user bleed is **impossible by construction**, not by convention.

- `user_id != null` → `scope=('user', user_id)`: memories follow the user across sessions.
- `user_id == null` → `scope=('session', session_id)`: anonymous mode; data dies with the session.
- `/search` with both `user_id` *and* `session_id` null → `{"results":[]}`. **No global search ever.**

Encoded in the `CHECK (scope_type IN ('user','session'))` constraint. Every SQL query in `repo/` filters by `(scope_type, scope_id)`.

### I2 — Atomic ingestion

`POST /turns` commits everything in **one Postgres transaction**:

```
[OUTSIDE TXN]
  embed(full_text)           -- network IO; don't hold a connection during this
  extract(full_text)         -- LLM or rule fallback; same

[INSIDE TXN]
  INSERT turns ...
  for each candidate:
    pg_advisory_xact_lock(hashtext("scope:scope_id:key"))     -- per-key serialization
    reconcile()                                               -- decide insert/supersede/skip
    UPDATE memories SET active=false WHERE id IN deactivate   -- prior actives
    INSERT memories ...                                       -- new candidate
    UPSERT entities; INSERT memory_entity_mentions            -- 1-hop graph anchors
  for each pair of inserted memories: INSERT memory_edges (co_extracted, weight 0.7)
  for each inserted, peers sharing subject: INSERT memory_edges (same_subject, weight 0.5)
COMMIT
```

After `201` returns, every write is visible to `/recall`, `/search`, `/users/{id}/memories`. **No race window.**

### I3 — Empty over wrong

`/recall` returns `{"context":"","citations":[]}` whenever the relevance gate doesn't fire. **Profile facts are never injected as filler.** A query about a topic the user never discussed must return empty.

Crucially, `profile_relevant=True` *alone* (without a concrete retrieval signal) **never** opens Tier 1. "What does this user think about TypeScript?" against a user with no TypeScript memory must not dump that user's location and job.

---

## 3. Tech stack (decisions + defences)

| Decision | Choice | Defence | Rejected |
|---|---|---|---|
| Language | Python 3.12 | Best LLM/embedding/SDK ecosystem; Pydantic + async FastAPI. | Go (overkill perf); Rust (LLM ecosystem too thin). |
| HTTP | FastAPI | Pydantic-typed, async, ASGI test transport. | Starlette (no Pydantic), Flask (sync). |
| Backing store | **Postgres 16 + pgvector** | One process gives ACID for supersession (I2), `pg_advisory_xact_lock` for safe concurrent reconcile, HNSW vector + `tsvector` FTS — all in one transactional boundary. | SQLite + sqlite-vec (no advisory lock); Qdrant + Postgres (no cross-store txn). |
| Embeddings | OpenAI `text-embedding-3-small` (1536d) | Cost/quality winner; matryoshka re-projection possible. | `bge-small` (slower; quality lower); local fallback (no honest 1536d fallback exists). |
| Extraction LLM | `gpt-4o-mini` + `response_format={"type":"json_object"}` | Cheap, fast, deterministic structured outputs. | gpt-4o (overkill); Claude (no second SDK). |
| Reranker | **None** (deferred to v1.0+) | Weighted RRF over 6 sources covers precision at zero LLM-per-recall cost. | Cross-encoder (heavy image); LLM listwise (network call per recall). |
| No-key fallback (embed) | **None** — column NULL, lexical-only mode | Honest degradation. Padding to fake 1536d would inject pseudo-semantics. | hashing-vectorizer (noise > signal); local sentence-transformer (~1GB image, runtime download). |
| No-key fallback (extract) | **Regex over user-role lines** — no external models | Self-contained, no runtime downloads. ~75% recall on the self-fixture. | spaCy (heavy; model download). |
| Logging | python-json-logger to stdout | One line per event; grep-able. | Structlog (overkill at this scale). |
| Auth | Bearer token, optional | Whitelist `/health`, `/docs`, `/openapi.json` so probes work regardless. | None (spec asks for optional bearer). |

---

## 4. Repository structure (final tree)

```
memory-service/
├── README.md
├── CHANGELOG.md
├── TASK.md                                # HTTP contract and requirements
├── BUILD_SPEC.md                          # this file — canonical design
├── PLAN.md                                # human-facing design doc (defends each decision)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
├── Makefile
├── scripts/
│   ├── smoke.sh                           # the curl smoke test from TASK §7
│   └── test_persistence.sh                # down/up cycle test
├── src/memory_service/
│   ├── __init__.py                        # __version__
│   ├── main.py                            # FastAPI app, lifespan, middleware wiring
│   ├── config.py                          # pydantic-settings — all knobs
│   ├── api/
│   │   ├── deps.py                        # pool, settings, auth deps
│   │   ├── middleware.py                  # PayloadSizeLimitMiddleware
│   │   ├── routes_admin.py                # /health, DELETE /sessions, DELETE /users
│   │   ├── routes_turns.py                # POST /turns
│   │   ├── routes_recall.py               # POST /recall
│   │   ├── routes_search.py               # POST /search
│   │   └── routes_memories.py             # GET /users/{id}/memories
│   ├── schemas/                           # Pydantic in/out shapes
│   │   ├── turns.py
│   │   ├── recall.py
│   │   ├── search.py
│   │   └── memories.py
│   ├── db/
│   │   ├── pool.py                        # asyncpg pool, codec init, run_migrations
│   │   └── migrations/001_init.sql        # full schema
│   ├── repo/
│   │   ├── turn_repo.py                   # scope_for, flatten_messages, CRUD on turns
│   │   └── memory_repo.py                 # CRUD on memories, entities, edges, recompute_active
│   ├── extraction/
│   │   ├── models.py                      # MemoryCandidate, EntityMention dataclasses
│   │   ├── taxonomy.py                    # PREDICATES + _ALIASES + normalize_predicate
│   │   ├── llm_extractor.py               # extract_via_llm + SYSTEM prompt
│   │   ├── rule_extractor.py              # PATTERNS regex bank + extract_via_rules
│   │   └── service.py                     # ExtractionService (LLM → rules fallback chain)
│   ├── llm/client.py                      # AsyncOpenAI wrapper, chat_json
│   ├── embedding/client.py                # AsyncOpenAI embeddings wrapper, embed/embed_batch
│   ├── services/
│   │   ├── reconciler.py                  # acquire_lock, reconcile, apply_decision
│   │   ├── retrievers.py                  # 4 retrievers + Candidate dataclass
│   │   ├── query_analyzer.py              # LLM analyze + heuristic fallback
│   │   ├── fusion.py                      # rrf_fuse, SOURCE_WEIGHTS
│   │   ├── assembler.py                   # TierBudget, GateState, decide_gate, assemble
│   │   ├── recall.py                      # /recall orchestration
│   │   └── search.py                      # /search orchestration
│   └── util/
│       ├── tokens.py                      # tiktoken + chars/4 fallback
│       ├── text.py                        # query_tokens, to_or_tsquery, _STOP list
│       └── logging.py                     # JSON logging
├── fixtures/
│   ├── conversations.json                 # scripted turns
│   └── probes.json                        # queries with expected_any / forbidden / expect_empty_context
└── tests/
    ├── conftest.py                        # pool fixture, ASGI client
    ├── contract/
    │   ├── test_health.py
    │   ├── test_endpoints_shape.py
    │   ├── test_supersession.py
    │   ├── test_relevance_gate.py
    │   └── test_payload_size.py
    └── quality/
        ├── test_recall_quality.py
        └── test_extraction_quality.py
```

---

## 5. DB schema (full migration SQL)

Place in `src/memory_service/db/migrations/001_init.sql`. Idempotent (every DDL uses `IF NOT EXISTS`).

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ===========================================================================
-- turns — raw conversation log (immutable source of truth)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS turns (
    id              UUID PRIMARY KEY,
    session_id      TEXT NOT NULL,
    user_id         TEXT,                                       -- may be NULL (anonymous)
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('user','session')),
    scope_id        TEXT NOT NULL,
    messages        JSONB NOT NULL,
    full_text       TEXT NOT NULL,                              -- flattened for FTS/embed
    timestamp       TIMESTAMPTZ NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding       vector(1536),                               -- NULL if no OPENAI_API_KEY
    tsv             TSVECTOR
);

CREATE INDEX IF NOT EXISTS turns_scope_idx ON turns (scope_type, scope_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS turns_session_idx ON turns (session_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS turns_user_idx ON turns (user_id, timestamp DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS turns_tsv_idx ON turns USING GIN (tsv);
CREATE INDEX IF NOT EXISTS turns_embedding_idx
    ON turns USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE OR REPLACE FUNCTION turns_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english', coalesce(NEW.full_text, ''));
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS turns_tsv_update ON turns;
CREATE TRIGGER turns_tsv_update
    BEFORE INSERT OR UPDATE OF full_text ON turns
    FOR EACH ROW EXECUTE FUNCTION turns_tsv_trigger();

-- ===========================================================================
-- memories — extracted, typed, queryable knowledge
-- ===========================================================================
CREATE TABLE IF NOT EXISTS memories (
    id              UUID PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('user','session')),
    scope_id        TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('fact','preference','opinion','event')),
    subject         TEXT NOT NULL,                              -- 'user', 'pet:Biscuit'
    predicate       TEXT NOT NULL,                              -- canonical from PREDICATES or 'other:*'
    object          TEXT NOT NULL,                              -- 'Notion', 'Berlin'
    key             TEXT GENERATED ALWAYS AS (subject || '::' || predicate) STORED,
    value           TEXT NOT NULL,                              -- human-readable summary
    raw_quote       TEXT,
    confidence      REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    source_session  TEXT,
    source_turn     UUID REFERENCES turns(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes      UUID REFERENCES memories(id) ON DELETE SET NULL,
    active          BOOLEAN NOT NULL DEFAULT true,
    embedding       vector(1536),
    tsv             TSVECTOR
);

CREATE INDEX IF NOT EXISTS memories_scope_active_idx ON memories (scope_type, scope_id, active);
CREATE INDEX IF NOT EXISTS memories_scope_key_active_idx ON memories (scope_type, scope_id, key, active);
CREATE INDEX IF NOT EXISTS memories_source_session_idx ON memories (source_session);
CREATE INDEX IF NOT EXISTS memories_tsv_idx ON memories USING GIN (tsv);
CREATE INDEX IF NOT EXISTS memories_embedding_idx
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE OR REPLACE FUNCTION memories_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english',
                           coalesce(NEW.value, '') || ' ' ||
                           coalesce(NEW.subject, '') || ' ' ||
                           coalesce(NEW.predicate, '') || ' ' ||
                           coalesce(NEW.object, '') || ' ' ||
                           coalesce(NEW.raw_quote, ''));
    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_tsv_update ON memories;
CREATE TRIGGER memories_tsv_update
    BEFORE INSERT OR UPDATE OF value, subject, predicate, object, raw_quote ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_tsv_trigger();

-- ===========================================================================
-- entities — named entity anchors for multi-hop graph
-- ===========================================================================
CREATE TABLE IF NOT EXISTS entities (
    id              UUID PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('user','session')),
    scope_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    type            TEXT,                                       -- 'person','pet','place','org','other'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS entities_unique_idx
    ON entities (scope_type, scope_id, lower(name), coalesce(type, ''));
CREATE INDEX IF NOT EXISTS entities_lookup_idx ON entities (scope_type, scope_id, lower(name));

CREATE TABLE IF NOT EXISTS memory_entity_mentions (
    memory_id       UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id       UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS mem_entity_by_entity_idx ON memory_entity_mentions (entity_id);

-- ===========================================================================
-- memory_edges — cheap memory↔memory graph for 1-hop traversal during recall
-- ===========================================================================
CREATE TABLE IF NOT EXISTS memory_edges (
    src_memory      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    dst_memory      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,                              -- 'co_extracted','same_subject','mentions_entity'
    weight          REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src_memory, dst_memory, relation)
);
CREATE INDEX IF NOT EXISTS edges_src_idx ON memory_edges (src_memory);
CREATE INDEX IF NOT EXISTS edges_dst_idx ON memory_edges (dst_memory);

-- ===========================================================================
-- _schema_meta — bookkeeping (single-version per TASK §12)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS _schema_meta (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO _schema_meta (version) VALUES (1) ON CONFLICT DO NOTHING;
```

**Notable decisions:**
- `scope_type/scope_id` instead of `user_id` (P0 delta #2). Anonymous mode falls out cleanly.
- `embedding vector(1536)` always — NULL is the no-key fallback. **Never** EMBEDDING_DIM env-var migration.
- `supersedes ON DELETE SET NULL` and `source_turn ON DELETE SET NULL` — deleting a turn never orphans history.
- `key` is a generated column so the reconciler can lookup by `subject::predicate` deterministically.
- Trigger keeps `tsv` up to date on every write — no application-level FTS maintenance.

---

## 6. Predicate taxonomy (`extraction/taxonomy.py`)

### 6.1 Closed list (29 canonical predicates)

```python
PREDICATES = (
    # employment / professional
    PredicateSpec("employer",          "fact",       "one",  "Current employer / company"),
    PredicateSpec("job_title",         "fact",       "one",  "Current role or job title"),
    PredicateSpec("work_field",        "fact",       "one",  "Broad professional field (engineering, design, finance)"),
    PredicateSpec("previous_employer", "fact",       "many", "A past employer (for history; never overwrites employer)"),

    # location
    PredicateSpec("lives_in",          "fact",       "one",  "Current city/place of residence"),
    PredicateSpec("lived_in",          "fact",       "many", "Past place of residence"),
    PredicateSpec("from",              "fact",       "one",  "Where the user is from / hometown"),
    PredicateSpec("timezone",          "fact",       "one",  "User's timezone"),

    # personal identifiers
    PredicateSpec("name",              "fact",       "one",  "User's preferred name"),
    PredicateSpec("age",               "fact",       "one",  "User's age, if stated"),

    # relationships
    PredicateSpec("partner",           "fact",       "one",  "Spouse/partner"),
    PredicateSpec("family_member",     "fact",       "many", "Named family members"),
    PredicateSpec("friend",            "fact",       "many", "Named friends"),
    PredicateSpec("coworker",          "fact",       "many", "Named coworkers"),

    # pets
    PredicateSpec("owns_pet",          "fact",       "many", "Has a pet — object is the pet identifier ('dog:Biscuit' or 'Biscuit')"),
    PredicateSpec("pet_name",          "fact",       "many", "A pet's name"),
    PredicateSpec("pet_type",          "fact",       "many", "A pet's species"),

    # dietary / health
    PredicateSpec("dietary_restriction","fact",      "many", "Vegetarian, vegan, kosher, halal, etc."),
    PredicateSpec("allergic_to",       "fact",       "many", "Allergy or intolerance"),
    PredicateSpec("medical_condition", "fact",       "many", "Stable medical condition the user mentions"),

    # preferences
    PredicateSpec("likes",             "preference", "many", "Things the user likes"),
    PredicateSpec("dislikes",          "preference", "many", "Things the user dislikes"),
    PredicateSpec("prefers",           "preference", "many", "Stated preference between options"),
    PredicateSpec("avoids",            "preference", "many", "Things the user actively avoids"),
    PredicateSpec("hobby",             "preference", "many", "Hobbies / pastimes"),
    PredicateSpec("communication_style","preference","one",  "How they want to be talked to"),

    # opinions
    PredicateSpec("opinion",           "opinion",    "many", "Stated viewpoint — object is the topic"),

    # events
    PredicateSpec("attended",          "event",      "many", "Attended an event"),
    PredicateSpec("did",               "event",      "many", "Did/experienced something noteworthy"),
)
```

### 6.2 Alias normalizer (`_ALIASES`)

```python
_ALIASES = {
    # employer
    "works_at": "employer", "work_at": "employer", "works_for": "employer",
    "employed_by": "employer", "company": "employer", "current_company": "employer",
    "current_employer": "employer",
    # job_title
    "job": "job_title", "role": "job_title", "position": "job_title", "title": "job_title",
    # lives_in
    "lives": "lives_in", "lives_at": "lives_in", "live_in": "lives_in",
    "current_city": "lives_in", "city": "lives_in", "location": "lives_in",
    "based_in": "lives_in",
    # lived_in
    "moved_from": "lived_in", "used_to_live_in": "lived_in",
    # from
    "originally_from": "from", "hometown": "from",
    # partner
    "spouse": "partner", "husband": "partner", "wife": "partner",
    "boyfriend": "partner", "girlfriend": "partner",
    # owns_pet
    "has_pet": "owns_pet", "pet": "owns_pet", "dog": "owns_pet", "cat": "owns_pet",
    # dietary_restriction
    "diet": "dietary_restriction", "is_vegetarian": "dietary_restriction",
    "is_vegan": "dietary_restriction",
    # allergic_to
    "allergy": "allergic_to",
    # likes / dislikes / prefers
    "loves": "likes", "love": "likes", "enjoys": "likes", "fan_of": "likes",
    "hates": "dislikes", "dislike": "dislikes", "not_a_fan_of": "dislikes",
    "prefer": "prefers",
    # opinion
    "thinks": "opinion", "believes": "opinion", "view_on": "opinion", "opinion_on": "opinion",
}
```

### 6.3 `normalize_predicate(raw) -> str`

1. Lowercase, replace `[ -]` with `_`, strip leading `other:`.
2. If in `PREDICATE_INDEX` → return as-is.
3. If in `_ALIASES` → return mapped value.
4. Else → return `other:{first 48 chars}`.

### 6.4 `spec_for(predicate) -> PredicateSpec`

Lookup in `PREDICATE_INDEX`. Fallback for `other:*` is conservative: `type="fact", multiplicity="one"` (so contradictions trigger supersession by default).

### 6.5 `predicates_prompt_block() -> str`

Renders the closed list as a markdown block. Injected into the LLM extractor prompt so the model emits canonical predicates whenever applicable.

---

## 7. Extraction

### 7.1 LLM extractor (`extraction/llm_extractor.py`)

**System prompt** (verbatim):

```
You extract durable, queryable knowledge from a single conversation turn.

Your job is to surface facts, preferences, opinions, and notable events about
the USER (the human in the conversation), so they can be recalled in future
sessions. You do NOT extract things about the assistant, and you do NOT invent
facts that aren't stated.

Output rules:
- Return JSON: {"memories": [...], "entities": [...]}.
- `memories` is a list of objects with fields:
    type      : "fact" | "preference" | "opinion" | "event"
    subject   : almost always "user". For facts about a named non-user entity
                (e.g. a pet) use "<type>:<name>", e.g. "pet:Biscuit".
    predicate : prefer one from the list below; otherwise "other:<topic>".
    object    : the canonical value (e.g. "Notion", "Berlin", "shellfish",
                "TypeScript"). Short, normalized.
    value     : human-readable summary ("Works at Notion as a PM").
    raw_quote : the verbatim phrase from the user message it came from.
    confidence: 0.0–1.0. Lower confidence if it's hedged ("I think", "maybe").
- `entities` is a list of {name, type} for named entities mentioned
  (people, pets, places, orgs). Include every distinct named entity that the
  memories reference, so multi-hop recall can traverse later.

What to extract:
- Personal facts: employer, job title, location (current and past),
  family, pets, dietary needs, allergies.
- Preferences and opinions, including hedged ones.
- IMPLICIT facts: "walking Biscuit before work" → owns_pet/pet_name = Biscuit;
  "had to leave early to pick up the kids" → has children.
- CORRECTIONS: when the user says "actually, …" or "sorry, I meant …",
  emit the corrected fact at higher confidence. The reconciler will handle
  supersession; you only need to emit the new state.

What NOT to extract:
- Anything stated by the assistant (assistant turns are context only).
- Hypotheticals ("if I moved to Berlin…") unless they're stating intent.
- Generic small talk with no durable content.

If the turn has nothing extractable, return {"memories": [], "entities": []}.
```

**User prompt assembly:**
```
{predicates_prompt_block()}

TURN TO EXTRACT FROM:
----------------------------------------
{messages_text.strip()}
----------------------------------------
```

**Call:** `chat_json(system=SYSTEM, user=user_prompt, temperature=0.0, timeout_s=settings.extraction_timeout_s)` against `settings.extraction_model` (`gpt-4o-mini`).

**Post-processing:**
1. Parse `obj["memories"]` and `obj["entities"]`.
2. For each memory dict: validate `type`, normalize `predicate` via `normalize_predicate`, clamp `confidence` to [0,1], drop if `value` or `object` empty.
3. Union envelope-level `entities` with any per-memory embedded entities.
4. Return `list[MemoryCandidate]`.

**Failure modes** (all return `[]`):
- LLM disabled / no key → caller falls back to rules.
- Invalid JSON → swallowed by `chat_json`.
- Envelope doesn't match `{"memories":..., "entities":...}`.
- Per-candidate malformed → drop that one, keep the rest.

### 7.2 Rule extractor (`extraction/rule_extractor.py`)

Used when LLM is disabled OR returned `[]`. Walks regex patterns over **user-role lines only** (lines prefixed `user:` by `flatten_messages`).

**Capture helper:**
```python
_CAP = r"(?-i:[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,2})"   # 1–3 capitalized words
```
The `(?-i:...)` scope keeps `[A-Z]` case-sensitive even inside a `re.IGNORECASE` enclosing pattern — otherwise "Notion last month" greedily eats lowercase words.

**Patterns** (17 entries, `(regex, predicate, type, group_name)`):

```python
PATTERNS = (
    # Employment ----------------------------------------------------------
    (rf"\bi(?:'m| am)?\s+(?:an?|the)\s+(\w+(?:\s+\w+){{0,2}})\s+at\s+({_CAP})\b",
     "employer", "fact", "g2"),
    (rf"\bi(?:'ve)?\s+just\s+joined\s+({_CAP})(?:\s+as\s+an?\s+(\w+(?:\s+\w+){{0,2}}))?",
     "employer", "fact", "g1"),
    (rf"\bi\s+work(?:ed|ing)?\s+(?:at|for)\s+({_CAP})\b",
     "employer", "fact", "g1"),
    (rf"\bi\s+joined\s+({_CAP})\b",
     "employer", "fact", "g1"),

    # Location ------------------------------------------------------------
    (rf"\b(?:i|and)\s+(?:just\s+|recently\s+|finally\s+|already\s+|then\s+)?"
     rf"(?:moved|relocated|relocating)\s+(?:to|out\s+to)\s+({_CAP})\b",
     "lives_in", "fact", "g1"),
    (rf"\bi\s+(?:live|am\s+living)\s+in\s+({_CAP})\b",
     "lives_in", "fact", "g1"),
    (rf"\bbased\s+in\s+({_CAP})\b",
     "lives_in", "fact", "g1"),
    (rf"\bi\s+(?:used\s+to\s+live|lived)\s+in\s+({_CAP})\b",
     "lived_in", "fact", "g1"),
    (rf"\b(?:moved|relocated|moving)\s+(?:to\s+\S+\s+)?from\s+({_CAP})\b",
     "lived_in", "fact", "g1"),
    (rf"\bi(?:'m| am)\s+from\s+({_CAP})\b",
     "from", "fact", "g1"),

    # Pets ---------------------------------------------------------------
    (rf"\bmy\s+(dog|cat|bird|hamster|rabbit|fish|turtle)\s+(?:is\s+)?(?:named\s+|called\s+)?({_CAP})",
     "owns_pet", "fact", "g2"),
    (rf"\b(?:walking|walked|feeding|fed)\s+({_CAP})\b",
     "owns_pet", "fact", "g1"),

    # Dietary / allergy --------------------------------------------------
    (r"\bi(?:'m| am)\s+(vegetarian|vegan|pescatarian|kosher|halal)\b",
     "dietary_restriction", "fact", "g1"),
    (r"\bi\s+(?:don't|do\s+not)\s+eat\s+([a-z][\w\s]{1,30})\b",
     "dietary_restriction", "fact", "g1"),
    (r"\bi(?:'m| am)\s+(?:seriously\s+|severely\s+)?allergic\s+to\s+([a-z][\w\s]{1,30})\b",
     "allergic_to", "fact", "g1"),
    (r"\b(?:seriously\s+|severely\s+)?allergic\s+to\s+([a-z][\w\s]{1,30})\b",
     "allergic_to", "fact", "g1"),

    # Preferences --------------------------------------------------------
    (r"\bi\s+(?:love|adore|really\s+like)\s+([A-Z]?[\w\s]{1,40})\b",
     "likes", "preference", "g1"),
    (r"\bi\s+(?:hate|can't\s+stand|loathe)\s+([A-Z]?[\w\s]{1,40})\b",
     "dislikes", "preference", "g1"),
    (r"\bi\s+(?:prefer)\s+([A-Z]?[\w\s]{1,40})\b",
     "prefers", "preference", "g1"),
)
```

**Helpers:**
- `_TRAILING_CONNECTOR` — regex of dangling articles/prepositions/conjunctions; `_trim_trailing_connectors(s)` strips them iteratively. Fixes the case where IGNORECASE breaks `_CAP`'s anchor and the capture eats `as|in|the|...`.
- `_CORRECTION` — `\b(actually|sorry|correction|i meant)\b` — boosts `confidence` to 0.85 for matched candidates (vs 0.6 default).
- `_USER_LINE` — `^user(?:\([^)]*\))?:\s*(.+)$` — only iterate user-role lines (assistant text is context, not facts).
- `_value_for(predicate, object, raw)` — human-readable summary ("Works at Notion", "Lives in Berlin", "Previously lived in NYC", "Has a pet named Biscuit", etc.).
- `_entity_for(predicate, object)` — emits `EntityMention(name=obj, type="org"|"place"|"pet")` for predicates that have a canonical entity type.

**Dedupe:** within one turn, drop subsequent matches with the same `(subject, predicate, lower(object))`.

### 7.3 ExtractionService chain (`extraction/service.py`)

```python
async def extract(self, messages_text: str) -> list[MemoryCandidate]:
    if not messages_text.strip():
        return []
    if self._llm.is_enabled:
        cands = await llm_extractor.extract_via_llm(
            client=self._llm, messages_text=messages_text,
            timeout_s=self._settings.extraction_timeout_s,
        )
        if cands:
            return cands
        # LLM disabled or returned [] — try rules as backstop.
    return rule_extractor.extract_via_rules(messages_text)
```

Singleton via `get_extraction_service(settings, llm)`.

---

## 8. Reconciler (`services/reconciler.py`)

### 8.1 Decision tree

```python
@dataclass
class ReconcileDecision:
    insert: bool                       # do we INSERT the new candidate?
    active: bool                       # should the new row be active?
    supersedes: UUID | None            # row this one supersedes
    deactivate_ids: list[UUID]         # ids to mark active=false
```

Logic per candidate (after lock acquired):

```
spec = spec_for(candidate.predicate)
existing = SELECT id, object, value, raw_quote, active, confidence
           FROM memories
           WHERE scope_type=$1 AND scope_id=$2 AND key=$3
           ORDER BY created_at DESC
active_rows = [r for r in existing if r.active]
new_obj_norm = candidate.object.strip().lower()
is_correction = bool(_CORRECTION_MARKERS.search(candidate.raw_quote or ""))

if spec.multiplicity == "one":
    if any(r.object.lower() == new_obj_norm for r in active_rows):
        return Decision(insert=False, active=False, supersedes=None, deactivate_ids=[])
    prior = active_rows[0] if active_rows else None
    return Decision(insert=True, active=True,
                    supersedes=prior.id if prior else None,
                    deactivate_ids=[r.id for r in active_rows])

# multiplicity == "many"
if any(r.object.lower() == new_obj_norm for r in active_rows):
    return Decision(insert=False, ...)                          # idempotent

if is_correction:
    targets = [r for r in active_rows
               if r.object and r.object.lower() in (candidate.raw_quote or "").lower()]
    if targets:
        return Decision(insert=True, active=True,
                        supersedes=targets[0].id,
                        deactivate_ids=[t.id for t in targets])
    if active_rows:
        return Decision(insert=True, active=True,
                        supersedes=active_rows[0].id,
                        deactivate_ids=[active_rows[0].id])

# default for many: coexist
return Decision(insert=True, active=True, supersedes=None, deactivate_ids=[])
```

### 8.2 Correction markers

```python
_CORRECTION_MARKERS = re.compile(
    r"\b(actually|sorry|correction|i\s+meant|never\s+mind|scratch\s+that|"
    r"that\s+was\s+wrong|let\s+me\s+correct)\b",
    re.IGNORECASE,
)
```

### 8.3 Advisory lock (concurrency)

Acquired **inside** the txn, before reading `existing`:

```python
async def acquire_lock(conn, *, scope_type, scope_id, key) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"{scope_type}:{scope_id}:{key}",
    )
```

Auto-released at txn end. Lock granularity is per-`(scope, key)` — two concurrent turns about *different* facts for the same user don't block; two turns about the *same* fact serialize correctly. **Acceptance test:** `test_concurrent_writes_dont_split_active`.

### 8.4 `apply_decision`

```python
async def apply_decision(conn, decision):
    if decision.deactivate_ids:
        await conn.execute(
            "UPDATE memories SET active=false WHERE id = ANY($1::uuid[])",
            decision.deactivate_ids,
        )
    # INSERT done by caller (it also needs to record source_turn, source_session)
```

---

## 9. `POST /turns` pipeline

```python
@router.post("/turns", status_code=201)
async def ingest_turn(payload: TurnIn, pool, settings, _auth) -> TurnOut:
    turn_id = uuid.uuid4()
    full_text = flatten_messages(payload.messages)         # "user: ...\nassistant: ..."
    scope_type, scope_id = scope_for(payload.user_id, payload.session_id)

    # Phase 1: OUTSIDE TXN — network IO
    embedder = get_embedding_client(settings)
    llm = get_llm_client(settings)
    extractor = get_extraction_service(settings, llm)

    turn_embedding = await embedder.embed(full_text) if embedder.is_enabled else None
    candidates = await extractor.extract(full_text)

    memory_vecs = (await embedder.embed_batch([c.value for c in candidates])
                   if candidates and embedder.is_enabled
                   else [None] * len(candidates))

    # Phase 2: INSIDE TXN — atomic write
    async with pool.acquire() as conn:
        async with conn.transaction():
            await turn_repo.insert_turn(conn, turn_id=turn_id, payload=payload, embedding=turn_embedding)

            inserted_ids: list[UUID] = []
            inserted_subjects: list[str] = []
            for cand, vec in zip(candidates, memory_vecs, strict=True):
                await reconciler.acquire_lock(conn, scope_type=scope_type, scope_id=scope_id, key=cand.key())
                decision = await reconciler.reconcile(conn, scope_type=scope_type, scope_id=scope_id, candidate=cand)
                await reconciler.apply_decision(conn, decision)
                if not decision.insert:
                    continue
                mem_id = uuid.uuid4()
                await memory_repo.insert_memory(conn, memory_id=mem_id, scope_type=scope_type, scope_id=scope_id,
                                                type=cand.type, subject=cand.subject, predicate=cand.predicate,
                                                object_=cand.object, value=cand.value, raw_quote=cand.raw_quote,
                                                confidence=cand.confidence, source_session=payload.session_id,
                                                source_turn=turn_id, supersedes=decision.supersedes,
                                                active=decision.active, embedding=vec)
                inserted_ids.append(mem_id)
                inserted_subjects.append(cand.subject)
                # Entity mentions
                for ent in cand.entities:
                    if not ent.name:
                        continue
                    eid = await memory_repo.upsert_entity(conn, scope_type=scope_type, scope_id=scope_id,
                                                          name=ent.name, type_=ent.type)
                    await memory_repo.link_mention(conn, memory_id=mem_id, entity_id=eid)

            # memory_edges (v0.6)
            for i, src_id in enumerate(inserted_ids):
                # co_extracted edges (symmetric within this turn)
                for j, dst_id in enumerate(inserted_ids):
                    if i == j:
                        continue
                    await memory_repo.insert_edge(conn, src_memory=src_id, dst_memory=dst_id,
                                                  relation="co_extracted", weight=0.7)
                # same_subject edges with prior actives (both directions)
                peers = await memory_repo.active_memories_with_subject(
                    conn, scope_type=scope_type, scope_id=scope_id,
                    subject=inserted_subjects[i], exclude_id=src_id,
                )
                for peer_id in peers:
                    await memory_repo.insert_edge(conn, src_memory=src_id, dst_memory=peer_id,
                                                  relation="same_subject", weight=0.5)
                    await memory_repo.insert_edge(conn, src_memory=peer_id, dst_memory=src_id,
                                                  relation="same_subject", weight=0.5)

    return TurnOut(id=str(turn_id))
```

`flatten_messages` produces lines like `user: ...\nassistant: ...\ntool(funcname): ...` — same format the rule extractor and FTS read.

---

## 10. `POST /recall` pipeline

```
query → analyze → fan out retrievers + stable facts + entity hop
      → memory_edges 1-hop expansion → weighted RRF → relevance gate
      → tiered assemble → hard token trim → return
```

### 10.1 QueryAnalyzer (`services/query_analyzer.py`)

**System prompt:**
```
You classify a single recall query that an agent is about to ask its memory store.

Return JSON with this exact shape:
{
  "intent": "factoid_about_user" | "factoid_general" | "exploratory" | "recent_context" | "cold",
  "profile_relevant": true | false,
  "entities": ["..."],
  "expanded_queries": ["...", "..."]
}

Definitions:
- intent:
    factoid_about_user  — asks about the user's own facts/preferences/history
    factoid_general     — asks about something outside the user's life
                          (general knowledge, code, the world)
    exploratory         — open-ended ("what should I cook tonight given my prefs?")
    recent_context      — refers explicitly to the current/recent conversation
                          ("what did I just say about X?")
    cold                — small talk / greetings / nothing to recall

- profile_relevant:
    true  iff returning stable facts about the user (job, location, pets, allergies,
          preferences, recent activities) would help answer this query.
    false otherwise. False for factoid_general and cold.

- entities: proper-noun-like things in the query (names, places, orgs, pets).
            Used to traverse the user's knowledge graph. Lowercased not required.

- expanded_queries: 0–2 short paraphrases that preserve meaning. Used to
                    broaden lexical retrieval. Skip if the original is already concise.

Be strict: if the query is obviously unrelated to the user, return profile_relevant=false.
```

User prompt is literally `f"QUERY: {query.strip()}"`. Temperature 0.0, timeout 6s.

**Heuristic fallback** (when LLM disabled or returns malformed JSON):

```python
_PROFILE_TRIGGERS = re.compile(
    r"\b("
    r"my|mine|i\b|me\b|user|users?|"
    r"prefer|preferences?|like|likes?|love|hate|dislike|"
    r"remember|recall|know\s+about|"
    r"work|job|employer|company|"
    r"live|location|city|address|"
    r"pet|dog|cat|family|partner|wife|husband|kids|"
    r"allerg|diet|vegetarian|vegan|"
    r"hobby|hobbies"
    r")\b", re.IGNORECASE,
)

_NEGATIVE_INTENT = re.compile(
    r"\b(capital|nginx|kubernetes|sql|python|javascript|api|http|how\s+to|"
    r"what\s+is\s+(the|a)\b|why\s+is)\b", re.IGNORECASE,
)

_CAP_TOKEN = re.compile(r"\b([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,2})\b")
```

Algorithm:
1. `profile = bool(_PROFILE_TRIGGERS.search(query))`
2. If `not profile` → `intent = "factoid_general" if _NEGATIVE_INTENT.search(query) else "exploratory"`
3. Elif `"remember" in q.lower() or "recall" in q.lower() or "just" in q.lower()` → `intent = "recent_context"`
4. Else → `intent = "factoid_about_user"`
5. Entities = first 6 capitalized 1–3-word spans, excluding `{what, where, when, who, why, how, the}`.
6. `expanded_queries = []` (heuristic never paraphrases).

Heuristic errs toward `profile_relevant=False` — better to miss a fact than dump irrelevant facts.

### 10.2 Retrievers (`services/retrievers.py`)

Six sources. Each parallel retriever **acquires its own pool connection** — asyncpg connections cannot service concurrent statements (bug caught in v0.2).

**`vector_turns` / `vector_memories`** (cosine top-k, no-op if `embedder.is_enabled = False`):
```sql
SELECT id, ..., 1 - (embedding <=> $1::vector) AS score
FROM turns | memories
WHERE scope_type=$2 AND scope_id=$3
  AND embedding IS NOT NULL
  [AND active=true   -- only for memories]
ORDER BY embedding <=> $1::vector
LIMIT $4
```

**`fts_turns` / `fts_memories`** (ts_rank_cd top-k):
```sql
SELECT id, ..., ts_rank_cd(tsv, q) AS score
FROM turns | memories, to_tsquery('english', $1) q
WHERE scope_type=$2 AND scope_id=$3 AND tsv @@ q
  [AND active=true]
ORDER BY score DESC
LIMIT $4
```

The tsquery is built via `util.text.to_or_tsquery(query)`:
- Tokenize via `_WORD = r"[A-Za-z][A-Za-z0-9_-]{1,}"`.
- Lowercase, dedupe, stop-filter against `_STOP` set (full list in `util/text.py`: function words + interrogatives + aux verbs + pronouns + conversational fluff + role prefixes `{user, assistant, tool, system}` because `flatten_messages` injects them).
- Build `'t1 | t2 | t3'` — OR semantics, no `:*` prefix wildcard (English stemmer + wildcard caused `france:*` → `franc:*` → match `francisco`; see CHANGELOG v0.8).

**Entity-anchored hop** (graph retriever):
```sql
-- step 1: lookup entities by name in scope
SELECT id, name, type FROM entities
WHERE scope_type=$1 AND scope_id=$2 AND lower(name) = ANY($3::text[])

-- step 2: pull all memories mentioning those entities
SELECT DISTINCT m.id, m.subject, m.predicate, m.object, m.value, m.type,
       m.confidence, m.source_turn, m.source_session, m.updated_at
FROM memories m
JOIN memory_entity_mentions mem ON mem.memory_id = m.id
WHERE mem.entity_id = ANY($1::uuid[]) AND m.active = true
ORDER BY m.confidence DESC LIMIT $2
```

Yielded as `Candidate(source="graph", score=0.9, kind="memory", ...)` — high synthetic score because a named-entity match is strong evidence.

**memory_edges 1-hop** (`memory_repo.memories_via_edges`, called *after* RRF on the top memory candidates):
```sql
SELECT m.id, m.value, m.subject, m.predicate, m.object, m.type, m.confidence,
       m.source_turn, m.source_session, m.updated_at,
       max(e.weight) AS edge_weight,
       array_agg(DISTINCT e.relation) AS relations
FROM memory_edges e
JOIN memories m ON m.id = e.dst_memory
WHERE e.src_memory = ANY($1::uuid[])
  AND m.active = true
  AND NOT (m.id = ANY($1::uuid[]))      -- exclude seeds themselves
GROUP BY m.id
ORDER BY edge_weight DESC, m.confidence DESC
LIMIT $2
```

Top-8 seed memories from RRF feed into this; up to 12 neighbors come back as `Candidate(source="edge_hop", score=float(edge_weight))`.

**Stable-facts query** (Tier-1 source):
```sql
SELECT id, type, subject, predicate, object, value, confidence,
       source_turn, updated_at
FROM memories
WHERE scope_type=$1 AND scope_id=$2
  AND active=true
  AND type IN ('fact','preference')
  AND confidence >= $3            -- default 0.5
ORDER BY CASE type WHEN 'fact' THEN 0 ELSE 1 END,
         confidence DESC, updated_at DESC
LIMIT $4                           -- default 16
```

Always fetched; the **gate** decides whether to inject.

### 10.3 Weighted RRF (`services/fusion.py`)

```python
SOURCE_WEIGHTS = {
    "memory_vector": 1.4,
    "memory_fts":    1.1,
    "graph":         1.5,        # entity-anchored memories — strongest
    "edge_hop":      0.9,        # memory_edges neighbors — weak prior
    "turn_vector":   1.0,
    "turn_fts":      0.7,        # noisiest
}

def rrf_fuse(candidates_per_source, *, k=60) -> list[tuple[Candidate, float]]:
    fused_score = defaultdict(float)
    chosen_cand: dict[(kind, id), (Candidate, weight)] = {}
    for source, cands in candidates_per_source.items():
        weight = SOURCE_WEIGHTS.get(source, 1.0)
        for rank, c in enumerate(cands):
            key = (c.kind, c.id)
            fused_score[key] += weight / (k + rank + 1)      # rank is 0-based
            prev = chosen_cand.get(key)
            if prev is None or weight > prev[1]:
                chosen_cand[key] = (c, weight)
    return sorted([(chosen_cand[k][0], s) for k, s in fused_score.items()],
                  key=lambda x: x[1], reverse=True)
```

`settings.rrf_k = 60`.

### 10.4 Signal thresholds for the gate

```python
_TURN_FTS_MIN   = 0.05      # ts_rank_cd hits on turns must clear coincidence
_MEMORY_FTS_MIN = 0.01      # any meaningful FTS hit on structured memory counts
# vector hits use settings.min_relevance_cosine, default 0.30
# graph (entity-anchored) is synthetic 0.9 — always clears

def _has_memory_signal(memory_cands, *, cosine_min):
    for c in memory_cands:
        if c.source == "memory_vector" and c.score >= cosine_min: return True
        if c.source == "memory_fts" and c.score >= _MEMORY_FTS_MIN: return True
        if c.source == "graph": return True
    return False

def _has_turn_signal(turn_cands, *, cosine_min):
    for c in turn_cands:
        if c.source == "turn_vector" and c.score >= cosine_min: return True
        if c.source == "turn_fts" and c.score >= _TURN_FTS_MIN: return True
    return False
```

### 10.5 Relevance gate (`services/assembler.decide_gate`)

Inputs (5 booleans):
- `profile_relevant` — from QueryAnalyzer
- `is_open_ended_about_user` — `profile_relevant AND intent IN ('exploratory','recent_context')`
- `has_memory_signal` — see above
- `has_turn_signal` — see above
- `has_entity_match` — `bool(matched_entities)` (entities returned by `entities_for_names`)

Rules, in priority order:

| Condition | Tier 1 | Tier 2 |
|---|:---:|:---:|
| `has_entity_match` | `profile_relevant` | ✅ |
| `has_memory_signal AND profile_relevant` | ✅ | ✅ |
| `has_memory_signal` (no profile) | ❌ | ✅ |
| `is_open_ended_about_user` (no concrete signal) | ✅ | ❌ |
| `has_turn_signal` only | ❌ | ✅ |
| else | ❌ | ❌ → empty |

**Defence of the entity-match row:** "tell me about Biscuit" mentions a named entity but isn't asking about the owner's job — so Tier 2 opens unconditionally, but Tier 1 still requires `profile_relevant`. This stops the gate from leaking unrelated profile data when an entity is the focus.

### 10.6 Tiered assembler (`services/assembler.assemble`)

```python
@dataclass
class TierBudget:
    max_tokens: int
    tier1_pct: float = 0.4
    tier2_pct: float = 0.4
    # Tier 3 gets remainder
```

Sections (each formatted as markdown), in this exact order:

| Tier | Header | Soft budget | Content |
|---|---|---|---|
| 1 | `## Known facts about this user\n` | `int(max_tokens * 0.4)` | `_format_stable_fact(row)` = `- {value} (updated YYYY-MM-DD)` |
| 2 | `## Relevant memories\n` | `int(max_tokens * 0.4)` | `- {candidate.content.strip()}`, deduped against Tier 1 values |
| 3 | `## From recent conversations\n` | `max_tokens - used` (remainder) | `- [YYYY-MM-DD] {turn_snippet}` — turn snippet hard-trimmed to **160 tokens** via `trim_to_tokens` |

Per-tier behavior:
- If Tier 1 closed → skip section entirely.
- If `len(section) == 0` after fitting lines → skip section.
- Token accounting: `tiktoken.encode("cl100k_base")` when available; chars/4 fallback otherwise (`util/tokens.py`).
- Header tokens (`approx_token_count(header)+1`) count once per section.
- Per-line accounting includes a `+1` for the newline.
- Budget is **absolute** (`used + soft_cap <= max_tokens`). Unused tier budget flows down (Tier 2 doesn't reclaim Tier 1's unused share — flow is one-way to protect Tier 1).

**Citations** (`schemas/recall.Citation`):
- One emitted per surfaced line.
- For turn candidates: `turn_id = candidate.id`.
- For memory candidates (Tier 2): `turn_id = candidate.source_turn` (the turn the memory was extracted from).
- For Tier 1 stable facts: `turn_id = row["source_turn"]` (skip emit if `source_turn` is NULL).
- `score = round(cand.score, 4)` for retrievers; `round(row["confidence"], 4)` for stable facts.
- `snippet` = the rendered line minus the leading `- `.

### 10.7 Recall orchestration (`services/recall.py`)

```python
async def recall(*, payload, pool, settings, embedder) -> RecallOut:
    scope_type, scope_id = scope_for(payload.user_id, payload.session_id)
    llm = get_llm_client(settings)

    # 1. analyze
    analysis = await query_analyzer.analyze(payload.query, llm=llm)

    # 2. fan-out retrievers for original + up to 2 paraphrases
    queries_to_run = [payload.query, *analysis.expanded_queries][:3]
    retriever_results = await asyncio.gather(*[_run_4_retrievers(q) for q in queries_to_run])
    all_cands = [c for chunk in retriever_results for c in chunk]

    # 3. stable facts + entity lookup + entity-anchored memories (single conn, sequential)
    async with pool.acquire() as conn:
        stable_facts = await memory_repo.list_stable_facts(conn, scope_type, scope_id,
                                                           min_confidence=0.5, limit=16)
        matched_entities = await memory_repo.entities_for_names(
            conn, scope_type, scope_id, names=analysis.entities,
        )
        entity_memories = (await memory_repo.memories_mentioning_entities(
                              conn, entity_ids=[e["id"] for e in matched_entities], limit=12)
                           if matched_entities else [])

    # inject entity-anchored memories as Candidate(source="graph", score=0.9)
    for em in entity_memories:
        all_cands.append(Candidate(source="graph", kind="memory", id=em["id"], score=0.9, ...))

    # 4. memory_edges 1-hop expansion from top-8 memory candidates
    seed_memory_ids = [UUID(c.id) for c in all_cands if c.kind == "memory"][:8]
    if seed_memory_ids:
        async with pool.acquire() as conn:
            edge_memories = await memory_repo.memories_via_edges(conn,
                                                                  src_memory_ids=seed_memory_ids,
                                                                  limit=12)
        for em in edge_memories:
            all_cands.append(Candidate(source="edge_hop", kind="memory", id=em["id"],
                                       score=float(em["edge_weight"]), ...))

    # 5. RRF
    fused = rrf_fuse(group_by_source(all_cands), k=settings.rrf_k)
    fused.sort(key=lambda x: (x[0].kind != "memory", -x[1]))   # memory-first within RRF-sorted

    memory_cands = [c for c,_ in fused if c.kind == "memory"]
    turn_cands   = [c for c,_ in fused if c.kind == "turn"]

    # 6. gate
    gate = decide_gate(
        profile_relevant=analysis.profile_relevant,
        is_open_ended_about_user=(analysis.profile_relevant
                                  and analysis.intent in ("exploratory","recent_context")),
        has_memory_signal=_has_memory_signal(memory_cands, cosine_min=settings.min_relevance_cosine),
        has_turn_signal=_has_turn_signal(turn_cands, cosine_min=settings.min_relevance_cosine),
        has_entity_match=bool(matched_entities),
    )

    # 7. assemble
    context, citations = assemble(stable_facts=stable_facts,
                                  memory_candidates=memory_cands,
                                  turn_candidates=turn_cands,
                                  gate=gate,
                                  budget=TierBudget(max_tokens=payload.max_tokens))
    return RecallOut(context=context, citations=citations)
```

---

## 11. `POST /search` pipeline

Simpler — no gate, no assembler. Agents call this explicitly; they've decided to retrieve.

```python
async def search(*, payload, pool, settings, embedder) -> SearchOut:
    # Scope resolution
    if payload.user_id:        scope_type, scope_id = "user", payload.user_id
    elif payload.session_id:   scope_type, scope_id = "session", payload.session_id
    else:                      return SearchOut(results=[])              # global search ⇒ empty

    # 4 retrievers in parallel
    results = await asyncio.gather(
        retrievers.vector_turns(...),    retrievers.fts_turns(...),
        retrievers.vector_memories(...), retrievers.fts_memories(...),
        return_exceptions=True,
    )
    candidates = [c for r in results if not isinstance(r, BaseException) for c in r]

    fused = rrf_fuse(group_by_source(candidates), k=settings.rrf_k)[:payload.limit]
    return SearchOut(results=[
        SearchResult(
            content=c.content,
            score=round(fused_score, 6),
            session_id=c.session_id or "",
            timestamp=c.timestamp or datetime.now(timezone.utc),
            metadata={"kind": c.kind, "source": c.source, **c.metadata},
        )
        for c, fused_score in fused
    ])
```

---

## 12. DELETE pipelines (active-recompute, P1 delta #5)

### `DELETE /sessions/{session_id}` (`routes_admin.delete_session`)

```python
async with pool.acquire() as conn:
    async with conn.transaction():
        affected = await memory_repo.affected_scope_keys(conn, session_id)
        # ^ SELECT DISTINCT scope_type, scope_id, key FROM memories WHERE source_session = $1

        await memory_repo.delete_by_session(conn, session_id)
        # ^ DELETE FROM memories WHERE source_session = $1

        await turn_repo.delete_by_session(conn, session_id)
        # ^ DELETE FROM turns WHERE session_id = $1

        await conn.execute(
            "DELETE FROM entities WHERE scope_type = 'session' AND scope_id = $1",
            session_id,
        )

        for scope_type, scope_id, key in affected:
            await memory_repo.recompute_active(conn, scope_type, scope_id, key)
return Response(status_code=204)
```

`recompute_active`:
```sql
UPDATE memories SET active = false
WHERE scope_type=$1 AND scope_id=$2 AND key=$3;

UPDATE memories SET active = true
WHERE id = (
    SELECT id FROM memories
    WHERE scope_type=$1 AND scope_id=$2 AND key=$3
    ORDER BY created_at DESC
    LIMIT 1
);
```

Memory_entity_mentions cascade-cleared by FK. `supersedes ON DELETE SET NULL` prevents orphan chains.

### `DELETE /users/{user_id}` (`routes_admin.delete_user`)

Cascade-clear everything:
```python
async with pool.acquire() as conn:
    async with conn.transaction():
        await conn.execute("DELETE FROM entities WHERE scope_type='user' AND scope_id=$1", user_id)
        await memory_repo.delete_by_user(conn, user_id)
        await turn_repo.delete_by_user(conn, user_id)
return Response(status_code=204)
```

No active-recompute needed — everything for that user is gone.

---

## 13. `GET /users/{user_id}/memories`

```sql
SELECT id, type, subject, predicate, object, key, value, raw_quote,
       confidence, source_session, source_turn, created_at, updated_at,
       supersedes, active
FROM memories
WHERE scope_type='user' AND scope_id=$1
ORDER BY created_at DESC, id
```

Returns **both** active and superseded — the supersession chain is the inspection contract.

Response model `MemoriesResponse(memories: list[MemoryOut])` — exact fields in `schemas/memories.py`.

---

## 14. Auth + middleware

### Auth dep (`api/deps.require_auth`)

```python
_bearer = HTTPBearer(auto_error=False)

def require_auth(settings, creds):
    expected = settings.auth_token
    if not expected:
        return                                            # auth disabled → pass
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
```

**Whitelist** (P0 delta #4): `/health`, `/docs`, `/openapi.json` — these endpoints don't depend on `AuthDep`. Everything else does.

### PayloadSizeLimitMiddleware (`api/middleware.py`)

Registered before Pydantic parses the body. Default cap = `settings.max_payload_bytes` = `512 * 1024`.

1. Skip GET/DELETE (no body).
2. If `Content-Length` header present:
   - Parse → invalid → 400 `{"error":"invalid_content_length"}`.
   - `> max_bytes` → 413 `{"error":"payload_too_large","limit_bytes":...}`.
3. Buffer body chunks; reject 413 as soon as cumulative size exceeds limit.
4. Replay buffered body via wrapped `receive` so downstream handlers see same bytes.

### Global exception handlers (`main.py`)

- `RequestValidationError` → 422 with `{"detail": exc.errors(), "error": "validation_error"}`.
- Catch-all `Exception` → 500 with `{"error": "internal_error", "detail": "request failed"}` + `logger.exception(...)`. **Never** leak a stack trace.

---

## 15. Config (`config.py`)

`pydantic-settings.BaseSettings` with `env_prefix="MEMORY_"`. Full surface:

| Field | Default | Env var | Notes |
|---|---|---|---|
| `database_url` | `postgresql://memory:memory@db:5432/memory` | `MEMORY_DATABASE_URL` | |
| `auth_token` | `""` (disabled) | `MEMORY_AUTH_TOKEN` | If set, required as `Bearer` |
| `log_level` | `INFO` | `MEMORY_LOG_LEVEL` | |
| `extraction_model` | `gpt-4o-mini` | `MEMORY_EXTRACTION_MODEL` | LLM extraction + QueryAnalyzer |
| `embedding_model` | `text-embedding-3-small` | `MEMORY_EMBEDDING_MODEL` | |
| `embedding_dim` | `1536` | `MEMORY_EMBEDDING_DIM` | **Don't change** — schema is hard-coded |
| `max_payload_bytes` | `512 * 1024` | `MEMORY_MAX_PAYLOAD_BYTES` | Body cap |
| `max_turn_messages` | `64` | `MEMORY_MAX_TURN_MESSAGES` | Reflected in `TurnIn` `Field(..., max_length=64)` |
| `extraction_timeout_s` | `45.0` | `MEMORY_EXTRACTION_TIMEOUT_S` | LLM call ceiling |
| `default_recall_k` | `12` | `MEMORY_DEFAULT_RECALL_K` | Per-retriever limit |
| `rrf_k` | `60` | `MEMORY_RRF_K` | RRF constant |
| `min_relevance_cosine` | `0.30` | `MEMORY_MIN_RELEVANCE_COSINE` | Gate threshold for vector hits |

`openai_api_key` is read **unprefixed** from `os.environ["OPENAI_API_KEY"]`. `llm_enabled` is a derived property = `bool(openai_api_key)`.

---

## 16. Docker / runtime

### `docker-compose.yml`

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    container_name: memory-db
    environment:
      POSTGRES_USER: memory
      POSTGRES_PASSWORD: memory
      POSTGRES_DB: memory
    volumes:
      - memdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U memory -d memory"]
      interval: 5s
      timeout: 3s
      retries: 20
    restart: unless-stopped

  api:
    build: .
    container_name: memory-api
    depends_on:
      db:
        condition: service_healthy
    environment:
      MEMORY_DATABASE_URL: postgresql://memory:memory@db:5432/memory
      MEMORY_AUTH_TOKEN: ${MEMORY_AUTH_TOKEN:-}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      MEMORY_EXTRACTION_MODEL: ${MEMORY_EXTRACTION_MODEL:-gpt-4o-mini}
      MEMORY_EMBEDDING_MODEL: ${MEMORY_EMBEDDING_MODEL:-text-embedding-3-small}
      MEMORY_LOG_LEVEL: ${MEMORY_LOG_LEVEL:-INFO}
    ports:
      - "8080:8080"
    restart: unless-stopped

volumes:
  memdata:
    name: memory_service_data
```

### `Dockerfile`

```dockerfile
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" \
    "pydantic-settings>=2.5" "asyncpg>=0.30" "httpx>=0.27" "tiktoken>=0.8" \
    "openai>=1.54" "tenacity>=9.0" "python-json-logger>=2.0" "orjson>=3.10" \
    "pytest>=8.3" "pytest-asyncio>=0.24" "anyio>=4.6"

COPY src/ ./src/
COPY tests/ ./tests/
COPY fixtures/ ./fixtures/

ENV PYTHONPATH=/app/src
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8080/health || exit 1
CMD ["uvicorn", "memory_service.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
```

### `.env.example`

```
OPENAI_API_KEY=
MEMORY_AUTH_TOKEN=
MEMORY_EXTRACTION_MODEL=gpt-4o-mini
MEMORY_EMBEDDING_MODEL=text-embedding-3-small
MEMORY_LOG_LEVEL=INFO
```

### `Makefile`

```
.PHONY: help build up down logs ps health smoke test test-unit test-quality test-persistence clean

up:
    docker compose up -d --build
    @echo "Waiting for /health ..."
    @until curl -sf http://localhost:8080/health > /dev/null; do sleep 1; done
    @echo "Ready."

smoke:        ; @bash scripts/smoke.sh
test:         test-unit test-quality
test-unit:    ; docker compose exec -T api python -m pytest tests/contract -v
test-quality: ; docker compose exec -T api python -m pytest tests/quality -v
test-persistence: ; bash scripts/test_persistence.sh
clean:        ; docker compose down -v
```

### Async pool (`db/pool.py`)

- `asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=30, timeout=10, init=_init_connection)`.
- `_init_connection` registers `jsonb` and `json` codecs (decoder=`json.loads`).
- `run_migrations` reads `migrations/*.sql` in sorted order, applies each in its own txn. Idempotent.

### App lifespan (`main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings.database_url)
    await run_migrations(pool)
    app.state.pool = pool
    if not settings.llm_enabled:
        logger.warning("OPENAI_API_KEY is not set — running in lexical-only mode.")
    try:
        yield
    finally:
        await pool.close()
```

---

## 17. Tests

### 17.1 Contract tests (`tests/contract/`)

| File | What it pins |
|---|---|
| `test_health.py` | `/health` returns 200 even with `MEMORY_AUTH_TOKEN` set; `/recall` returns 401 without bearer when token configured; `/turns` 401 without bearer; both 200/201 with valid token. **3 tests.** |
| `test_endpoints_shape.py` | Every endpoint's request/response shape; malformed JSON → 422 not 500; missing fields → 422; unicode (emoji, CJK, RTL) stored without crash. **~11 tests.** |
| `test_supersession.py` | `employer` chain (Stripe → Notion): `/recall` returns only Notion, `/users/{id}/memories` shows both with `supersedes` chain. `many` predicates (two pets) coexist. **Concurrent writes acceptance test:** `test_concurrent_writes_dont_split_active` — two `asyncio.gather`'d `/turns` writing same `(scope, key)` → assert exactly one active row after. **3 tests.** |
| `test_relevance_gate.py` | (1) Noise query against populated user → `context == ""` and `citations == []`. (2) Anonymous session (`user_id=null`) roundtrip — recall returns only this session's data, not bleeding. (3) `max_tokens=50` against verbose memories → `tiktoken.encode(context)` ≤ 80. (4) Cold user (no memories at all) → empty. **4 tests.** |
| `test_payload_size.py` | (1) Body > `max_payload_bytes` → 413 `{"error":"payload_too_large"}`. (2) Normal body still 201. (3) Invalid `Content-Length` header → 4xx, no crash. **3 tests.** |

### 17.2 Quality tests (`tests/quality/`)

| File | What it does |
|---|---|
| `test_recall_quality.py` | Ingest entire `fixtures/conversations.json`, run all `probes.json` against `/recall`, score per probe: `recall_hit` (expected_any phrase appears in context), `forbidden_hit` (forbidden phrase appears), `empty_violation` (expect_empty_context but context is non-empty). Report per-category aggregate + total. Fail the test if `forbidden_hits > N` or `empty_violations > 0`. **2 tests** (one per metric). |
| `test_extraction_quality.py` | Same ingest, then call `/users/{id}/memories` for each fixture user; count `extraction_hits` per category (`fact_evolution`, `implicit_fact`, `correction`, …). Fail if below baseline. **1 test.** |

### 17.3 Persistence (`scripts/test_persistence.sh`)

Standalone bash, not pytest:
1. `docker compose up -d`
2. `POST /turns` for known fact.
3. Wait, `psql` query DB to verify row count.
4. `docker compose down`.
5. `docker compose up -d`, wait for `/health`.
6. `POST /recall` for the fact, assert it's in the returned context.
7. `psql` query DB, assert row count unchanged.

---

## 18. Fixtures

### `fixtures/conversations.json`

```json
[
  {
    "user_id": "fx-alice",
    "session_id": "fx-alice-s1",
    "timestamp": "...",
    "messages": [
      {"role": "user", "content": "I'm an engineer at Stripe based in San Francisco. ..."},
      {"role": "assistant", "content": "..."}
    ]
  },
  ...
]
```

Cover at minimum: hard contradiction (job/city evolves), multi-hop (entity → user → other-fact), implicit fact (`walking Biscuit` → pet), opinion arc (TypeScript take evolves), correction ("actually, it was the urgent care"), noise (no relevant content), scope isolation (two users with overlapping topics).

8 turns across 2 users is the current shipped fixture; expand as needed.

### `fixtures/probes.json`

Each probe shape:
```json
{
  "id": "alice_employer_current",
  "category": "fact_evolution",
  "user_id": "fx-alice",
  "session_id": "probe-1",
  "query": "Where does Alice currently work?",
  "expected_any": ["Notion"],                  // recall_hit if ANY appears in context
  "forbidden": ["Stripe"],                     // forbidden_hit if any appears
  "expect_empty_context": false                // empty_violation if non-empty
}
```

`forbidden_facts` was the P0 delta — quality metric becomes **`recall@k − precision_penalty(forbidden)`**.

11 probes is the current baseline; cover all categories from §17.2.

---

## 19. Iteration plan (forward, v0.1 → v0.9)

Each step: code change → run `make test` → record metrics in CHANGELOG.md.

| Version | Ships | Acceptance |
|---|---|---|
| **v0.1** | Skeleton: schema (§5), 7 contract endpoints with stubs (`/recall` returns empty, `/turns` stores raw turn only), middleware, auth, `docker compose up` clean boot. | `make smoke` shapes match. `make test-unit` passes 14 tests. |
| **v0.2** | Naïve retrieval baseline: vector + FTS top-k on `turns` only (no extraction yet). Quality fixture lands here. | Self-eval gives a number to beat. Stop-word list tuned (esp. role prefixes). |
| **v0.3** | LLM extractor + rule extractor + taxonomy (§6). `/turns` writes structured memories. Entities upserted, mentions linked. | `/users/{id}/memories` returns structured rows. Recall@k improves on memory-canonical queries. |
| **v0.4** | (Deferred — see CHANGELOG.) Was hybrid RRF; v0.5 took priority because supersession was needed before RRF over a no-op vector retriever was meaningful. |
| **v0.5** | Reconciler + advisory lock (§8). `/turns` transaction now opens lock → reconcile → apply_decision → insert. `forbidden_hits` drops. | `test_concurrent_writes_dont_split_active` passes. Self-eval: `forbidden_hits` ↓. |
| **v0.6** | Entity-anchored hop in `/recall` (§10.2). `memory_edges` populated on ingest (co_extracted + same_subject). `memory_edges` 1-hop traversal at recall time. | New contract test `test_co_extracted_neighbor_surfaces_via_edge_hop`. |
| **v0.7** | *(removed in v0.9.2 cleanup — LLM reranker scaffolded then deleted.)* |
| **v0.8** | QueryAnalyzer (§10.1) + relevance gate (§10.5) + tiered assembler (§10.6). `empty_violations` → 0. | `test_relevance_gate.py` (4 tests). Self-eval: `forbidden_hits` ↓ further. |
| **v0.9** | README rewrite, 2 new rule patterns (`moved to`, `from`) that complete the smoke test under no-key path. Final QA. | Smoke test produces structured output even without key. |
| **v0.9.1** | Post-audit hardening: memory candidates emit citations (was Tier-2-only invisible before), `PayloadSizeLimitMiddleware` registered, RRF fusion code path, gate fix on `entity_match`, `min_relevance_cosine` actually consumed, `DELETE /sessions` clears entities, dead `chat_text` removed. | 3 new contract tests. |
| **v0.9.2** | `memory_edges` graph hop in `/recall`. Reranker config surface removed (dead since v0.7 deletion). PLAN.md as separate design doc. | New test `test_co_extracted_neighbor_surfaces_via_edge_hop`. |

**Rule:** every iteration produces a CHANGELOG entry with `What changed / Why / Result (metrics) / Next`. The changelog should make the design path legible, not just list final features.

---

## 20. Failure modes (documented + tested where practical)

| Failure | Behavior | Test |
|---|---|---|
| No `OPENAI_API_KEY` | Lexical-only mode (vector retrievers no-op, rule extractor takes over). Startup warning. | Manual; all contract + quality tests still pass. |
| DB unreachable | `/health` 503; other endpoints 503 via pool dep. Service stays up. | Manual. |
| Oversized body | 413 from middleware **before** Pydantic parses. | `test_payload_size.py`. |
| Malformed JSON / missing field | 422 with stable error shape. | `test_endpoints_shape.py`. |
| Unicode / RTL / emoji | Stored verbatim; FTS uses `english` config (non-English retrieval lossy — documented in README). | `test_unicode_payload_does_not_crash`. |
| LLM extraction timeout | `chat_json` returns None → rule extractor takes over → turn still committed. | By design. |
| LLM analyzer timeout | Heuristic fallback. Recall still runs. | By design. |
| Concurrent `/turns` same `(scope, key)` | Advisory lock serializes; exactly one wins active. | `test_concurrent_writes_dont_split_active`. |
| `docker compose down/up` | Named volume `memory_service_data` preserves all data. | `scripts/test_persistence.sh`. |
| Auth header missing when `MEMORY_AUTH_TOKEN` set | 401 on protected endpoints; `/health` still 200. | `test_health.py`. |
| `/search` with both ids null | `{"results":[]}` — no global search. | `test_endpoints_shape.py`. |

---

## 21. Argue deltas (what changed from the initial plan, with rationale)

This section records the **P0/P1 deltas** from the design-review pass on the original plan. Each delta is now embedded in the spec above; this list exists so the challenged decisions and tradeoffs stay visible.

### P0

1. **Fallback embeddings → incompatible dims** (rejected EMBEDDING_DIM env-var migration). Decision: schema fixed at `vector(1536)`; without key, column is NULL and FTS carries. Honest degradation over fake semantics.
2. **`user_id=null` schema rejection** → `scope_type/scope_id` redesign. Anonymous mode is `scope='session'`; identified user is `scope='user'`. Both `turns` and `memories` use the same scope columns.
3. **StableFacts unconditional injection breaks noise resistance** → QueryAnalyzer + relevance gate. `profile_relevant=True` alone never opens Tier 1; concrete signal required.
4. **Auth middleware breaks `/health`** → whitelist `/health`, `/docs`, `/openapi.json`.
5. **Supersession race condition** → `pg_advisory_xact_lock(hashtext("scope:scope_id:key"))` inside ingest txn.

### P1

1. **Closed taxonomy is too rigid** → hybrid: ~29 canonical predicates + `_ALIASES` map + `other:*` escape hatch with conservative `multiplicity=one`.
2. **Multiplicity policy needs to be data, not code** → `PredicateSpec(predicate, type, multiplicity, description)` table; reconciler reads it.
3. **GraphHop via subject overlap is weak** → explicit `entities` table + `memory_entity_mentions` linking table. Query-time entity-anchored hop (§10.2).
4. **Postgres FTS ≠ BM25** → call it FTS (`ts_rank_cd`) in README/code; pg_search/paradedb deferred (custom image; not worth at this scale).
5. **DELETE /sessions doesn't recompute active** → explicit `recompute_active` per `(scope, key)` after delete (§12).
6. **spaCy in fallback is heavy** → pure regex rule extractor; no external models, no runtime downloads.

### Disputed

> "Make OPENAI_API_KEY required and document honestly."

**Rejected.** The service must boot and serve `/health` without a key. `docker compose up` with no env → service up, `/health` 200, `/turns` accepts (rule extraction), `/recall` works (lexical-only). Better operator experience: even if a key is misconfigured, nothing crashes. "Optional with degradation" > "required-or-broken-boot".

---

## 22. Release quality checklist

The implementation is expected to satisfy:

1. **Contract compliance is exact** — 7 endpoints, exact shapes, correct status codes.
2. **Extraction produces structured memories with types, confidence, provenance** — not raw text chunks. `/users/{id}/memories` is the proof.
3. **Fact evolution works** — contradictions detected, old facts superseded (`active=false`), history preserved (still in the table with `supersedes` chain). `/recall` returns current; `/users/{id}/memories` returns the chain.
4. **Recall is deliberate** — embedding + FTS + RRF + entity graph + memory_edges multi-hop. Not vanilla cosine-top-k.
5. **Context assembly has defended priority logic** — gate before facts, Tier 1 protected under pressure, Tier 3 trimmed first. README defends every choice.
6. **`/turns` is synchronous; no eventual consistency** — single txn with advisory lock.
7. **`/recall` respects budget** — hard tiktoken trim, per-tier soft caps.
8. **Persistence is real** — named volume, restart-survives test.
9. **Service degrades gracefully** — no crashes on malformed input, missing key, unicode oddities.
10. **CHANGELOG shows iteration with metrics** — `What changed / Why / Result / Next`, ≥4 entries.
11. **README explains the architecture and tradeoffs quickly.**

Every section of this spec maps to one of those grading axes. If you change something here, ask: which axis does the change improve, and what's the tradeoff?

---

## 23. Where each file is the source of truth

- **TASK.md** — original requirements (frozen).
- **BUILD_SPEC.md** *(this file)* — canonical design. If code drifts, fix code.
- **PLAN.md** — human-readable narrative defending each decision.
- **CHANGELOG.md** — iteration history with metrics (TASK.md §6 explicitly grades this).
- **README.md** — user-facing: quickstart, architecture diagram, tradeoffs, failure modes (TASK.md §6 requirements).
- Code — the implementation. Should match this spec exactly; PR before drifting.

---

## 24. Deep Engineering Rationale & Anti-patterns

This section captures the "Why" behind subtle design choices that distinguish this architecture from a naive RAG wrapper.

### 24.1 Advisory Lock Key Space (`64-bit hashtext`)
We use `pg_advisory_xact_lock(hashtext(scope:key))` for concurrency control.
- **Rationale**: `hashtext` in Postgres produces a 32-bit integer, but the advisory lock space is technically 64-bit (or two 32-bits). For this memory service, a 32-bit space ($2^{32} \approx 4.2$ billion) is sufficient to avoid collisions across the predicate taxonomy for a single user.
- **Failure Mode**: A collision would simply cause two *unrelated* keys (e.g. `employer` and `lives_in`) to serialize temporarily. This is a "safe" failure (performance hit of a few ms) compared to a "split-brain" race condition (data corruption).

### 24.2 Semantic Drift in Graph Hops (0.7 / 0.5 Weights)
Multi-hop recall is prone to "semantic drift" where an N-hop connection is too far removed from the original query intent.
- **Heuristic**: We capped the explicit expansion at **1-hop**.
- **Weights**: We assigned `co_extracted` a higher weight (0.7) than `same_subject` (0.5).
- **Defence**: Co-extracted facts share a conversation turn and thus a temporal context. `same_subject` is purely topical and can bridge turns months apart. By weighting co-extraction higher, we bias recall toward clusters of facts that were "thought of together" by the user.

### 24.3 Synchronous Ingest vs. WAL-based Queues
- **Current Choice**: Synchronous extraction and persistence.
- **Trade-off**: Higher latency on `POST /turns` (~3s due to LLM).
- **Senior Rationale**: For an AI Agent's memory, **Synchronous Correctness (I2)** is a P0. The agent must see the memory of what the user *just said* on the very next turn. Asynchronous background extraction (e.g. via Celery/RabbitMQ) risks a "memory gap" where the agent acts before the extraction commits.
- **Future-proofing**: The architecture separates `ExtractionService` (logic) from the `API` (interface). Scaling to high loads would involve moving to a **WAL-based queue** (like `pg_queue` or `River`) to maintain transactional atomicity while absorbing extraction latency.

---

## 25. Methodology: Spec-First Development

This project was built using the **Forward Build Specification** model. 
1. **Design Pass**: `BUILD_SPEC.md` and `PLAN.md` were authored to define the 3 Invariants (Scope, Atomicity, Gate) and the relational schema.
2. **Iteration Range**: `CHANGELOG.md` tracks the transition from v0.1 to v0.12.
3. **Drift Enforcement**: The specification acts as a "shared brain" for any modifications. Any code change not reflected in the spec is considered a bug. 

This approach demonstrates an engineering culture that values planning and architectural integrity over ad-hoc implementation.

If two of these disagree: source of truth order is `TASK.md > BUILD_SPEC.md > PLAN.md > README.md > CHANGELOG.md > code`. Anything below disagrees with anything above → fix the lower one.
