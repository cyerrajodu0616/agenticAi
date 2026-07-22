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


def test_peer_kb_entry_gated_then_approved_writes_to_kb():
    """Full real flow: kb_learn_pending queues it (no agent_knowledge row yet),
    list_open() surfaces it, review.approve() is what actually calls kb_learn."""
    from assistant.db.client import get_connection, init_schema
    from assistant.kb import kb_learn_pending
    from assistant.review import approve
    from assistant.tasks import list_open

    init_schema()
    item_id = kb_learn_pending(
        question=f"{REF} what is the answer?", answer=f"{REF} the answer is 42"
    )
    try:
        with get_connection() as conn:
            not_yet = conn.execute(
                "SELECT id FROM agent_knowledge WHERE canonical_question=%s",
                (f"{REF} what is the answer?",),
            ).fetchone()
        assert not_yet is None, "must not be in agent_knowledge before approval"

        pending_ids = [k[0] for k in list_open()["pending_kb_entries"]]
        assert item_id in pending_ids

        result = approve(item_id)
        assert result["final_text"] == f"{REF} the answer is 42"

        with get_connection() as conn:
            learned = conn.execute(
                "SELECT canonical_answer, created_by FROM agent_knowledge"
                " WHERE canonical_question=%s",
                (f"{REF} what is the answer?",),
            ).fetchone()
        assert learned == (f"{REF} the answer is 42", "peer-approved")
    finally:
        with get_connection() as conn:
            conn.execute("DELETE FROM review_items WHERE id=%s", (item_id,))
            conn.execute(
                "DELETE FROM agent_knowledge WHERE canonical_question=%s",
                (f"{REF} what is the answer?",),
            )


def test_peer_kb_entry_rejected_never_reaches_kb():
    from assistant.db.client import get_connection, init_schema
    from assistant.kb import kb_learn_pending
    from assistant.review import reject

    init_schema()
    item_id = kb_learn_pending(question=f"{REF} rejected q?", answer=f"{REF} rejected a")
    try:
        assert reject(item_id) is True
        with get_connection() as conn:
            never_learned = conn.execute(
                "SELECT id FROM agent_knowledge WHERE canonical_question=%s",
                (f"{REF} rejected q?",),
            ).fetchone()
        assert never_learned is None
    finally:
        with get_connection() as conn:
            conn.execute("DELETE FROM review_items WHERE id=%s", (item_id,))
