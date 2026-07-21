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


def test_edit_confirm_no_does_not_update(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(
        chat, "kb_find",
        lambda t: [{"id": 3, "question": "Old Q?", "answer": "Old A", "similarity": 0.9}],
    )
    called = []
    monkeypatch.setattr(chat, "kb_update", lambda *a, **kw: called.append((a, kw)) or True)
    # picks entry 3, edits question+answer, but declines the final confirm
    ask, say, said = _io(["3", "New Q?", "New A", "n"])
    intent = chat.ChatIntent(action="edit_kb", ref_id=None, reasoning="x")
    chat.handle_edit_delete(intent, "that answer about wifi is wrong", ask, say)
    assert called == []
    assert any("Not saved" in s for s in said)


def test_edit_confirm_yes_updates(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(
        chat, "kb_find",
        lambda t: [{"id": 3, "question": "Old Q?", "answer": "Old A", "similarity": 0.9}],
    )
    called = []
    monkeypatch.setattr(chat, "kb_update", lambda *a, **kw: called.append((a, kw)) or True)
    ask, say, said = _io(["3", "New Q?", "New A", "y"])
    intent = chat.ChatIntent(action="edit_kb", ref_id=None, reasoning="x")
    chat.handle_edit_delete(intent, "that answer about wifi is wrong", ask, say)
    assert len(called) == 1
    args, kwargs = called[0]
    assert args == (3,)
    assert kwargs == {"question": "New Q?", "answer": "New A"}


def test_run_repl_survives_classify_exception(monkeypatch):
    import assistant.chat as chat
    import assistant.config as config
    import assistant.db.client as db_client

    # run_repl imports these locally at call time, so patch the source modules.
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(db_client, "init_schema", lambda: None)

    def boom(text):
        raise ValueError("Groq emitted the string 'None' instead of JSON null")

    monkeypatch.setattr(chat, "classify_chat", boom)
    ask, say, said = _io(["some confusing input", "quit"])
    chat.run_repl(ask_fn=ask, say_fn=say)
    assert any("couldn't understand" in s for s in said)
    assert said[-1] == "bye"
