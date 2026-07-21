# UI-Driven Question Import — Design

**Date:** 2026-07-22 · **Extends:** the merged core loop + web UI. Status: Approved (user, this session).

## Goal

Let the user get a real incoming question (e.g. copy-pasted from Teams) into the system without a CLI or a watched folder: upload a `.txt`/`.md` file or paste text into a panel on the Chat tab, click Run, and have it processed through the exact same pipeline `run_local.py` already uses (redact → classify → answer-or-escalate), landing in Tasks.

## Non-goals

No drop-folder / filesystem watcher (superseded by this design — considered and explicitly rejected in favor of a single UI-driven path). No `.eml`/`.pdf`/`.msg` parsing — only plain text, read client-side regardless of whether it came from a file picker or a textarea. No sender/thread extraction — `sender` is `"unknown"`, matching the existing `run_local.py` convention; a real header/extraction scheme is a later addition if needed. No batch/multi-item import — one question per Run click.

## Architecture

**`assistant/ingest.py`** (new) — business logic, mirroring how `chat.py`/`kb.py`/`tasks.py` hold logic while `web/app.py` stays a thin JSON adapter:

```python
def ingest_text(raw_text: str, source: str = "web-import") -> dict:
    """
    - hash = sha256(raw_text); SELECT raw_documents WHERE file_hash=hash — a repeat
      submission of identical content returns {"status": "duplicate"} without
      calling the graph at all (dedup is content-based: two DIFFERENT senders
      submitting byte-identical wording also collapse to one entry — acceptable
      for a personal tool's low volume, worth knowing if it ever surprises)
    - if not seen before: build_graph().invoke({...}) FIRST (source_channel=source,
      sender="unknown", thread_id=None) — the risky, possibly-failing step
    - ONLY on success: redact(raw_text) and INSERT into raw_documents (source,
      sender, thread_id, body=redacted, file_hash=hash). If build_graph() raises,
      nothing is recorded, so retrying the same submission re-runs the graph
      instead of falsely reporting "duplicate" — same ordering rule as
      resolve_escalation's learn-before-mark-resolved (risky step first, durable
      record only after it succeeds)
    - returns {"status": "escalated"|"drafted"|"duplicate", ...} with enough detail
      (escalation_id or review_item_id) for the caller to link to Tasks
    """
```

Reuses `assistant.graph.build_graph()`, `assistant.redact.redact()`, and `assistant.db.client.get_connection()` — no new pipeline logic, no new tables (`raw_documents` already exists in schema.sql with exactly the needed columns: `source`, `sender`, `thread_id`, `body`, `file_hash UNIQUE`).

**`assistant/web/app.py`** — one new route:

```python
class IngestRequest(BaseModel):
    text: str

@app.post("/api/ingest")
def ingest_endpoint(req: IngestRequest) -> dict:
    if not req.text.strip():
        raise HTTPException(400, "no text provided")
    return ingest_text(req.text)
```

**Frontend** — a `<details>` panel in the Chat tab (collapsed by default), visually and semantically separate from the regular chat input since an imported item is someone else's question, not the user talking to the assistant:

```
Import a question ▾
  [Choose file (.txt/.md)]   — or —   [paste textarea]
  [Run]
  <result: "Escalated — check Tasks" / "Drafted a reply — check Tasks" / "Already processed before">
```

JS: if a file is chosen, `await file.text()` before the request; the textarea is used only if no file is picked. Either way, exactly one `POST /api/ingest {text}` call, so the backend has a single code path regardless of source.

## Error handling

Empty/whitespace-only input → 400 before anything is touched (mirrors the existing `if (!text) return` pattern already used for the chat input). `ingest_text` checks for a duplicate by `SELECT` before calling the graph, then records the `raw_documents` row only after `build_graph()` succeeds — the `file_hash UNIQUE` constraint stays as a safety net for the rare concurrent-duplicate race (a solo-user tool, so low risk), but the ordering (risky step, then durable record) is what prevents a failed run from permanently blocking retries. Graph failures (redact/classify/DB) surface as the existing global `{"error": ...}` JSON handler already in `app.py` — no new error-shaping needed.

## Testing

Unit tests for `ingest_text` mocking `build_graph`/`get_connection` (same style as `test_graph.py`'s fakes); one integration test running real text through the real pipeline + real dedup (submit twice, assert the second call returns `duplicate` without creating a second escalation). Endpoint tests for `/api/ingest` mirror the existing `test_web_chat.py` pattern (monkeypatch `ingest_text`, assert routing/status codes). One live acceptance pass: a real question through the real UI panel, confirmed it lands in Tasks; resubmitting the same text confirmed as a duplicate.
