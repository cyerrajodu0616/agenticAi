# Claude Code Prompt: LangGraph Personal RCA/Q&A Assistant — MVP Scaffold

Paste this into Claude Code inside the `agenticAi` repo root.

---

## Context

Repo `agenticAi` (Python 3.12, `uv`/pyproject-managed) currently has: `langchain`, `langchain-anthropic`, `langchain-groq`, `langchain-google-genai`, `python-dotenv` installed, a working `.env` with `GROQ_API_KEY`, `GOOGLE_API_KEY`, `CLAUDE_API_KEY`, and exploratory notebooks under `langchain/` (no production code yet — `main.py` is a placeholder).

Goal: scaffold the first 3 build steps of a personal assistant that answers recurring RCA/how-to questions from peers/helpdesk, using LangGraph. No channel adapters yet (Teams/email come later) — this pass is: package setup, Postgres+pgvector schema, model factory + PII redaction stub, and a runnable LangGraph graph skeleton driven by a hardcoded question list for testing.

Do not invent real business logic for `get_consent_pdf` or any other structured-lookup tool — the real table/column names for the eConsent/HIPAA PDF lookup are not yet confirmed. Build it as a clearly-marked stub that raises `NotImplementedError` with a comment explaining what needs to be filled in once the schema is confirmed.

## Task 1 — Dependencies

Add to `pyproject.toml` `dependencies`:
- `langgraph>=0.6`
- `langgraph-checkpoint-postgres`
- `psycopg[binary]>=3.2`
- `pgvector>=0.3`

Add the same to `requirements.txt` for parity with the existing file.

## Task 2 — Project layout

Create this structure under a new `assistant/` package (not inside `langchain/`, which stays as scratch notebooks):

```
assistant/
  __init__.py
  config.py          # env loading, ENV=dev|prod switch
  models.py          # get_model(role, env) factory
  redact.py          # PII redaction stub (regex-based to start)
  db/
    __init__.py
    schema.sql        # DDL for agent_knowledge + agent_escalations
    client.py         # psycopg connection helper, reads DATABASE_URL from env
  tools/
    __init__.py
    consent_lookup.py # get_consent_pdf(arc_id) stub tool
  graph.py            # LangGraph graph definition (nodes below)
  run_local.py        # CLI entrypoint: feeds a hardcoded list of test questions through the graph
```

## Task 3 — `assistant/config.py`

- Load `.env` via `python-dotenv`.
- Export `ENV = os.getenv("ENV", "dev")`.
- Export `DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/agent_assistant")`.
- Validate on import that required keys for the current `ENV` are present (dev needs `GROQ_API_KEY` or `GOOGLE_API_KEY`; prod needs `CLAUDE_API_KEY`), raise a clear `RuntimeError` naming the missing var if not.

## Task 4 — `assistant/models.py`

```python
def get_model(role: str, env: str | None = None):
    """
    role: "classify" | "compose"
    env defaults to config.ENV
    dev  -> groq:qwen/qwen3-32b for classify, google_genai:gemini-2.5-flash for compose
    prod -> anthropic:claude-haiku-4-5-20251001 for both roles
    Uses langchain's init_chat_model under the hood (already used in langchain/Messages.ipynb).
    """
```
Raise `ValueError` on an unrecognized role — no silent fallback.

## Task 5 — `assistant/redact.py`

A `redact(text: str) -> tuple[str, dict[str, str]]` function that:
- Masks SSN-like patterns (`\d{3}-\d{2}-\d{4}`), email addresses, and phone numbers with placeholders (`[REDACTED_SSN_1]`, etc.).
- Returns the redacted text plus a mapping placeholder -> original, kept in memory only (never persisted).
- Add a docstring flagging this is a first-pass regex approach and should be reviewed before handling real PII traffic — not a substitute for a proper PII/NER library at scale.

