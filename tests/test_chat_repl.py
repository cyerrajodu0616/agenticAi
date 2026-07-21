"""REPL handler tests — scripted I/O, all LLM/DB faked, offline."""


def _io(answers):
    given = list(answers)
    said = []

    def ask(prompt=""):
        return given.pop(0)

    def say(msg):
        said.append(str(msg))

    return ask, say, said


def test_teach_confirm_yes_learns(monkeypatch):
    import assistant.chat as chat

    learned = {}
    monkeypatch.setattr(
        chat, "extract_teach_pair",
        lambda text: chat.TeachPair(question="Who owns sync?", answer="Bob"),
    )
    monkeypatch.setattr(
        chat, "kb_learn",
        lambda **kw: learned.update(kw) or 1,
    )
    ask, say, said = _io(["y"])
    chat.handle_teach("remember bob owns sync", ask, say)
    assert learned["question"] == "Who owns sync?"
    assert learned["created_by"] == "chat"


def test_teach_confirm_no_learns_nothing(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(
        chat, "extract_teach_pair",
        lambda text: chat.TeachPair(question="Q?", answer="A"),
    )
    called = []
    monkeypatch.setattr(chat, "kb_learn", lambda **kw: called.append(kw))
    ask, say, said = _io(["n"])
    chat.handle_teach("whatever", ask, say)
    assert called == []


def test_resolve_requires_pending_escalation(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(chat, "get_escalation", lambda i: None)
    ask, say, said = _io([])
    intent = chat.ChatIntent(action="resolve", ref_id=42, reasoning="x")
    chat.handle_resolve(intent, "tell them X", ask, say)
    assert any("42" in s for s in said)  # says escalation 42 not found


def test_resolve_confirm_yes_resolves(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(
        chat, "get_escalation",
        lambda i: {"id": 7, "sender": "p", "question_text": "How rerun sync?", "status": "pending"},
    )
    monkeypatch.setattr(chat, "extract_resolution", lambda t, q: "Run resync.")
    resolved = {}
    monkeypatch.setattr(
        chat, "resolve_escalation",
        lambda eid, text, resolved_by="chat": resolved.update(id=eid, text=text) or True,
    )
    ask, say, said = _io(["y"])
    intent = chat.ChatIntent(action="resolve", ref_id=7, reasoning="x")
    chat.handle_resolve(intent, "tell them to run resync", ask, say)
    assert resolved == {"id": 7, "text": "Run resync."}


def test_delete_requires_typed_confirmation(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(
        chat, "kb_find",
        lambda t: [{"id": 3, "question": "Old Q?", "answer": "Old A", "similarity": 0.9}],
    )
    deleted = []
    monkeypatch.setattr(chat, "kb_delete", lambda i: deleted.append(i) or True)
    # picks entry 3, but types "yes" instead of "delete" -> refused
    ask, say, said = _io(["3", "yes"])
    intent = chat.ChatIntent(action="delete_kb", ref_id=None, reasoning="x")
    chat.handle_edit_delete(intent, "remove the old wifi answer", ask, say)
    assert deleted == []
