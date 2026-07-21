import pytest

pytestmark = pytest.mark.integration

REF = "pytest:tasks"


@pytest.fixture()
def esc_id():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_escalations (source_channel, sender, question_text)"
            " VALUES ('test', 'peer@corp.com', 'How do I rerun the nightly sync?')"
            " RETURNING id"
        ).fetchone()
    yield row[0]
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_escalations WHERE id=%s", (row[0],))
        conn.execute(
            "DELETE FROM agent_knowledge WHERE %s = ANY(source_refs)",
            (f"escalation:{row[0]}",),
        )


def test_list_open_includes_new_escalation(esc_id):
    from assistant.tasks import list_open

    ids = [e[0] for e in list_open()["escalations"]]
    assert esc_id in ids


def test_resolve_learns_then_marks(esc_id):
    from assistant.db.client import get_connection
    from assistant.tasks import get_escalation, resolve_escalation

    assert resolve_escalation(esc_id, "Run scripts/nightly.sh --force", resolved_by="pytest") is True
    assert get_escalation(esc_id)["status"] == "resolved"
    with get_connection() as conn:
        learned = conn.execute(
            "SELECT canonical_answer FROM agent_knowledge WHERE %s = ANY(source_refs)",
            (f"escalation:{esc_id}",),
        ).fetchone()
    assert learned[0] == "Run scripts/nightly.sh --force"
    # second resolve must refuse
    assert resolve_escalation(esc_id, "again") is False
