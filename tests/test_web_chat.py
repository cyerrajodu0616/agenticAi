"""Unit tests — chat.py/kb.py/tasks.py functions faked, no real network/DB/LLM."""
import pytest
from fastapi.testclient import TestClient


class FakeIntent:
    def __init__(self, action, ref_id=None, reasoning="fake"):
        self.action = action
        self.ref_id = ref_id
        self.reasoning = reasoning


class FakePair:
    def __init__(self, question, answer):
        self.question = question
        self.answer = answer


@pytest.fixture()
def client():
    from assistant.web.app import app

    return TestClient(app)


def test_chat_ask_returns_answer(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("ask"))
    monkeypatch.setattr(web_app, "answer_from_kb", lambda text: "the answer, cited")
    resp = client.post("/api/chat", json={"text": "when is the deploy window?"})
    assert resp.status_code == 200
    assert resp.json() == {"action": "ask", "answer": "the answer, cited"}


def test_chat_teach_returns_proposal_without_writing(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("teach"))
    monkeypatch.setattr(
        web_app, "extract_teach_pair", lambda text: FakePair("Q?", "A")
    )
    learned = []
    monkeypatch.setattr(web_app, "kb_learn", lambda **kw: learned.append(kw) or 1)
    resp = client.post("/api/chat", json={"text": "remember that..."})
    assert resp.status_code == 200
    assert resp.json() == {"action": "teach", "question": "Q?", "answer": "A"}
    assert learned == []  # proposal only, no write


def test_chat_edit_kb_returns_matches(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("edit_kb"))
    monkeypatch.setattr(
        web_app, "kb_find",
        lambda text, **kw: [{"id": 5, "question": "Q?", "answer": "A", "similarity": 0.7}],
    )
    resp = client.post("/api/chat", json={"text": "that answer is wrong"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "edit_kb"
    assert body["matches"][0]["id"] == 5


def test_chat_resolve_without_ref_id_errors(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("resolve", ref_id=None))
    resp = client.post("/api/chat", json={"text": "here's the answer"})
    assert resp.status_code == 200
    assert resp.json() == {"action": "resolve", "error": "no escalation id given"}


def test_chat_resolve_with_ref_id_returns_draft(client, monkeypatch):
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("resolve", ref_id=9))
    monkeypatch.setattr(
        web_app, "get_escalation",
        lambda i: {"id": 9, "sender": "p", "question_text": "How rerun sync?", "status": "pending"},
    )
    monkeypatch.setattr(web_app, "extract_resolution", lambda text, q: "Run resync.")
    resp = client.post("/api/chat", json={"text": "tell them to run resync"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "resolve", "escalation_id": 9,
        "question": "How rerun sync?", "resolution": "Run resync.",
    }


def test_chat_unhandled_exception_returns_json_error(monkeypatch):
    import assistant.web.app as web_app

    def _boom(text):
        raise ValueError("boom")

    # classify_chat failures are now recovered from (see test below), so exercise
    # the generic unhandled-exception handler via a different, unprotected call.
    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("ask"))
    monkeypatch.setattr(web_app, "answer_from_kb", _boom)
    # raise_server_exceptions=False: let the app's own exception handler produce
    # the response instead of the test client re-raising the original exception.
    no_raise_client = TestClient(web_app.app, raise_server_exceptions=False)
    resp = no_raise_client.post("/api/chat", json={"text": "trigger a crash"})
    assert resp.status_code == 500
    body = resp.json()
    assert body == {"error": "internal server error"}
    assert "boom" not in body["error"]  # exception detail must not reach the client


def test_chat_classify_failure_returns_friendly_message(client, monkeypatch):
    """Groq's structured-output quirk (literal "None" instead of null for
    ChatIntent.ref_id) makes classify_chat raise sometimes — chat_endpoint must
    recover the same way assistant.chat.run_repl already does, instead of a 500.
    """
    import assistant.web.app as web_app

    def _boom(text):
        raise ValueError("boom")

    monkeypatch.setattr(web_app, "classify_chat", _boom)
    resp = client.post("/api/chat", json={"text": "some ask-type question"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "other",
        "message": "Sorry, I couldn't understand that — try rephrasing.",
    }


def test_teach_confirm_writes(client, monkeypatch):
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "kb_learn", lambda **kw: captured.update(kw) or 42
    )
    resp = client.post("/api/teach/confirm", json={"question": "Q?", "answer": "A"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 42}
    assert captured["question"] == "Q?"
    assert captured["answer"] == "A"
    assert captured["created_by"] == "web"
