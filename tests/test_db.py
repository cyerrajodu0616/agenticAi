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
