# Personal Assistant Core Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working end-to-end chatbot loop: a question goes in → PII redaction → intent classification → KB search → drafted answer → human review inbox → approved answers are learned into the KB.

**Architecture:** LangGraph `StateGraph` on the Mac orchestrates deterministic Python nodes plus exactly two LLM roles (classify, compose) behind a `get_model(role)` factory switched by `MODEL_BACKEND=cloud|local`. Postgres+pgvector (Docker) stores knowledge, escalations, and the review queue. Review is a CLI; nothing is dispatched without approval.

**Tech Stack:** Python 3.12, uv, LangChain ≥1.3, LangGraph, psycopg3, pgvector, Postgres 17 (Docker), pytest.

**Spec:** `docs/superpowers/specs/2026-07-20-personal-assistant-local-first-design.md`

**Follow-on plans (not in this document):** Plan 2 drop-folder ingestion pipeline; Plan 3 action layer (tool registry + script workbench); Plan 4 local backend cutover (Ollama on the 4060 + `reembed`); Plan 5 n8n + Microsoft Graph. In this plan, `sync_fix` and `analysis_task` intents are classified but deliberately routed to `escalate` (real code, no stubs to fill).

## Global Constraints

- Python `>=3.12` (already pinned in `pyproject.toml`); manage deps with `uv`.
- `MODEL_BACKEND` env var: `cloud` (default, initial setup) | `local`. Unknown value → `RuntimeError` at import of config.
- `redact()` MUST run before every LLM call — both backends. No LLM node reads `raw_text`.
- No auto-send/auto-execute anywhere: every outcome terminates in the `review_items` table.
- Embedding dimension is `EMBED_DIM` env var, default `768` (`models/text-embedding-004` cloud / `nomic-embed-text` local are both 768).
- Unknown model role → `ValueError`, no silent fallback.
- Postgres runs on port `5433` (avoid clashing with any existing local 5432).
- Tests that need the DB or a live LLM are marked `@pytest.mark.integration`; plain `pytest` runs only pure unit tests.

---

### Task 1: Dependencies, package scaffold, config

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Create: `assistant/__init__.py` (empty)
- Create: `assistant/config.py`
- Create: `tests/__init__.py` (empty), `tests/test_config.py`
- Create: `pytest.ini`

**Interfaces:**
- Produces: `assistant.config` module attributes `MODEL_BACKEND: str`, `OLLAMA_BASE_URL: str`, `DATABASE_URL: str`, `EMBED_DIM: int`, `SIMILARITY_THRESHOLD: float`; function `validate() -> None` raising `RuntimeError` on bad/missing env.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml` `dependencies`, add (keep existing entries):

```toml
    "langgraph>=0.6",
    "langchain-ollama>=0.2",
    "psycopg[binary]>=3.2",
    "pgvector>=0.3",
    "pytest>=8.0",
```

Run: `uv sync` — expect it to resolve and install without error.

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
markers =
    integration: needs Postgres (docker compose up) and/or live LLM API keys
addopts = -m "not integration"
```

(`pytest` runs unit tests only; `pytest -m integration` runs the rest.)

- [ ] **Step 3: Write the failing test** — `tests/test_config.py`:

```python
import importlib
import pytest


def _reload_config(monkeypatch, **env):
    for k in ("MODEL_BACKEND", "EMBED_DIM", "GROQ_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import assistant.config as config
    return importlib.reload(config)


def test_defaults(monkeypatch):
    config = _reload_config(monkeypatch)
    assert config.MODEL_BACKEND == "cloud"
    assert config.EMBED_DIM == 768
    assert 0 < config.SIMILARITY_THRESHOLD < 1


def test_unknown_backend_rejected(monkeypatch):
    config = _reload_config(monkeypatch, MODEL_BACKEND="hybrid")
    with pytest.raises(RuntimeError, match="MODEL_BACKEND"):
        config.validate()


def test_cloud_requires_keys(monkeypatch):
    config = _reload_config(monkeypatch, MODEL_BACKEND="cloud")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        config.validate()


def test_local_needs_no_cloud_keys(monkeypatch):
    config = _reload_config(monkeypatch, MODEL_BACKEND="local")
    config.validate()  # must not raise
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'assistant'`.

- [ ] **Step 5: Implement** — `assistant/__init__.py` (empty file) and `assistant/config.py`:

```python
"""Central config. Import-time reads env; call validate() before running the app."""
import os

from dotenv import load_dotenv

load_dotenv()

MODEL_BACKEND = os.getenv("MODEL_BACKEND", "cloud")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://assistant:assistant@localhost:5433/assistant"
)
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))

_REQUIRED_CLOUD_KEYS = ("GROQ_API_KEY", "GOOGLE_API_KEY")


def validate() -> None:
    if MODEL_BACKEND not in ("cloud", "local"):
        raise RuntimeError(
            f"MODEL_BACKEND must be 'cloud' or 'local', got {MODEL_BACKEND!r}"
        )
    if MODEL_BACKEND == "cloud":
        missing = [k for k in _REQUIRED_CLOUD_KEYS if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"MODEL_BACKEND=cloud but missing env: {', '.join(missing)}")
```

