import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from assistant.web.app import app

    return TestClient(app)


def test_list_kb_no_query_uses_recent(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(
        web_app, "kb_list_recent",
        lambda limit=50: [{"id": 1, "question": "Q?", "answer": "A"}],
    )
    resp = client.get("/api/kb")
    assert resp.status_code == 200
    assert resp.json() == {"entries": [{"id": 1, "question": "Q?", "answer": "A"}]}


def test_list_kb_with_query_searches(client, monkeypatch):
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "kb_find",
        lambda text, **kw: captured.update(text=text) or [
            {"id": 2, "question": "Q2?", "answer": "A2", "similarity": 0.6}
        ],
    )
    resp = client.get("/api/kb?q=deploy+window")
    assert resp.status_code == 200
    assert resp.json()["entries"][0]["id"] == 2
    assert captured["text"] == "deploy window"


def test_patch_kb_updates(client, monkeypatch):
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "kb_update",
        lambda entry_id, question=None, answer=None: captured.update(
            id=entry_id, question=question, answer=answer
        )
        or True,
    )
    resp = client.patch("/api/kb/7", json={"answer": "new answer"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured == {"id": 7, "question": None, "answer": "new answer"}


def test_patch_kb_missing_returns_404(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "kb_update", lambda entry_id, **kw: False)
    resp = client.patch("/api/kb/999", json={"answer": "x"})
    assert resp.status_code == 404


def test_delete_kb(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "kb_delete", lambda entry_id: entry_id == 7)
    ok_resp = client.delete("/api/kb/7")
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"ok": True}
    missing_resp = client.delete("/api/kb/999")
    assert missing_resp.status_code == 404
