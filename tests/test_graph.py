"""Routing tests with LLM and DB faked — runs offline."""
import pytest


class FakeClassifier:
    def __init__(self, intent):
        self.intent = intent

    def with_structured_output(self, schema):
        return self

    def with_retry(self, **kwargs):
        return self

    def invoke(self, messages):
        from assistant.graph import Intent

        return Intent(intent=self.intent, reasoning="fake")


class FakeComposer:
    def with_retry(self, **kwargs):
        return self

    def invoke(self, messages):
        class R:
            content = "DRAFT ANSWER grounded in KB"

        return R()


@pytest.fixture()
def wired(monkeypatch):
    saved = {}

    def fake_get_model(role):
        return {"classify": wired.classifier, "compose": FakeComposer()}[role]

    def fake_save_review_item(kind, payload):
        saved["item"] = (kind, payload)
        return 42

    def fake_save_escalation(**kw):
        saved["escalation"] = kw
        return 7

    monkeypatch.setattr("assistant.graph.get_model", fake_get_model)
    monkeypatch.setattr("assistant.graph._save_review_item", fake_save_review_item)
    monkeypatch.setattr("assistant.graph._save_escalation", fake_save_escalation)
    wired.saved = saved
    return wired


def _run(graph_module, text):
    app = graph_module.build_graph()
    return app.invoke(
        {"raw_text": text, "source_channel": "test", "sender": "peer@corp.com",
         "thread_id": None}
    )


def test_kb_hit_produces_reply_draft(wired, monkeypatch):
    import assistant.graph as graph

    wired.classifier = FakeClassifier("kb_answer")
    monkeypatch.setattr(
        "assistant.graph.kb_search",
        lambda q: [{"source": "agent", "title": "t", "content": "the answer", "similarity": 0.95}],
    )
    out = _run(graph, "Where is the eConsent PDF for ARCF25344h646?")
    assert out["intent"] == "kb_answer"
    assert out["review_item_id"] == 42
    kind, payload = wired.saved["item"]
    assert kind == "reply"
    assert "DRAFT ANSWER" in payload["draft"]


def test_kb_miss_escalates(wired, monkeypatch):
    import assistant.graph as graph

    wired.classifier = FakeClassifier("kb_answer")
    monkeypatch.setattr(
        "assistant.graph.kb_search",
        lambda q: [{"source": "agent", "title": "t", "content": "x", "similarity": 0.2}],
    )
    out = _run(graph, "Something the KB has never seen")
    assert out["escalation_id"] == 7


def test_action_intents_escalate_until_action_layer_exists(wired, monkeypatch):
    import assistant.graph as graph

    wired.classifier = FakeClassifier("sync_fix")
    out = _run(graph, "data for ARCF999 is not synced, can you resync")
    assert out["escalation_id"] == 7
    assert "action layer" in wired.saved["escalation"]["reason"]


def test_llm_nodes_never_see_raw_text(wired, monkeypatch):
    import assistant.graph as graph

    seen = []

    class SpyClassifier(FakeClassifier):
        def invoke(self, messages):
            seen.append(str(messages))
            return super().invoke(messages)

    wired.classifier = SpyClassifier("kb_answer")
    monkeypatch.setattr("assistant.graph.kb_search", lambda q: [])
    _run(graph, "ssn 123-45-6789 and mail a@b.com need checking")
    joined = " ".join(seen)
    assert "123-45-6789" not in joined
    assert "a@b.com" not in joined