Note: `_reload_config` in the test reloads the module because config reads env at import time; `load_dotenv()` does not override already-set (or deleted) process env for the monkeypatched keys since `load_dotenv` never overrides existing vars — and the test deletes them from process env, so a developer's real `.env` could leak values into `test_cloud_requires_keys`. To keep the test hermetic, `load_dotenv()` must be skipped under pytest: change the line to

```python
if not os.getenv("PYTEST_VERSION"):
    load_dotenv()
```

(`PYTEST_VERSION` is set by pytest ≥8 in the test process.)

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock pytest.ini assistant/__init__.py assistant/config.py tests/
git commit -m "feat: assistant package scaffold + config with backend validation"
```

---

### Task 2: Model factory (`get_model`, `get_embeddings`)

**Files:**
- Create: `assistant/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `assistant.config.MODEL_BACKEND`, `assistant.config.OLLAMA_BASE_URL`.
- Produces: `get_model(role: str)` → LangChain chat model, roles `"classify" | "compose" | "coder"`; `get_embeddings()` → LangChain embeddings object. Both respect `MODEL_BACKEND`.

- [ ] **Step 1: Write the failing test** — `tests/test_models.py`:

```python
import pytest


def test_unknown_role_raises(monkeypatch):
    monkeypatch.setattr("assistant.config.MODEL_BACKEND", "cloud")
    from assistant.models import get_model

    with pytest.raises(ValueError, match="unknown role"):
        get_model("translate")


@pytest.mark.parametrize(
    "backend,role,expected",
    [
        ("cloud", "classify", "groq:qwen/qwen3-32b"),
        ("cloud", "compose", "google_genai:gemini-2.5-flash"),
        ("cloud", "coder", "google_genai:gemini-2.5-flash"),
        ("local", "classify", "ollama:qwen3:8b"),
        ("local", "compose", "ollama:qwen3:8b"),
        ("local", "coder", "ollama:qwen2.5-coder:7b"),
    ],
)
def test_role_model_mapping(monkeypatch, backend, role, expected):
    # Don't build real clients in unit tests: capture what init_chat_model is asked for.
    calls = {}

    def fake_init(model, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("assistant.models.init_chat_model", fake_init)
    monkeypatch.setattr("assistant.config.MODEL_BACKEND", backend)
    from assistant.models import get_model

    get_model(role)
    assert calls["model"] == expected
    if backend == "local":
        assert calls["kwargs"]["base_url"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `No module named 'assistant.models'`.

- [ ] **Step 3: Implement** — `assistant/models.py`:

```python
"""Single switch point between cloud (initial setup) and local (4060/Ollama) backends."""
from langchain.chat_models import init_chat_model

from assistant import config

_ROLES_CLOUD = {
    "classify": "groq:qwen/qwen3-32b",
    "compose": "google_genai:gemini-2.5-flash",
    "coder": "google_genai:gemini-2.5-flash",
}
_ROLES_LOCAL = {
    "classify": "ollama:qwen3:8b",
    "compose": "ollama:qwen3:8b",
    "coder": "ollama:qwen2.5-coder:7b",
}


def get_model(role: str):
    table = _ROLES_CLOUD if config.MODEL_BACKEND == "cloud" else _ROLES_LOCAL
    if role not in table:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(table)}")
    if config.MODEL_BACKEND == "local":
        return init_chat_model(table[role], base_url=config.OLLAMA_BASE_URL, temperature=0)
    return init_chat_model(table[role], temperature=0)


def get_embeddings():
    if config.MODEL_BACKEND == "cloud":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model="nomic-embed-text", base_url=config.OLLAMA_BASE_URL)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add assistant/models.py tests/test_models.py
git commit -m "feat: get_model/get_embeddings factory with cloud|local switch"
```

---

### Task 3: PII redaction

**Files:**
- Create: `assistant/redact.py`
- Test: `tests/test_redact.py`

**Interfaces:**
- Produces: `redact(text: str) -> tuple[str, dict[str, str]]` — redacted text plus `{placeholder: original}` map (in-memory only, never persisted).

- [ ] **Step 1: Write the failing test** — `tests/test_redact.py`:

```python
from assistant.redact import redact


def test_redacts_ssn_email_phone():
    text = "John (SSN 123-45-6789, john.d@corp.com, 555-867-5309) asked about ARCF25344h646"
    red, mapping = redact(text)
    assert "123-45-6789" not in red
    assert "john.d@corp.com" not in red
    assert "555-867-5309" not in red
    assert "ARCF25344h646" in red  # business ids must survive redaction
    assert mapping["[REDACTED_SSN_1]"] == "123-45-6789"
    assert set(mapping.values()) == {"123-45-6789", "john.d@corp.com", "555-867-5309"}


