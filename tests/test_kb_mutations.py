import pytest

pytestmark = pytest.mark.integration

REF = "pytest:kb-mutations"


@pytest.fixture()
def entry_id():
    from assistant.db.client import get_connection, init_schema
    from assistant.kb import kb_learn

    init_schema()
    eid = kb_learn(
        question="What is the deploy window for portal releases?",
        answer="Wednesdays 6pm ET.",
        created_by="pytest",
        source_refs=[REF],
    )
    yield eid
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_knowledge WHERE %s = ANY(source_refs)", (REF,))


def test_find_get_roundtrip(entry_id):
    from assistant.kb import kb_find, kb_get

    hits = kb_find("when can I deploy the portal?")
    assert any(h["id"] == entry_id for h in hits)
    got = kb_get(entry_id)
    assert got["answer"] == "Wednesdays 6pm ET."
    assert kb_get(99999999) is None


def test_update_answer_only_keeps_embedding(entry_id):
    from assistant.db.client import get_connection
    from assistant.kb import kb_get, kb_update

    with get_connection() as conn:
        before = conn.execute(
            "SELECT question_embedding::text FROM agent_knowledge WHERE id=%s", (entry_id,)
        ).fetchone()[0]
    assert kb_update(entry_id, answer="Thursdays 6pm ET.") is True
    with get_connection() as conn:
        after = conn.execute(
            "SELECT question_embedding::text FROM agent_knowledge WHERE id=%s", (entry_id,)
        ).fetchone()[0]
    assert kb_get(entry_id)["answer"] == "Thursdays 6pm ET."
    assert before == after  # answer-only update must not re-embed


def test_update_question_reembeds(entry_id):
    from assistant.db.client import get_connection
    from assistant.kb import kb_update

    with assistant_embedding_snapshot(entry_id) as before:
        assert kb_update(entry_id, question="What is the portal release window?") is True
    with assistant_embedding_snapshot(entry_id) as after:
        pass
    assert before != after


from contextlib import contextmanager


@contextmanager
def assistant_embedding_snapshot(entry_id):
    from assistant.db.client import get_connection

    with get_connection() as conn:
        yield conn.execute(
            "SELECT question_embedding::text FROM agent_knowledge WHERE id=%s", (entry_id,)
        ).fetchone()[0]


def test_delete(entry_id):
    from assistant.kb import kb_delete, kb_get

    assert kb_delete(entry_id) is True
    assert kb_get(entry_id) is None
    assert kb_delete(entry_id) is False
