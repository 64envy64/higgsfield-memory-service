-- Memory service: initial schema.
--
-- Design notes:
--   * scope_type/scope_id replaces a single user_id column. Lets us scope memories
--     either to a user (cross-session for that user) or to a session (anonymous mode
--     when user_id is null), enforcing Invariant 1 (scope isolation) by construction.
--   * embedding is vector(1536) always. When OPENAI_API_KEY is missing the column
--     stays NULL and vector retrievers no-op; lexical FTS carries retrieval.
--   * supersedes uses ON DELETE SET NULL so deleting a turn/session doesn't orphan
--     downstream memory history.
--   * source_turn FK is also SET NULL — explicit memory cleanup is driven by
--     source_session, not by turn deletion cascade.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- =============================================================================
-- turns: the raw conversation log (source of truth, immutable)
-- =============================================================================
CREATE TABLE IF NOT EXISTS turns (
    id              UUID PRIMARY KEY,
    session_id      TEXT NOT NULL,
    user_id         TEXT,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('user', 'session')),
    scope_id        TEXT NOT NULL,
    messages        JSONB NOT NULL,
    full_text       TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding       vector(1536),
    tsv             TSVECTOR
);

CREATE INDEX IF NOT EXISTS turns_scope_idx
    ON turns (scope_type, scope_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS turns_session_idx
    ON turns (session_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS turns_user_idx
    ON turns (user_id, timestamp DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS turns_tsv_idx
    ON turns USING GIN (tsv);
-- HNSW for vector. Tolerates NULLs (they're simply not indexed).
CREATE INDEX IF NOT EXISTS turns_embedding_idx
    ON turns USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE OR REPLACE FUNCTION turns_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english', coalesce(NEW.full_text, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS turns_tsv_update ON turns;
CREATE TRIGGER turns_tsv_update
    BEFORE INSERT OR UPDATE OF full_text ON turns
    FOR EACH ROW EXECUTE FUNCTION turns_tsv_trigger();

-- =============================================================================
-- memories: extracted, typed, queryable knowledge
-- =============================================================================
CREATE TABLE IF NOT EXISTS memories (
    id              UUID PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('user', 'session')),
    scope_id        TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('fact', 'preference', 'opinion', 'event')),
    subject         TEXT NOT NULL,                          -- "user", "pet:Biscuit"
    predicate       TEXT NOT NULL,                          -- "employer", "lives_in", "owns_pet"
    object          TEXT NOT NULL,                          -- "Notion", "Berlin"
    key             TEXT GENERATED ALWAYS AS
                       (subject || '::' || predicate) STORED,
    value           TEXT NOT NULL,                          -- human-readable summary
    raw_quote       TEXT,                                   -- quote from source message
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

CREATE INDEX IF NOT EXISTS memories_scope_active_idx
    ON memories (scope_type, scope_id, active);
CREATE INDEX IF NOT EXISTS memories_scope_key_active_idx
    ON memories (scope_type, scope_id, key, active);
CREATE INDEX IF NOT EXISTS memories_source_session_idx
    ON memories (source_session);
CREATE INDEX IF NOT EXISTS memories_tsv_idx
    ON memories USING GIN (tsv);
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
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_tsv_update ON memories;
CREATE TRIGGER memories_tsv_update
    BEFORE INSERT OR UPDATE OF value, subject, predicate, object, raw_quote
    ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_tsv_trigger();

-- =============================================================================
-- entities: named entities mentioned across memories. Anchors for multi-hop.
-- =============================================================================
CREATE TABLE IF NOT EXISTS entities (
    id              UUID PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('user', 'session')),
    scope_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    type            TEXT,                                   -- 'person','pet','place','org','other'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS entities_unique_idx
    ON entities (scope_type, scope_id, lower(name), coalesce(type, ''));
CREATE INDEX IF NOT EXISTS entities_lookup_idx
    ON entities (scope_type, scope_id, lower(name));

CREATE TABLE IF NOT EXISTS memory_entity_mentions (
    memory_id       UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id       UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, entity_id)
);

CREATE INDEX IF NOT EXISTS mem_entity_by_entity_idx
    ON memory_entity_mentions (entity_id);

-- =============================================================================
-- memory_edges: cheap graph layer for 1-hop traversal during recall.
-- Edge weight lets us decay neighbour contribution in fusion.
-- =============================================================================
CREATE TABLE IF NOT EXISTS memory_edges (
    src_memory      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    dst_memory      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,                          -- 'same_subject','mentions_entity','co_extracted'
    weight          REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src_memory, dst_memory, relation)
);

CREATE INDEX IF NOT EXISTS edges_src_idx ON memory_edges (src_memory);
CREATE INDEX IF NOT EXISTS edges_dst_idx ON memory_edges (dst_memory);

-- =============================================================================
-- migration bookkeeping (single-version is fine per spec §12, but recorded)
-- =============================================================================
CREATE TABLE IF NOT EXISTS _schema_meta (
    version   INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO _schema_meta (version) VALUES (1) ON CONFLICT DO NOTHING;