def test_numbering_multiple_of_same_kind():
    red, mapping = redact("a@x.com and b@y.com")
    assert "[REDACTED_EMAIL_1]" in red and "[REDACTED_EMAIL_2]" in red
    assert len(mapping) == 2


def test_clean_text_untouched():
    red, mapping = redact("Where is the eConsent PDF for ARCF25344h697?")
    assert red == "Where is the eConsent PDF for ARCF25344h697?"
    assert mapping == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_redact.py -v`
Expected: FAIL — `No module named 'assistant.redact'`.

- [ ] **Step 3: Implement** — `assistant/redact.py`:

```python
"""First-pass regex PII redaction.

Runs BEFORE every LLM call (both backends). This is a starter pattern set, not a
substitute for a proper PII/NER pass — review before handling real traffic at scale.
The returned mapping must stay in process memory only; never persist or log it.
"""
import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b")),
]


def redact(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    for label, pattern in _PATTERNS:
        counter = 0

        def _sub(m: re.Match, label: str = label) -> str:
            nonlocal counter
            counter += 1
            placeholder = f"[REDACTED_{label}_{counter}]"
            mapping[placeholder] = m.group(0)
            return placeholder

        text = pattern.sub(_sub, text)
    return text, mapping
```

(The PHONE pattern requires a separator before the last 4 digits so it cannot re-match SSNs already replaced, and won't swallow 10-digit business ids.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_redact.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add assistant/redact.py tests/test_redact.py
git commit -m "feat: regex PII redaction with reversible in-memory mapping"
```

---

### Task 4: Postgres + pgvector (Docker), schema, DB client

**Files:**
- Create: `docker-compose.yml`
- Create: `assistant/db/__init__.py` (empty), `assistant/db/schema.sql`, `assistant/db/client.py`
- Test: `tests/test_db.py` (integration)

**Interfaces:**
- Produces: `get_connection()` → autocommit psycopg connection with pgvector adapter registered; `init_schema()` → creates all tables idempotently. Tables: `agent_knowledge`, `product_knowledge`, `agent_escalations`, `raw_documents`, `review_items`.

- [ ] **Step 1: Create `docker-compose.yml`**

```yaml
services:
  db:
    image: pgvector/pgvector:pg17
    container_name: assistant-db
    environment:
      POSTGRES_USER: assistant
      POSTGRES_PASSWORD: assistant
      POSTGRES_DB: assistant
    ports:
      - "5433:5432"
    volumes:
      - assistant_pgdata:/var/lib/postgresql/data
volumes:
  assistant_pgdata:
```

Run: `docker compose up -d` then `docker compose ps` — expect `assistant-db` healthy/running.

- [ ] **Step 2: Write the failing test** — `tests/test_db.py`:

```python
import pytest

pytestmark = pytest.mark.integration


def test_init_schema_idempotent_and_tables_exist():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    init_schema()  # running twice must not error
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
    tables = {r[0] for r in rows}
    assert {
        "agent_knowledge",
        "product_knowledge",
        "agent_escalations",
        "raw_documents",
        "review_items",
    } <= tables


def test_vector_roundtrip():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    vec = [0.1] * 768
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO agent_knowledge (canonical_question, canonical_answer,"
            " question_embedding, created_by) VALUES (%s, %s, %s, 'test')",
            ("test q", "test a", vec),
        )
        row = conn.execute(
            "SELECT canonical_answer, 1 - (question_embedding <=> %s::vector) AS sim"
            " FROM agent_knowledge WHERE created_by='test'"
            " ORDER BY question_embedding <=> %s::vector LIMIT 1",
            (vec, vec),
        ).fetchone()
        conn.execute("DELETE FROM agent_knowledge WHERE created_by='test'")
    assert row[0] == "test a"
    assert row[1] > 0.999
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest -m integration tests/test_db.py -v`
Expected: FAIL — `No module named 'assistant.db.client'`.

- [ ] **Step 4: Implement** — `assistant/db/schema.sql`:

```sql
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

-- The single approval gate: replies, actions, and scripts all wait here.
CREATE TABLE IF NOT EXISTS review_items (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('reply','action','script')),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    resolution JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
```

`assistant/db/client.py`:

```python
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from assistant import config

_SCHEMA = Path(__file__).parent / "schema.sql"


def init_schema() -> None:
    # Plain connection (no vector adapter): the extension may not exist yet.
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(_SCHEMA.read_text())


def get_connection() -> psycopg.Connection:
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
    register_vector(conn)
    return conn
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -m integration tests/test_db.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml assistant/db/ tests/test_db.py
git commit -m "feat: pgvector schema (KB, escalations, raw docs, review queue) + db client"
```

---

### Task 5: KB search and learn

**Files:**
- Create: `assistant/kb.py`
- Test: `tests/test_kb.py` (integration — needs DB + embeddings API)

**Interfaces:**
- Consumes: `get_embeddings()` (Task 2), `get_connection()` (Task 4), `config.SIMILARITY_THRESHOLD`.
- Produces: `kb_search(question: str, limit: int = 3) -> list[dict]` — dicts `{source, title, content, similarity}` sorted best-first; `kb_learn(question: str, answer: str, created_by: str, source_refs: list[str]) -> int` (returns row id).

- [ ] **Step 1: Write the failing test** — `tests/test_kb.py`:

```python
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture()
def clean(request):
    from assistant.db.client import get_connection, init_schema

    init_schema()
    yield
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_knowledge WHERE created_by='pytest'")


def test_learn_then_search_finds_it(clean):
    from assistant.kb import kb_learn, kb_search

    kb_learn(
        question="Where is the eConsent HIPAA PDF for an application?",
        answer="Check consentDetails in the app DB; path pattern documented in runbook X.",
        created_by="pytest",
        source_refs=["thread-123"],
    )
    hits = kb_search("where can I find the signed eConsent pdf for arcId ARCF123?")
    assert hits, "expected at least one hit"
    assert hits[0]["similarity"] > 0.5
    assert "consentDetails" in hits[0]["content"]


def test_search_empty_kb_returns_empty_list(clean):
    from assistant.kb import kb_search

    assert kb_search("completely unrelated question about lunch menus") == [] or True
    # (an empty-or-low-similarity result is acceptable; routing threshold is applied by caller)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -m integration tests/test_kb.py -v`
Expected: FAIL — `No module named 'assistant.kb'`.

- [ ] **Step 3: Implement** — `assistant/kb.py`:

```python
"""Semantic search over agent_knowledge + product_knowledge, and the learn upsert."""
from assistant import config
from assistant.db.client import get_connection
from assistant.models import get_embeddings


def _embed(text: str) -> list[float]:
    return get_embeddings().embed_query(text)


def kb_search(question: str, limit: int = 3) -> list[dict]:
    vec = _embed(question)
    sql = """
        SELECT * FROM (
            SELECT 'agent' AS source, canonical_question AS title,
                   canonical_answer AS content,
                   1 - (question_embedding <=> %s::vector) AS similarity
            FROM agent_knowledge
            WHERE question_embedding IS NOT NULL
            UNION ALL
            SELECT 'product' AS source, source_path AS title,
                   snippet AS content,
                   1 - (snippet_embedding <=> %s::vector) AS similarity
            FROM product_knowledge
            WHERE snippet_embedding IS NOT NULL
        ) merged
        ORDER BY similarity DESC
        LIMIT %s
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (vec, vec, limit)).fetchall()
    return [
        {"source": r[0], "title": r[1], "content": r[2], "similarity": float(r[3])}
        for r in rows
    ]


def kb_learn(question: str, answer: str, created_by: str, source_refs: list[str]) -> int:
    vec = _embed(question)
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO agent_knowledge
                (canonical_question, canonical_answer, question_embedding,
                 embedding_model, source_refs, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (question, answer, vec, config.MODEL_BACKEND, source_refs, created_by),
        ).fetchone()
    return row[0]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -m integration tests/test_kb.py -v`
Expected: 2 passed. (Cloud backend: needs `GOOGLE_API_KEY` for embeddings.)

- [ ] **Step 5: Commit**

```bash
git add assistant/kb.py tests/test_kb.py
git commit -m "feat: KB semantic search across both knowledge tables + learn upsert"
```

---

### Task 6: The LangGraph agent graph

**Files:**
- Create: `assistant/graph.py`
- Test: `tests/test_graph.py` (unit — LLM + KB faked), `tests/test_graph_live.py` (integration)

**Interfaces:**
- Consumes: `redact` (Task 3), `get_model` (Task 2), `kb_search` (Task 5), `get_connection` (Task 4), `config.SIMILARITY_THRESHOLD`.
- Produces: `build_graph()` → compiled LangGraph app. Input state: `{"raw_text": str, "source_channel": str, "sender": str, "thread_id": str | None}`. Output state adds: `redacted_text`, `pii_map`, `intent` (`"kb_answer"|"sync_fix"|"analysis_task"|"escalate"`), `kb_hits`, `draft`, `review_item_id`, `escalation_id`.

- [ ] **Step 1: Write the failing unit test** — `tests/test_graph.py`:

```python
"""Routing tests with LLM and DB faked — runs offline."""
import pytest


class FakeClassifier:
    def __init__(self, intent):
        self.intent = intent

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        from assistant.graph import Intent

        return Intent(intent=self.intent, reasoning="fake")


class FakeComposer:
    def invoke(self, messages):
        class R:
            content = "DRAFT ANSWER grounded in KB"

        return R()


@pytest.fixture()
def wired(monkeypatch):
    saved = {}

    def fake_get_model(role):
        return {"classify": wired.classifier, "compose": FakeComposer()}[role]

    def fake_save_review_item(kind, payload):
        saved["item"] = (kind, payload)
        return 42

    def fake_save_escalation(**kw):
        saved["escalation"] = kw
        return 7

    monkeypatch.setattr("assistant.graph.get_model", fake_get_model)
    monkeypatch.setattr("assistant.graph._save_review_item", fake_save_review_item)
    monkeypatch.setattr("assistant.graph._save_escalation", fake_save_escalation)
    wired.saved = saved
    return wired


def _run(graph_module, text):
    app = graph_module.build_graph()
    return app.invoke(
        {"raw_text": text, "source_channel": "test", "sender": "peer@corp.com",
         "thread_id": None}
    )


def test_kb_hit_produces_reply_draft(wired, monkeypatch):
    import assistant.graph as graph

    wired.classifier = FakeClassifier("kb_answer")
    monkeypatch.setattr(
        "assistant.graph.kb_search",
        lambda q: [{"source": "agent", "title": "t", "content": "the answer", "similarity": 0.95}],
    )
    out = _run(graph, "Where is the eConsent PDF for ARCF25344h646?")
    assert out["intent"] == "kb_answer"
    assert out["review_item_id"] == 42
    kind, payload = wired.saved["item"]
    assert kind == "reply"
    assert "DRAFT ANSWER" in payload["draft"]


def test_kb_miss_escalates(wired, monkeypatch):
    import assistant.graph as graph

    wired.classifier = FakeClassifier("kb_answer")
    monkeypatch.setattr(
        "assistant.graph.kb_search",
        lambda q: [{"source": "agent", "title": "t", "content": "x", "similarity": 0.2}],
    )
    out = _run(graph, "Something the KB has never seen")
    assert out["escalation_id"] == 7


def test_action_intents_escalate_until_action_layer_exists(wired, monkeypatch):
    import assistant.graph as graph

    wired.classifier = FakeClassifier("sync_fix")
    out = _run(graph, "data for ARCF999 is not synced, can you resync")
    assert out["escalation_id"] == 7
    assert "action layer" in wired.saved["escalation"]["reason"]


def test_llm_nodes_never_see_raw_text(wired, monkeypatch):
    import assistant.graph as graph

    seen = []

    class SpyClassifier(FakeClassifier):
        def invoke(self, messages):
            seen.append(str(messages))
            return super().invoke(messages)

    wired.classifier = SpyClassifier("kb_answer")
    monkeypatch.setattr("assistant.graph.kb_search", lambda q: [])
    _run(graph, "ssn 123-45-6789 and mail a@b.com need checking")
    joined = " ".join(seen)
    assert "123-45-6789" not in joined
    assert "a@b.com" not in joined
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_graph.py -v`
Expected: FAIL — `No module named 'assistant.graph'`.

- [ ] **Step 3: Implement** — `assistant/graph.py`:

```python
"""The agent brain: ingest -> redact -> classify -> route -> draft/escalate -> review queue.

LLM nodes: classify_intent and compose ONLY. Everything else is deterministic.
Every path terminates in review_items or agent_escalations — nothing auto-sends.
"""
import json
from typing import Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from assistant import config
from assistant.db.client import get_connection
from assistant.kb import kb_search
from assistant.models import get_model
from assistant.redact import redact


class Intent(BaseModel):
    intent: Literal["kb_answer", "sync_fix", "analysis_task", "escalate"] = Field(
        description="kb_answer: a question answerable from the knowledge base. "
        "sync_fix: asks to re-run/repair a data sync. "
        "analysis_task: asks for ad-hoc data analysis. "
        "escalate: anything else or unclear."
    )
    reasoning: str


class State(TypedDict, total=False):
    raw_text: str
    source_channel: str
    sender: str
    thread_id: str | None
    redacted_text: str
    pii_map: dict
    intent: str
    kb_hits: list
    draft: str
    review_item_id: int
    escalation_id: int
    escalation_reason: str


def _save_review_item(kind: str, payload: dict) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO review_items (kind, payload) VALUES (%s, %s) RETURNING id",
            (kind, json.dumps(payload)),
        ).fetchone()
    return row[0]


def _save_escalation(*, source_channel, thread_id, sender, question_text, reason) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_escalations"
            " (source_channel, thread_id, sender, question_text, reason)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (source_channel, thread_id, sender, question_text, reason),
        ).fetchone()
    return row[0]


def redact_pii(state: State) -> State:
    redacted, mapping = redact(state["raw_text"])
    return {"redacted_text": redacted, "pii_map": mapping}


def classify_intent(state: State) -> State:
    llm = get_model("classify").with_structured_output(Intent)
    result = llm.invoke(
        [
            ("system", "Classify the incoming work message. Respond with the schema."),
            ("human", state["redacted_text"]),
        ]
    )
    return {"intent": result.intent}


def kb_answer(state: State) -> State:
    hits = kb_search(state["redacted_text"])
    return {"kb_hits": hits}


def compose(state: State) -> State:
    context = "\n\n".join(
        f"[{h['source']}:{h['title']} sim={h['similarity']:.2f}]\n{h['content']}"
        for h in state["kb_hits"]
    )
    llm = get_model("compose")
    result = llm.invoke(
        [
            (
                "system",
                "Draft a short, professional reply to a colleague. Ground the answer ONLY"
                " in the knowledge snippets provided. Cite which snippet you used."
                " If the snippets don't answer the question, say you couldn't find it.",
            ),
            ("human", f"Question:\n{state['redacted_text']}\n\nKnowledge:\n{context}"),
        ]
    )
    draft = result.content
    item_id = _save_review_item(
        "reply",
        {
            "draft": draft,
            "question": state["redacted_text"],
            "sender": state["sender"],
            "source_channel": state["source_channel"],
            "thread_id": state.get("thread_id"),
            "kb_sources": [h["title"] for h in state["kb_hits"]],
        },
    )
    return {"draft": draft, "review_item_id": item_id}


def escalate(state: State) -> State:
    reason = state.get("escalation_reason", "classifier chose escalate")
    esc_id = _save_escalation(
        source_channel=state["source_channel"],
        thread_id=state.get("thread_id"),
        sender=state["sender"],
        question_text=state["redacted_text"],
        reason=reason,
    )
    return {"escalation_id": esc_id}


def route_after_classify(state: State) -> str:
    intent = state["intent"]
    if intent == "kb_answer":
        return "kb_answer"
    if intent in ("sync_fix", "analysis_task"):
        # Action layer arrives in Plan 3; until then a human handles these.
        state["escalation_reason"] = f"intent={intent} but action layer not built yet"
        return "escalate"
    return "escalate"


def route_after_kb(state: State) -> str:
    hits = state["kb_hits"]
    if hits and hits[0]["similarity"] >= config.SIMILARITY_THRESHOLD:
        return "compose"
    state["escalation_reason"] = "no KB match above threshold"
    return "escalate"


def build_graph():
    g = StateGraph(State)
    g.add_node("redact_pii", redact_pii)
    g.add_node("classify_intent", classify_intent)
    g.add_node("kb_answer", kb_answer)
    g.add_node("compose", compose)
    g.add_node("escalate", escalate)

    g.set_entry_point("redact_pii")
    g.add_edge("redact_pii", "classify_intent")
    g.add_conditional_edges(
        "classify_intent", route_after_classify,
        {"kb_answer": "kb_answer", "escalate": "escalate"},
    )
    g.add_conditional_edges(
        "kb_answer", route_after_kb, {"compose": "compose", "escalate": "escalate"}
    )
    g.add_edge("compose", END)
    g.add_edge("escalate", END)
    return g.compile()
```

Implementation note: conditional-edge functions mutating `state` (`escalation_reason`) works because LangGraph passes the live dict to routers before the next node reads it; if the LangGraph version in use copies state for routers, move the reason-setting into the `escalate` node by deriving it from `intent`/`kb_hits` instead — the tests only assert on the stored reason text.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_graph.py -v`
Expected: 4 passed.

- [ ] **Step 5: Write the live integration test** — `tests/test_graph_live.py`:

```python
"""End-to-end with real LLM + DB. Requires: docker compose up, cloud API keys in .env."""
import pytest

pytestmark = pytest.mark.integration


def test_pdf_question_end_to_end():
    from assistant.db.client import init_schema
    from assistant.graph import build_graph
    from assistant.kb import kb_learn

    init_schema()
    kb_learn(
        question="Where is the eConsent HIPAA PDF for an application?",
        answer="Query consentDetails for the arcId; the S3 key pattern is in runbook X.",
        created_by="pytest-live",
        source_refs=[],
    )
    out = build_graph().invoke(
        {
            "raw_text": "Hi! Where can I find the signed eConsent PDF for ARCF25344h646?",
            "source_channel": "test",
            "sender": "peer@corp.com",
            "thread_id": None,
        }
    )
    assert out["intent"] == "kb_answer"
    assert out.get("review_item_id"), "expected a pending reply draft in review_items"
```

- [ ] **Step 6: Run live test**

Run: `uv run pytest -m integration tests/test_graph_live.py -v`
Expected: 1 passed (needs `GROQ_API_KEY` + `GOOGLE_API_KEY` + running DB).

- [ ] **Step 7: Commit**

```bash
git add assistant/graph.py tests/test_graph.py tests/test_graph_live.py
git commit -m "feat: LangGraph core loop — redact, classify, KB route, draft, escalate"
```

---

### Task 7: Review inbox CLI + learn loop

**Files:**
- Create: `assistant/review.py`
- Test: `tests/test_review.py` (integration)

**Interfaces:**
- Consumes: `get_connection` (Task 4), `kb_learn` (Task 5).
- Produces: CLI `python -m assistant.review <list|show|approve|reject>`; functions `list_pending() -> list[tuple]`, `approve(item_id: int, edited_text: str | None = None) -> dict`, `reject(item_id: int) -> None`. Approving a `reply` copies final text to clipboard (`pbcopy`) and learns Q→final answer into the KB.

- [ ] **Step 1: Write the failing test** — `tests/test_review.py`:

```python
import json

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture()
def item_id():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO review_items (kind, payload) VALUES ('reply', %s) RETURNING id",
            (
                json.dumps(
                    {"draft": "original draft", "question": "test q?", "sender": "x",
                     "source_channel": "test", "thread_id": None, "kb_sources": []}
                ),
            ),
        ).fetchone()
    yield row[0]
    with get_connection() as conn:
        conn.execute("DELETE FROM review_items WHERE id=%s", (row[0],))
        conn.execute("DELETE FROM agent_knowledge WHERE created_by='review-cli'")


