"""Semantic search over agent_knowledge + product_knowledge, and the learn upsert."""
from assistant import graphify
from assistant.db.client import get_connection
from assistant.models import embedding_model_name, get_embeddings


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
    local_hits = [
        {"source": r[0], "title": r[1], "content": r[2], "similarity": float(r[3])}
        for r in rows
    ]
    external_hits = graphify.graphify_search(question, limit=limit)
    merged = sorted(local_hits + external_hits, key=lambda h: h["similarity"], reverse=True)
    return merged[:limit]


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
            (question, answer, vec, embedding_model_name(), source_refs, created_by),
        ).fetchone()
    return row[0]


def kb_find(text: str, limit: int = 5) -> list[dict]:
    """Search agent_knowledge only, returning ids — for interactive edit/delete flows."""
    vec = _embed(text)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, canonical_question, canonical_answer,"
            " 1 - (question_embedding <=> %s::vector) AS similarity"
            " FROM agent_knowledge WHERE question_embedding IS NOT NULL"
            " ORDER BY question_embedding <=> %s::vector LIMIT %s",
            (vec, vec, limit),
        ).fetchall()
    return [
        {"id": r[0], "question": r[1], "answer": r[2], "similarity": float(r[3])}
        for r in rows
    ]


def kb_get(entry_id: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT id, canonical_question, canonical_answer, created_by, source_refs"
            " FROM agent_knowledge WHERE id=%s",
            (entry_id,),
        ).fetchone()
    if r is None:
        return None
    return {"id": r[0], "question": r[1], "answer": r[2], "created_by": r[3], "source_refs": r[4]}


def kb_update(entry_id: int, question: str | None = None, answer: str | None = None) -> bool:
    from assistant.models import embedding_model_name

    current = kb_get(entry_id)
    if current is None:
        return False
    new_q = question if question is not None else current["question"]
    new_a = answer if answer is not None else current["answer"]
    with get_connection() as conn:
        if question is not None:
            vec = _embed(new_q)  # question changed -> must re-embed
            conn.execute(
                "UPDATE agent_knowledge SET canonical_question=%s, canonical_answer=%s,"
                " question_embedding=%s, embedding_model=%s WHERE id=%s",
                (new_q, new_a, vec, embedding_model_name(), entry_id),
            )
        else:
            conn.execute(
                "UPDATE agent_knowledge SET canonical_answer=%s WHERE id=%s",
                (new_a, entry_id),
            )
    return True


def kb_delete(entry_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM agent_knowledge WHERE id=%s", (entry_id,))
        return cur.rowcount == 1
