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

    def _boom(**kwargs):
        raise ValueError("boom")

    # classify_chat/answer_from_kb/extract_teach_pair/extract_resolution failures
    # are all now recovered from locally (see the tests around this one), so exercise
    # the generic unhandled-exception handler via a still-unprotected write path.
    monkeypatch.setattr(web_app, "kb_learn", _boom)
    # raise_server_exceptions=False: let the app's own exception handler produce
    # the response instead of the test client re-raising the original exception.
    # client=("127.0.0.1", ...): local -> hits kb_learn, not the peer gate.
    no_raise_client = TestClient(
        web_app.app, raise_server_exceptions=False, client=("127.0.0.1", 12345)
    )
    resp = no_raise_client.post(
        "/api/teach/confirm", json={"question": "Q?", "answer": "A"},
    )
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


def test_chat_teach_extraction_failure_returns_friendly_message(client, monkeypatch):
    """A second, distinct Groq structured-output failure (empty failed_generation,
    not the ref_id issue) was found live in extract_teach_pair — same recovery."""
    import assistant.web.app as web_app

    def _boom(text):
        raise ValueError("boom")

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("teach"))
    monkeypatch.setattr(web_app, "extract_teach_pair", _boom)
    resp = client.post("/api/chat", json={"text": "remember something tricky"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "other",
        "message": "Sorry, I couldn't understand that — try rephrasing.",
    }


def test_chat_ask_answer_failure_returns_friendly_message(client, monkeypatch):
    import assistant.web.app as web_app

    def _boom(text):
        raise ValueError("boom")

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("ask"))
    monkeypatch.setattr(web_app, "answer_from_kb", _boom)
    resp = client.post("/api/chat", json={"text": "a question"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "other",
        "message": "Sorry, I couldn't understand that — try rephrasing.",
    }


def test_chat_resolve_extraction_failure_returns_friendly_message(client, monkeypatch):
    import assistant.web.app as web_app

    def _boom(text, q):
        raise ValueError("boom")

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("resolve", ref_id=9))
    monkeypatch.setattr(
        web_app, "get_escalation",
        lambda i: {"id": 9, "sender": "p", "question_text": "How rerun sync?", "status": "pending"},
    )
    monkeypatch.setattr(web_app, "extract_resolution", _boom)
    resp = client.post("/api/chat", json={"text": "tell them something"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "other",
        "message": "Sorry, I couldn't understand that — try rephrasing.",
    }


def test_teach_confirm_writes_directly_when_local(monkeypatch):
    """_is_local_request keys off the TCP-derived client address (request.client.host),
    not the Host header -- Host is fully client-controlled and was flagged by a security
    review as spoofable. TestClient's `client=` param sets the simulated remote address
    at the ASGI-scope level, which is what request.client.host actually reads."""
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "kb_learn", lambda **kw: captured.update(kw) or 42
    )
    local_client = TestClient(web_app.app, client=("127.0.0.1", 12345))
    resp = local_client.post("/api/teach/confirm", json={"question": "Q?", "answer": "A"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "learned", "id": 42}
    assert captured["question"] == "Q?"
    assert captured["answer"] == "A"
    assert captured["created_by"] == "web"


def test_teach_confirm_gated_when_not_local(client, monkeypatch):
    """A request whose client address isn't 127.0.0.1/::1 (e.g. arrived via ngrok, whose
    local agent's forwarded X-Forwarded-For uvicorn substitutes into request.client) must
    NOT write to agent_knowledge directly -- it queues for approval instead. The default
    `client` fixture's TestClient uses ('testclient', 50000), which is already non-local,
    so no explicit override is needed here to exercise the gated path."""
    import assistant.web.app as web_app

    monkeypatch.setattr(
        web_app, "kb_learn", lambda **kw: pytest.fail("kb_learn must not be called for a peer request")
    )
    captured = {}
    monkeypatch.setattr(
        web_app, "kb_learn_pending",
        lambda question, answer: captured.update(question=question, answer=answer) or 7,
    )
    resp = client.post("/api/teach/confirm", json={"question": "Q?", "answer": "A"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_approval", "id": 7}
    assert captured == {"question": "Q?", "answer": "A"}