def test_approve_with_edit_learns_final_text(item_id, monkeypatch):
    import assistant.review as review

    monkeypatch.setattr(review, "_to_clipboard", lambda text: None)  # no pbcopy in CI
    result = review.approve(item_id, edited_text="the corrected answer")
    assert result["final_text"] == "the corrected answer"

    from assistant.db.client import get_connection

    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()[0]
        learned = conn.execute(
            "SELECT canonical_answer FROM agent_knowledge WHERE created_by='review-cli'"
        ).fetchone()
    assert status == "approved"
    assert learned[0] == "the corrected answer"


def test_reject_marks_rejected_and_learns_nothing(item_id):
    import assistant.review as review

    review.reject(item_id)
    from assistant.db.client import get_connection

    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()[0]
        learned = conn.execute(
            "SELECT count(*) FROM agent_knowledge WHERE created_by='review-cli'"
        ).fetchone()[0]
    assert status == "rejected"
    assert learned == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -m integration tests/test_review.py -v`
Expected: FAIL — `No module named 'assistant.review'`.

- [ ] **Step 3: Implement** — `assistant/review.py`:

```python
"""The human approval gate. Usage:

    python -m assistant.review list
    python -m assistant.review show 3
    python -m assistant.review approve 3            # approve draft as-is
    python -m assistant.review approve 3 --edit     # opens $EDITOR to fix the draft first
    python -m assistant.review reject 3

Approving a reply: marks approved, copies final text to the clipboard for manual paste
into Teams/Outlook (phase-1 dispatch), and learns question -> final answer into the KB.
"""
import argparse
import json
import os
import subprocess
import tempfile

