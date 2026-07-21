"""Turn a raw piece of text (uploaded or pasted in the web UI) into a
processed question — reuses the exact same pipeline run_local.py uses.

Ordering is deliberate: build_graph() runs BEFORE the raw_documents insert.
If the graph call raises, nothing is recorded, so retrying the same text
re-runs the graph instead of falsely reporting "duplicate" — same rule as
tasks.py's resolve_escalation (risky step first, durable record only after
it succeeds).
"""
import hashlib

from assistant.db.client import get_connection
from assistant.graph import build_graph
from assistant.redact import redact


def _hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode()).hexdigest()


def ingest_text(raw_text: str, source: str = "web-import") -> dict:
    file_hash = _hash(raw_text)
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM raw_documents WHERE file_hash=%s", (file_hash,)
        ).fetchone()
    if existing is not None:
        return {"status": "duplicate", "raw_document_id": existing[0]}

    out = build_graph().invoke(
        {"raw_text": raw_text, "source_channel": source, "sender": "unknown", "thread_id": None}
    )

    redacted, _ = redact(raw_text)
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO raw_documents (source, sender, thread_id, body, file_hash)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (source, "unknown", None, redacted, file_hash),
        ).fetchone()

    if out.get("escalation_id"):
        return {"status": "escalated", "escalation_id": out["escalation_id"], "raw_document_id": row[0]}
    return {"status": "drafted", "review_item_id": out["review_item_id"], "raw_document_id": row[0]}
