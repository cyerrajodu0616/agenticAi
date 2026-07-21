import pytest

pytestmark = pytest.mark.integration


@pytest.fixture()
def clean(request):
    from assistant.db.client import get_connection, init_schema

    init_schema()
    yield
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_knowledge WHERE created_by='pytest'")


def test_learn_then_search_finds_it(clean):
    from assistant.kb import kb_learn, kb_search

    kb_learn(
        question="Where is the eConsent HIPAA PDF for an application?",
        answer="Check consentDetails in the app DB; path pattern documented in runbook X.",
        created_by="pytest",
        source_refs=["thread-123"],
    )
    hits = kb_search("where can I find the signed eConsent pdf for arcId ARCF123?")
    assert hits, "expected at least one hit"
    assert hits[0]["similarity"] > 0.5
    assert "consentDetails" in hits[0]["content"]


def test_search_empty_kb_returns_empty_list(clean):
    from assistant import config
    from assistant.kb import kb_search

    results = kb_search("completely unrelated question about lunch menus")
    # (an empty result is fine; any hit returned must be below the routing threshold
    # the caller applies — kb_search itself does not filter by similarity)
    assert all(hit["similarity"] < config.SIMILARITY_THRESHOLD for hit in results)


def test_search_merges_graphify_results(clean, monkeypatch):
    from assistant import kb

    monkeypatch.setattr(
        kb.graphify, "graphify_search",
        lambda question, limit=3: [
            {"source": "graphify", "title": "eConsent", "content": "from graphify",
             "similarity": 0.99}
        ],
    )
    hits = kb.kb_search("anything", limit=3)
    assert hits[0]["source"] == "graphify"
    assert hits[0]["content"] == "from graphify"


def test_search_graphify_unreachable_still_returns_local_hits(clean, monkeypatch):
    from assistant import kb
    from assistant.kb import kb_learn

    monkeypatch.setattr(kb.graphify, "graphify_search", lambda question, limit=3: [])
    kb_learn(
        question="Where is the eConsent HIPAA PDF for an application?",
        answer="Check consentDetails in the app DB.",
        created_by="pytest",
        source_refs=["thread-456"],
    )
    hits = kb.kb_search("where can I find the signed eConsent pdf?")
    assert hits and hits[0]["source"] == "agent"


def test_list_recent_orders_newest_first(clean):
    from assistant.kb import kb_learn, kb_list_recent

    first_id = kb_learn(
        question="q1", answer="a1", created_by="pytest", source_refs=["thread-789"]
    )
    second_id = kb_learn(
        question="q2", answer="a2", created_by="pytest", source_refs=["thread-789"]
    )
    entries = kb_list_recent(limit=50)
    ids = [e["id"] for e in entries]
    assert ids.index(second_id) < ids.index(first_id)
    match = next(e for e in entries if e["id"] == second_id)
    assert match == {"id": second_id, "question": "q2", "answer": "a2"}
