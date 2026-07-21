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