## Task 6 — `assistant/db/schema.sql`

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agent_knowledge (
    id BIGSERIAL PRIMARY KEY,
    canonical_question TEXT NOT NULL,
    canonical_answer TEXT NOT NULL,
    question_embedding VECTOR(768),
    tags TEXT[] DEFAULT '{}',
    source_refs TEXT[] DEFAULT '{}',
    created_by TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    hit_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_knowledge_embedding_idx
    ON agent_knowledge USING ivfflat (question_embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS agent_escalations (
    id BIGSERIAL PRIMARY KEY,
    source_channel TEXT NOT NULL,
    thread_id TEXT,
    sender TEXT NOT NULL,
    question_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved')),
    resolution_text TEXT,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);
```
(embedding dimension 768 assumes a Gemini/Voyage-class embedding model — flag this as configurable, not hardcoded, if Claude Code knows the real embedding model dimension will differ.)

`assistant/db/client.py`: a `get_connection()` using `psycopg.connect(DATABASE_URL)`, and an `init_schema()` that executes `schema.sql`.

## Task 7 — `assistant/tools/consent_lookup.py`

```python
def get_consent_pdf(arc_id: str) -> dict:
    """
    STUB — do not treat as working. The real table/column that stores the
    consent/HIPAA PDF reference for a given arcId has NOT been confirmed yet
    (checked: add-allapps-column and arcenter-engine skill docs only document
    consentDetails/signatureDetails as staging tables with timestamp/decision
    columns — no file-path/S3-key column is documented). Before implementing:
    1. Query the actual consentDetails/signatureDetails schema for arcId
       ARCF25344h646 and confirm which column (if any) holds a path/URL/key.
    2. Confirm the S3/Blob bucket or container name and key pattern.
    Only then replace this stub with a real psycopg query + S3/Blob presigned
    URL generation.
    """
    raise NotImplementedError(
        "consentDetails/signatureDetails schema not yet confirmed — see docstring"
    )
```

## Task 8 — `assistant/graph.py`

Build a LangGraph `StateGraph` with a `TypedDict` state containing at least: `raw_text`, `redacted_text`, `pii_map`, `intent`, `tool_result`, `kb_match`, `draft_answer`, `needs_human_review`, `final_answer`.

Nodes, in this order, matching the plan doc:
1. `ingest` — passthrough for now (real channel parsing comes later), just wraps the input string into state.
2. `redact_pii` — calls `redact()` from Task 5.
3. `classify_intent` — calls `get_model("classify")`, prompts it to output one of `structured_lookup | kb_semantic_search | escalate` given the redacted text. Use structured output (the repo already uses `.with_structured_output` in `langchain/Messages.ipynb` — follow that pattern with a small Pydantic model for the intent).
4. Conditional edge on `intent` to one of:
   - `structured_lookup` — calls `get_consent_pdf` (will raise `NotImplementedError` for now — catch it and route to `escalate` on failure).
   - `kb_semantic_search` — stub that queries `agent_knowledge` via pgvector cosine distance; if no row above a `SIMILARITY_THRESHOLD = 0.85` (module constant), route to `escalate`.
   - `escalate` — inserts a row into `agent_escalations` with `status='pending'`, sets `needs_human_review=True`, and ends that branch (no auto-reply).
5. `compose_response` — only reached from `structured_lookup`/`kb_semantic_search` success — calls `get_model("compose")` to draft `final_answer` grounded only in `tool_result`/`kb_match`, never in `raw_text`.
6. `human_review_gate` — stub node that currently always sets `needs_human_review=True` (safe default) with a comment that this becomes configurable per question-type later.

Wire nodes with `add_node`/`add_conditional_edges` per LangGraph's standard pattern. Compile with `MemorySaver` checkpointer for now (Postgres checkpointer swap is a later step, not this pass).

## Task 9 — `assistant/run_local.py`

A CLI script that:
- Loads the graph from Task 8.
- Runs it against a hardcoded list of 3 test questions, one of which is: `"Where can I find the eConsent/HIPAA signed PDF for arcId ARCF25344h646?"`.
- Prints the final state for each, including whether it was escalated or auto-answered.
- No real DB required to run this end-to-end — if `DATABASE_URL` isn't reachable, `kb_semantic_search` and the escalation insert should catch the connection error and print a clear "DB not configured, skipping" message rather than crashing, so this is runnable before Postgres is set up.

## Acceptance check

After scaffolding, running `python -m assistant.run_local` (or `uv run python -m assistant.run_local`) should complete without unhandled exceptions and print a per-question trace showing intent classification, and for the consent-PDF question specifically, show it landing in `escalate` (since `get_consent_pdf` is still a stub).

## Explicitly out of scope for this pass

Teams/Email/ticketing channel adapters, real `get_consent_pdf` implementation, Postgres checkpointer, human_review_gate becoming configurable, and the `learn` node that upserts resolved escalations into `agent_knowledge`. These are the next prompt file, once the DB schema for consent PDFs is confirmed and a channel is chosen.