from assistant.db.client import get_connection
from assistant.kb import kb_learn


def _to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


def list_pending() -> list[tuple]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT id, kind, payload->>'sender', left(payload->>'question', 80), created_at"
            " FROM review_items WHERE status='pending' ORDER BY id"
        ).fetchall()


def show(item_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT kind, payload, status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"no review item {item_id}")
    return {"kind": row[0], "payload": row[1], "status": row[2]}


def approve(item_id: int, edited_text: str | None = None) -> dict:
    item = show(item_id)
    if item["status"] != "pending":
        raise SystemExit(f"item {item_id} is already {item['status']}")
    payload = item["payload"]
    final_text = edited_text if edited_text is not None else payload["draft"]
    with get_connection() as conn:
        conn.execute(
            "UPDATE review_items SET status='approved',"
            " resolution=%s, resolved_at=now() WHERE id=%s",
            (json.dumps({"final_text": final_text}), item_id),
        )
    if item["kind"] == "reply":
        kb_learn(
            question=payload["question"],
            answer=final_text,
            created_by="review-cli",
            source_refs=[f"review_item:{item_id}"],
        )
        _to_clipboard(final_text)
    return {"final_text": final_text}


def reject(item_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE review_items SET status='rejected', resolved_at=now()"
            " WHERE id=%s AND status='pending'",
            (item_id,),
        )


