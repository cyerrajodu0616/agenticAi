"""Task-queue helpers: pending escalations + drafts, and escalation resolution."""
from assistant.db.client import get_connection
from assistant.kb import kb_learn


def list_open() -> dict:
    with get_connection() as conn:
        escalations = conn.execute(
            "SELECT id, sender, left(question_text, 80), created_at"
            " FROM agent_escalations WHERE status='pending' ORDER BY id"
        ).fetchall()
        drafts = conn.execute(
            "SELECT id, payload->>'sender', left(payload->>'question', 80), created_at"
            " FROM review_items WHERE status='pending' AND kind='reply' ORDER BY id"
        ).fetchall()
        # Peer-submitted KB entries (gated in app.py's /api/teach/confirm) — full
        # question/answer, not truncated, since approving requires reading both in full.
        pending_kb_entries = conn.execute(
            "SELECT id, payload->>'question', payload->>'answer', created_at"
            " FROM review_items WHERE status='pending' AND kind='kb_entry' ORDER BY id"
        ).fetchall()
    return {
        "escalations": escalations,
        "drafts": drafts,
        "pending_kb_entries": pending_kb_entries,
    }


def get_escalation(esc_id: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT id, sender, question_text, status FROM agent_escalations WHERE id=%s",
            (esc_id,),
        ).fetchone()
    if r is None:
        return None
    return {"id": r[0], "sender": r[1], "question_text": r[2], "status": r[3]}


def resolve_escalation(esc_id: int, resolution_text: str, resolved_by: str = "chat") -> bool:
    esc = get_escalation(esc_id)
    if esc is None or esc["status"] != "pending":
        return False
    # Learn FIRST: if embedding fails, the escalation stays pending and resolve is retryable.
    kb_learn(
        question=esc["question_text"],
        answer=resolution_text,
        created_by=resolved_by,
        source_refs=[f"escalation:{esc_id}"],
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE agent_escalations SET status='resolved', resolution_text=%s,"
            " resolved_by=%s, resolved_at=now() WHERE id=%s AND status='pending'",
            (resolution_text, resolved_by, esc_id),
        )
    return True
