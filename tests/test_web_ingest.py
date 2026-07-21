import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from assistant.web.app import app

    return TestClient(app)


def test_ingest_empty_text_rejected(client):
    resp = client.post("/api/ingest", json={"text": "   "})
    assert resp.status_code == 400


def test_ingest_calls_ingest_text_and_returns_result(client, monkeypatch):
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "ingest_text",
        lambda text, source="web-import": captured.update(text=text, source=source)
        or {"status": "escalated", "escalation_id": 5, "raw_document_id": 9},
    )
    resp = client.post("/api/ingest", json={"text": "a real question"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "escalated", "escalation_id": 5, "raw_document_id": 9}
    assert captured["text"] == "a real question"


def test_ingest_duplicate_status(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(
        web_app, "ingest_text",
        lambda text, source="web-import": {"status": "duplicate", "raw_document_id": 3},
    )
    resp = client.post("/api/ingest", json={"text": "seen before"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"
