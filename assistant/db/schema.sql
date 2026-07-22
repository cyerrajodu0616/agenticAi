CREATE EXTENSION IF NOT EXISTS vector;

-- Learned Q->A pairs (the assistant's growing memory). Vector dim matches EMBED_DIM (768).
CREATE TABLE IF NOT EXISTS agent_knowledge (
    id BIGSERIAL PRIMARY KEY,
    canonical_question TEXT NOT NULL,
    canonical_answer TEXT NOT NULL,
    question_embedding VECTOR(768),
    embedding_model TEXT,
    tags TEXT[] DEFAULT '{}',
    source_refs TEXT[] DEFAULT '{}',
    created_by TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    hit_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Reference material: specs, docs, curated skill content, code snippets.
CREATE TABLE IF NOT EXISTS product_knowledge (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('skill','code','spec','doc')),
    source_path TEXT NOT NULL,
    symbol TEXT,
    snippet TEXT NOT NULL,
    snippet_embedding VECTOR(768),
    embedding_model TEXT,
    last_verified_commit TEXT,
    verified_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Human-in-the-loop queue; doubles as audit log.
CREATE TABLE IF NOT EXISTS agent_escalations (
    id BIGSERIAL PRIMARY KEY,
    source_channel TEXT NOT NULL,
    thread_id TEXT,
    sender TEXT NOT NULL,
    question_text TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','resolved')),
    resolution_text TEXT,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Every ingested item's parsed text; vectors elsewhere reference this so re-embedding
-- is always possible when the embedding backend changes.
CREATE TABLE IF NOT EXISTS raw_documents (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    sender TEXT,
    thread_id TEXT,
    doc_date TIMESTAMPTZ,
    body TEXT NOT NULL,
    file_hash TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- The single approval gate: replies, actions, scripts, and peer-submitted KB
-- entries all wait here.
CREATE TABLE IF NOT EXISTS review_items (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('reply','action','script','kb_entry')),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    resolution JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- Migration for installs that predate 'kb_entry' (idempotent: safe to re-run).
ALTER TABLE review_items DROP CONSTRAINT IF EXISTS review_items_kind_check;
ALTER TABLE review_items ADD CONSTRAINT review_items_kind_check
    CHECK (kind IN ('reply','action','script','kb_entry'));
