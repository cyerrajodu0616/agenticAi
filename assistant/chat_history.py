"""Persistence for chat "ask" interactions -- lets a wrong answer be corrected later.
Corrections write to assistant/kb.py's kb_learn/kb_learn_pending, not here; this
module only records what was asked, answered, and retrieved."""
import json

from assistant.db.client import get_connection


def save_chat(question: str, answer: str, sources: list[dict], created_by: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO chat_history (question, answer, sources, created_by)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (question, answer, json.dumps(sources), created_by),
        ).fetchone()
    return row[0]


def get_chat(chat_id: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT id, question, answer, sources, created_by, created_at"
            " FROM chat_history WHERE id=%s",
            (chat_id,),
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r[0], "question": r[1], "answer": r[2], "sources": r[3],
        "created_by": r[4], "created_at": r[5].isoformat(),
    }


def list_recent(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question, answer, sources, created_by, created_at"
            " FROM chat_history ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0], "question": r[1], "answer": r[2], "sources": r[3],
            "created_by": r[4], "created_at": r[5].isoformat(),
        }
        for r in rows
    ]