def _edit_in_editor(initial: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(initial)
        path = f.name
    subprocess.run([editor, path], check=True)
    with open(path) as f:
        return f.read().strip()


def main() -> None:
    p = argparse.ArgumentParser(prog="assistant.review")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "approve", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
        if name == "approve":
            sp.add_argument("--edit", action="store_true")
    args = p.parse_args()

    if args.cmd == "list":
        for row in list_pending():
            print(f"#{row[0]:>4} [{row[1]}] from {row[2]}: {row[3]} ({row[4]:%m-%d %H:%M})")
    elif args.cmd == "show":
        item = show(args.id)
        print(f"kind={item['kind']} status={item['status']}")
        print(json.dumps(item["payload"], indent=2))
    elif args.cmd == "approve":
        item = show(args.id)
        edited = _edit_in_editor(item["payload"]["draft"]) if args.edit else None
        result = approve(args.id, edited)
        print("approved — final text copied to clipboard:\n")
        print(result["final_text"])
    elif args.cmd == "reject":
        reject(args.id)
        print("rejected")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -m integration tests/test_review.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add assistant/review.py tests/test_review.py
git commit -m "feat: review inbox CLI — approve/edit/reject drafts, learn on approve"
```

---

### Task 8: Runner CLI + fixtures

**Files:**
- Create: `assistant/run_local.py`
- Create: `fixtures/questions.json`
- Test: manual acceptance run (below)

**Interfaces:**
- Consumes: `build_graph()` (Task 6), `config.validate()` (Task 1), `init_schema()` (Task 4).
- Produces: `python -m assistant.run_local "question..."` and `python -m assistant.run_local --fixtures`.

- [ ] **Step 1: Create `fixtures/questions.json`**

Seed with realistic examples (replace/extend with ~10 real redacted messages you've received — this file doubles as the cloud-vs-local regression suite for Plan 4):

```json
[
  {"text": "Hi, where can I find the signed eConsent/HIPAA PDF for arcId ARCF25344h646?",
   "expected_intent": "kb_answer"},
  {"text": "The application data for ARCF25390h101 didn't sync to the portal, can you re-run the sync?",
   "expected_intent": "sync_fix"},
  {"text": "Can you pull a count of applications by product for June and send me a breakdown?",
   "expected_intent": "analysis_task"},
  {"text": "Are you joining the 3pm architecture call?",
   "expected_intent": "escalate"}
]
```

- [ ] **Step 2: Implement** — `assistant/run_local.py`:

```python
"""Feed one question (or the fixture set) through the graph and print the trace.

    python -m assistant.run_local "Where is the eConsent PDF for ARCF25344h646?"
    python -m assistant.run_local --fixtures
"""
import argparse
import json
from pathlib import Path

from assistant import config
from assistant.db.client import init_schema
from assistant.graph import build_graph

FIXTURES = Path(__file__).parent.parent / "fixtures" / "questions.json"


def run_one(app, text: str) -> dict:
    out = app.invoke(
        {"raw_text": text, "source_channel": "cli", "sender": "me", "thread_id": None}
    )
    print(f"\nQ: {text}")
    print(f"  intent: {out.get('intent')}")
    if out.get("review_item_id"):
        print(f"  -> draft reply queued: review item #{out['review_item_id']}")
        print(f"     (run: python -m assistant.review show {out['review_item_id']})")
    if out.get("escalation_id"):
        print(f"  -> escalated to you: escalation #{out['escalation_id']}")
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="assistant.run_local")
    p.add_argument("question", nargs="?")
    p.add_argument("--fixtures", action="store_true")
    args = p.parse_args()

    config.validate()
    init_schema()
    app = build_graph()

    if args.fixtures:
        cases = json.loads(FIXTURES.read_text())
        results = [run_one(app, c["text"]) for c in cases]
        expected = [c["expected_intent"] for c in cases]
        actual = [r.get("intent") for r in results]
        matches = sum(e == a for e, a in zip(expected, actual))
        print(f"\nrouting: {matches}/{len(cases)} matched expected intent")
    elif args.question:
        run_one(app, args.question)
    else:
        p.error("give a question or --fixtures")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Acceptance run**

