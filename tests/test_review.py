import json

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture()
def item_id():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO review_items (kind, payload) VALUES ('reply', %s) RETURNING id",
            (
                json.dumps(
                    {"draft": "original draft", "question": "test q?", "sender": "x",
                     "source_channel": "test", "thread_id": None, "kb_sources": []}
                ),
            ),
        ).fetchone()
    yield row[0]
    with get_connection() as conn:
        conn.execute("DELETE FROM review_items WHERE id=%s", (row[0],))
        conn.execute("DELETE FROM agent_knowledge WHERE created_by='review-cli'")


def test_approve_with_edit_learns_final_text(item_id, monkeypatch):
    import assistant.review as review

    monkeypatch.setattr(review, "_to_clipboard", lambda text: None)  # no pbcopy in CI
    result = review.approve(item_id, edited_text="the corrected answer")
    assert result["final_text"] == "the corrected answer"

    from assistant.db.client import get_connection

    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()[0]
        learned = conn.execute(
            "SELECT canonical_answer FROM agent_knowledge WHERE created_by='review-cli'"
        ).fetchone()
    assert status == "approved"
    assert learned[0] == "the corrected answer"


def test_reject_marks_rejected_and_learns_nothing(item_id):
    import assistant.review as review

    review.reject(item_id)
    from assistant.db.client import get_connection

    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()[0]
        learned = conn.execute(
            "SELECT count(*) FROM agent_knowledge WHERE created_by='review-cli'"
        ).fetchone()[0]
    assert status == "rejected"
    assert learned == 0


def test_approve_already_approved_raises(item_id, monkeypatch):
    import assistant.review as review

    monkeypatch.setattr(review, "_to_clipboard", lambda text: None)  # no pbcopy in CI
    review.approve(item_id, edited_text="first approval")
    with pytest.raises(SystemExit):
        review.approve(item_id, edited_text="second approval")


def test_reject_already_approved_is_noop(item_id, monkeypatch):
    import assistant.review as review

    monkeypatch.setattr(review, "_to_clipboard", lambda text: None)  # no pbcopy in CI
    review.approve(item_id, edited_text="the approved answer")

    from assistant.db.client import get_connection

    review.reject(item_id)
    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()[0]
    assert status == "approved"
