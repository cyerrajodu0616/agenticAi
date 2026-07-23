"""Unit tests — real Postgres (docker compose), no LLM calls."""
import pytest

pytestmark = pytest.mark.integration

REF_ANSWER = "Wednesdays 6pm ET [agent:deploy window]"
SOURCES = [{"source": "agent", "title": "deploy window", "content": "Wed 6pm", "similarity": 0.9}]


@pytest.fixture()
def clean():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    yield
    with get_connection() as conn:
        conn.execute("DELETE FROM chat_history WHERE question = %s", ("when is the deploy window?",))


def test_save_and_get_chat(clean):
    from assistant.chat_history import get_chat, save_chat

    chat_id = save_chat(
        question="when is the deploy window?", answer=REF_ANSWER,
        sources=SOURCES, created_by="local",
    )
    row = get_chat(chat_id)
    assert row["question"] == "when is the deploy window?"
    assert row["answer"] == REF_ANSWER
    assert row["sources"] == SOURCES
    assert row["created_by"] == "local"
    assert row["created_at"]  # a real timestamp string


def test_get_chat_missing_returns_none(clean):
    from assistant.chat_history import get_chat

    assert get_chat(999999999) is None


def test_list_recent_orders_newest_first(clean):
    from assistant.chat_history import list_recent, save_chat

    first_id = save_chat(question="when is the deploy window?", answer="a1", sources=[], created_by="local")
    second_id = save_chat(question="when is the deploy window?", answer="a2", sources=[], created_by="peer")
    rows = list_recent(limit=5)
    ids = [r["id"] for r in rows]
    assert ids.index(second_id) < ids.index(first_id)