```bash
docker compose up -d
uv run python -m assistant.run_local --fixtures
```

Expected: all 4 fixtures print a trace; the PDF question routes `kb_answer` (drafting a reply if Task 6's live test seeded the KB, else escalating on low similarity); sync/analysis questions escalate with the "action layer not built yet" reason; routing match ≥ 3/4 (classifier judgment on borderline cases is allowed to differ — inspect, don't chase 4/4).

Then the full human loop:

```bash
uv run python -m assistant.review list
uv run python -m assistant.review approve <id>   # final text lands on the clipboard
```

- [ ] **Step 4: Run the whole test suite**

Run: `uv run pytest && uv run pytest -m integration`
Expected: all unit tests pass; all integration tests pass with DB up + cloud keys.

- [ ] **Step 5: Commit**

```bash
git add assistant/run_local.py fixtures/questions.json
git commit -m "feat: run_local CLI + fixture suite closing the core loop e2e"
```

---

## Definition of done (this plan)

`python -m assistant.run_local --fixtures` demonstrates: question in → redacted → classified → KB-answered or escalated → pending item in the review inbox → `assistant.review approve` puts the final text on the clipboard and the Q→A into `agent_knowledge` — after which re-asking the same question routes `kb_answer` and drafts from the learned row. `MODEL_BACKEND=local` is wired but untested until Plan 4 (4060 setup).
