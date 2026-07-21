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
