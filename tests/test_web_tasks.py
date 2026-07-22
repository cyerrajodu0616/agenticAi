import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from assistant.web.app import app

    return TestClient(app)


def test_list_tasks_shapes_tuples_to_dicts(client, monkeypatch):
    import datetime

    import assistant.web.app as web_app

    now = datetime.datetime(2026, 7, 21, 12, 0)
    monkeypatch.setattr(
        web_app, "list_open",
        lambda: {
            "escalations": [(1, "peer@corp.com", "How rerun sync?", now)],
            "drafts": [(2, "peer@corp.com", "Where's the PDF?", now)],
            "pending_kb_entries": [(3, "What are the product ids?", "4127, 511801", now)],
        },
    )
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["escalations"] == [
        {"id": 1, "sender": "peer@corp.com", "question": "How rerun sync?",
         "created_at": now.isoformat()}
    ]
    assert body["drafts"][0]["id"] == 2
    assert body["pending_kb_entries"] == [
        {"id": 3, "question": "What are the product ids?", "answer": "4127, 511801",
         "created_at": now.isoformat()}
    ]


def test_review_show_missing_returns_404(client, monkeypatch):
    import assistant.web.app as web_app

    def raise_exit(item_id):
        raise SystemExit(f"no review item {item_id}")

    monkeypatch.setattr(web_app, "review_show", raise_exit)
    resp = client.get("/api/review/999")
    assert resp.status_code == 404


def test_review_approve_writes_and_returns_final_text(client, monkeypatch):
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "review_approve",
        lambda item_id, edited_text=None: captured.update(
            id=item_id, edited_text=edited_text
        )
        or {"final_text": "the final text"},
    )
    resp = client.post("/api/review/3/approve", json={"edited_text": "the final text"})
    assert resp.status_code == 200
    assert resp.json() == {"final_text": "the final text"}
    assert captured == {"id": 3, "edited_text": "the final text"}


def test_review_approve_already_resolved_returns_409(client, monkeypatch):
    import assistant.web.app as web_app

    def raise_exit(item_id, edited_text=None):
        raise SystemExit(f"item {item_id} is already approved")

    monkeypatch.setattr(web_app, "review_approve", raise_exit)
    resp = client.post("/api/review/3/approve", json={})
    assert resp.status_code == 409


def test_review_approve_missing_item_returns_404(client, monkeypatch):
    import assistant.web.app as web_app

    def raise_exit(item_id, edited_text=None):
        raise SystemExit(f"no review item {item_id}")

    monkeypatch.setattr(web_app, "review_approve", raise_exit)
    resp = client.post("/api/review/999/approve", json={})
    assert resp.status_code == 404


def test_review_reject_not_pending_returns_409(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "review_reject", lambda item_id: False)
    resp = client.post("/api/review/3/reject")
    assert resp.status_code == 409


def test_draft_resolution_is_read_only(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(
        web_app, "get_escalation",
        lambda i: {"id": 9, "sender": "p", "question_text": "How rerun sync?", "status": "pending"},
    )
    resolved = []
    monkeypatch.setattr(web_app, "resolve_escalation", lambda *a, **kw: resolved.append(1))
    monkeypatch.setattr(web_app, "extract_resolution", lambda text, q: "Run resync.")
    resp = client.post("/api/escalation/9/draft-resolution", json={"text": "run resync please"})
    assert resp.status_code == 200
    assert resp.json() == {"resolution": "Run resync."}
    assert resolved == []


def test_resolve_escalation_endpoint(client, monkeypatch):
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "resolve_escalation",
        lambda esc_id, text, resolved_by="web": captured.update(
            id=esc_id, text=text, by=resolved_by
        )
        or True,
    )
    resp = client.post("/api/escalation/9/resolve", json={"resolution_text": "Run resync."})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured == {"id": 9, "text": "Run resync.", "by": "web"}
