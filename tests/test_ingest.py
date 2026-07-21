"""Integration tests — real DB and real dedup logic; build_graph is faked
(it's expensive/live-LLM and already has its own tests in test_graph.py —
these tests are about dedup and status-mapping, not the graph itself)."""
import pytest

pytestmark = pytest.mark.integration

REF_PREFIX = "pytest-ingest-"


class FakeApp:
    def __init__(self, result):
        self.result = result

    def invoke(self, state):
        return self.result


@pytest.fixture()
def clean():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    yield
    with get_connection() as conn:
        conn.execute("DELETE FROM raw_documents WHERE source LIKE %s", (REF_PREFIX + "%",))


def test_new_text_escalates_and_records(clean, monkeypatch):
    import assistant.ingest as ingest

    monkeypatch.setattr(ingest, "build_graph", lambda: FakeApp({"escalation_id": 999}))
    result = ingest.ingest_text("unique text one " + REF_PREFIX, source=REF_PREFIX + "esc")
    assert result["status"] == "escalated"
    assert result["escalation_id"] == 999
    assert result["raw_document_id"]

    from assistant.db.client import get_connection

    with get_connection() as conn:
        row = conn.execute(
            "SELECT source FROM raw_documents WHERE id=%s", (result["raw_document_id"],)
        ).fetchone()
    assert row[0] == REF_PREFIX + "esc"


def test_new_text_drafted_when_review_item_id_set(clean, monkeypatch):
    import assistant.ingest as ingest

    monkeypatch.setattr(ingest, "build_graph", lambda: FakeApp({"review_item_id": 555}))
    result = ingest.ingest_text("unique text drafted " + REF_PREFIX, source=REF_PREFIX + "draft")
    assert result == {
        "status": "drafted", "review_item_id": 555, "raw_document_id": result["raw_document_id"]
    }


def test_duplicate_text_short_circuits(clean, monkeypatch):
    import assistant.ingest as ingest

    calls = []
    monkeypatch.setattr(
        ingest, "build_graph",
        lambda: (calls.append(1), FakeApp({"escalation_id": 1}))[1],
    )
    text = "unique text two " + REF_PREFIX
    first = ingest.ingest_text(text, source=REF_PREFIX + "dup")
    assert first["status"] == "escalated"
    second = ingest.ingest_text(text, source=REF_PREFIX + "dup")
    assert second["status"] == "duplicate"
    assert len(calls) == 1  # graph only ran once


def test_graph_failure_does_not_block_retry(clean, monkeypatch):
    import assistant.ingest as ingest

    class FailingApp:
        def invoke(self, state):
            raise RuntimeError("boom")

    attempts = {"n": 0}

    def fake_build_graph():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FailingApp()
        return FakeApp({"escalation_id": 42})

    monkeypatch.setattr(ingest, "build_graph", fake_build_graph)
    text = "unique text three " + REF_PREFIX

    with pytest.raises(RuntimeError):
        ingest.ingest_text(text, source=REF_PREFIX + "retry")

    result = ingest.ingest_text(text, source=REF_PREFIX + "retry")
    assert result["status"] == "escalated"  # retry succeeded, not falsely "duplicate"


def test_concurrent_insert_race_resolves_to_existing_row(clean, monkeypatch):
    """Simulates two concurrent calls with identical text: both pass the pre-check
    dedup SELECT (neither sees the other's row yet), so both run the graph, then
    both attempt the INSERT. We drive this deterministically by pre-inserting the
    winning row (with the same file_hash) *after* the pre-check would have run but
    *before* our call's INSERT — a real Postgres unique-violation, not a mocked one.
    """
    import assistant.ingest as ingest
    from assistant.db.client import get_connection

    text = "unique text four " + REF_PREFIX
    file_hash = ingest._hash(text)

    # Seed the "winning racer's" row directly, bypassing ingest_text, so our call's
    # pre-check SELECT (which runs first, before this) still sees no existing row —
    # we insert only right as ingest_text is about to build_graph, simulating the
    # other request having already committed by the time our INSERT runs.
    real_get_connection = ingest.get_connection

    def seed_before_graph():
        with real_get_connection() as conn:
            conn.execute(
                "INSERT INTO raw_documents (source, sender, thread_id, body, file_hash)"
                " VALUES (%s, %s, %s, %s, %s)",
                (REF_PREFIX + "winner", "unknown", None, "winner body", file_hash),
            )
        return FakeApp({"escalation_id": 777})

    monkeypatch.setattr(ingest, "build_graph", seed_before_graph)

    result = ingest.ingest_text(text, source=REF_PREFIX + "race")

    # Our racer's own INSERT hit the real unique constraint and raised
    # psycopg.errors.UniqueViolation internally; ingest_text must not propagate it.
    assert result["status"] == "escalated"
    assert result["escalation_id"] == 777

    with get_connection() as conn:
        winner_row = conn.execute(
            "SELECT id, source FROM raw_documents WHERE file_hash=%s", (file_hash,)
        ).fetchone()
    # Exactly one row for this file_hash: our racer's INSERT did not create a second one.
    assert winner_row[1] == REF_PREFIX + "winner"
    assert result["raw_document_id"] == winner_row[0]


def test_stores_redacted_body_not_raw(clean, monkeypatch):
    import assistant.ingest as ingest

    monkeypatch.setattr(ingest, "build_graph", lambda: FakeApp({"escalation_id": 1}))
    text = f"contact me at ssntest@example.com about {REF_PREFIX}redact"
    result = ingest.ingest_text(text, source=REF_PREFIX + "redact")

    from assistant.db.client import get_connection

    with get_connection() as conn:
        body = conn.execute(
            "SELECT body FROM raw_documents WHERE id=%s", (result["raw_document_id"],)
        ).fetchone()[0]
    assert "ssntest@example.com" not in body
