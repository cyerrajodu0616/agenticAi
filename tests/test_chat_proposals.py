"""Unit tests — LLM faked, offline."""


class FakeStructured:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def with_structured_output(self, schema):
        return self

    def with_retry(self, **kwargs):
        return self

    def invoke(self, messages):
        self.seen.append(str(messages))
        return self.result


class FakeComposer:
    def __init__(self, text="composed"):
        self.text = text
        self.seen = []

    def with_retry(self, **kwargs):
        return self

    def invoke(self, messages):
        self.seen.append(str(messages))

        class R:
            content = self.text

        return R()


def test_classify_chat_redacts_before_llm(monkeypatch):
    import assistant.chat as chat

    fake = FakeStructured(chat.ChatIntent(action="teach", ref_id=0, reasoning="x"))
    monkeypatch.setattr(chat, "get_model", lambda role: fake)
    intent = chat.classify_chat("remember bob@corp.com owns the sync job")
    assert intent.action == "teach"
    assert "bob@corp.com" not in fake.seen[0]


def test_extract_teach_pair(monkeypatch):
    import assistant.chat as chat

    fake = FakeStructured(chat.TeachPair(question="Who owns the sync job?", answer="[REDACTED_EMAIL_1]"))
    monkeypatch.setattr(chat, "get_model", lambda role: fake)
    pair = chat.extract_teach_pair("remember bob@corp.com owns the sync job")
    assert pair.question.endswith("?")
    assert "bob@corp.com" not in fake.seen[0]


def test_extract_resolution_redacts(monkeypatch):
    import assistant.chat as chat

    fake = FakeComposer(text="Run the resync endpoint for that arcId.")
    monkeypatch.setattr(chat, "get_model", lambda role: fake)
    out = chat.extract_resolution(
        "tell them 555-867-5309 is my number and to run the resync endpoint",
        "How do I fix unsynced data?",
    )
    assert out == "Run the resync endpoint for that arcId."
    assert "555-867-5309" not in fake.seen[0]


def test_answer_from_kb_grounds_and_cites(monkeypatch):
    import assistant.chat as chat

    fake = FakeComposer(text="Wednesdays 6pm ET [agent:deploy window]")
    monkeypatch.setattr(chat, "get_model", lambda role: fake)
    monkeypatch.setattr(
        chat, "kb_search",
        lambda q: [{"source": "agent", "title": "deploy window", "content": "Wed 6pm", "similarity": 0.9}],
    )
    out = chat.answer_from_kb("when is the deploy window?")
    assert "Wednesdays" in out
    assert "Wed 6pm" in fake.seen[0]  # grounded in KB content


def test_answer_from_kb_empty_kb(monkeypatch):
    import assistant.chat as chat

    monkeypatch.setattr(chat, "kb_search", lambda q: [])
    out = chat.answer_from_kb("total mystery question")
    assert "know" in out.lower() or "found" in out.lower()  # honest no-answer, no LLM call
