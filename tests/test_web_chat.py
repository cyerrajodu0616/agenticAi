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
    hits = [{"source": "agent", "title": "deploy window", "content": "Wed 6pm", "similarity": 0.9}]
    monkeypatch.setattr(web_app, "answer_from_kb", lambda text: ("the answer, cited", hits))
    monkeypatch.setattr(web_app, "save_chat", lambda **kw: 42)
    resp = client.post("/api/chat", json={"text": "when is the deploy window?"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "ask", "answer": "the answer, cited", "chat_id": 42, "sources": hits,
    }


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
    # client=("127.0.0.1", ...) + base_url matching: _is_local_request now requires
    # BOTH the socket-level client address AND the Host header to say localhost.
    no_raise_client = TestClient(
        web_app.app, raise_server_exceptions=False,
        client=("127.0.0.1", 12345), base_url="http://127.0.0.1",
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


def test_chat_ask_save_chat_failure_still_returns_answer(client, monkeypatch):
    """If persistence (save_chat) fails, the endpoint should still return the correct
    answer and sources to the user — persistence is a secondary/optional operation that
    should never hide or replace the primary answer. The user gets their answer, just
    without a chat_id to correct against later (nothing was persisted, so nothing can
    be corrected)."""
    import assistant.web.app as web_app

    def _boom(**kwargs):
        raise RuntimeError("database connection failed")

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("ask"))
    hits = [{"source": "agent", "title": "deploy window", "content": "Wed 6pm", "similarity": 0.9}]
    monkeypatch.setattr(web_app, "answer_from_kb", lambda text: ("the answer, cited", hits))
    monkeypatch.setattr(web_app, "save_chat", _boom)
    resp = client.post("/api/chat", json={"text": "when is the deploy window?"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "ask", "answer": "the answer, cited", "chat_id": None, "sources": hits,
    }


def test_chat_ask_persists_with_redacted_question(monkeypatch):
    """The stored question must be the redacted text, not the raw user input --
    matching this project's redact-before-persist invariant, enforced at every
    other write site (kb.py, ingest.py)."""
    import assistant.web.app as web_app

    monkeypatch.setattr(web_app, "classify_chat", lambda text: FakeIntent("ask"))
    monkeypatch.setattr(web_app, "answer_from_kb", lambda text: ("cited answer", []))
    captured = {}
    monkeypatch.setattr(
        web_app, "save_chat",
        lambda **kw: captured.update(kw) or 7,
    )
    local_client = TestClient(
        web_app.app, client=("127.0.0.1", 12345), base_url="http://127.0.0.1"
    )
    local_client.post(
        "/api/chat", json={"text": "call bob@corp.com about the deploy window"},
    )
    assert "bob@corp.com" not in captured["question"]
    assert captured["created_by"] == "local"


def test_chat_history_returns_recent_entries(client, monkeypatch):
    import assistant.web.app as web_app

    rows = [
        {"id": 2, "question": "q2", "answer": "a2", "sources": [], "created_by": "local",
         "created_at": "2026-07-23T10:00:00"},
        {"id": 1, "question": "q1", "answer": "a1", "sources": [], "created_by": "local",
         "created_at": "2026-07-23T09:00:00"},
    ]
    monkeypatch.setattr(web_app, "list_recent", lambda limit=20: rows)
    resp = client.get("/api/chat/history")
    assert resp.status_code == 200
    assert resp.json() == {"entries": rows}


def test_teach_confirm_writes_directly_when_local(monkeypatch):
    """_is_local_request requires BOTH the TCP-derived client address AND the Host header
    to say localhost -- neither alone is trustworthy (a remote peer can fake Host; a
    DNS-rebinding page can make a real browser connect from a real local socket while
    sending its own attacker-controlled Host). TestClient's `client=` sets the simulated
    socket peer; `base_url` controls what Host header requests carry."""
    import assistant.web.app as web_app

    captured = {}
    monkeypatch.setattr(
        web_app, "kb_learn", lambda **kw: captured.update(kw) or 42
    )
    local_client = TestClient(
        web_app.app, client=("127.0.0.1", 12345), base_url="http://127.0.0.1"
    )
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


def test_teach_confirm_gated_for_dns_rebinding_attempt(monkeypatch):
    """DNS rebinding: a malicious webpage's hostname re-resolves to 127.0.0.1, so the
    victim's own browser makes a request that IS genuinely socket-local (client address
    really is 127.0.0.1) -- but the browser can't forge its own Host header, so it still
    carries the attacker's original domain, not "127.0.0.1"/"localhost". This must be
    gated, not treated as a trusted local write, even though the socket signal alone
    would say "local"."""
    import assistant.web.app as web_app

    monkeypatch.setattr(
        web_app, "kb_learn",
        lambda **kw: pytest.fail("kb_learn must not be called for a DNS-rebinding request"),
    )
    captured = {}
    monkeypatch.setattr(
        web_app, "kb_learn_pending",
        lambda question, answer: captured.update(question=question, answer=answer) or 9,
    )
    # Socket-local (client=127.0.0.1) but Host is the attacker's rebound domain, not
    # localhost -- exactly the DNS-rebinding shape.
    rebinding_client = TestClient(
        web_app.app, client=("127.0.0.1", 12345), base_url="http://evil-rebound.example",
    )
    resp = rebinding_client.post("/api/teach/confirm", json={"question": "Q?", "answer": "A"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_approval", "id": 9}
    assert captured == {"question": "Q?", "answer": "A"}
