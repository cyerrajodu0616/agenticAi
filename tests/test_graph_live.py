"""End-to-end with real LLM + DB. Requires: docker compose up, cloud API keys in .env."""
import pytest

pytestmark = pytest.mark.integration


def test_pdf_question_end_to_end():
    from assistant.db.client import init_schema
    from assistant.graph import build_graph
    from assistant.kb import kb_learn

    init_schema()
    kb_learn(
        question="Where is the eConsent HIPAA PDF for an application?",
        answer="Query consentDetails for the arcId; the S3 key pattern is in runbook X.",
        created_by="pytest-live",
        source_refs=[],
    )
    out = build_graph().invoke(
        {
            "raw_text": "Hi! Where can I find the signed eConsent PDF for ARCF25344h646?",
            "source_channel": "test",
            "sender": "peer@corp.com",
            "thread_id": None,
        }
    )
    assert out["intent"] == "kb_answer"
    assert out.get("review_item_id"), "expected a pending reply draft in review_items"


def test_unrelated_question_escalates_on_low_similarity():
    from assistant.db.client import init_schema
    from assistant.graph import build_graph
    from assistant.kb import kb_learn

    init_schema()
    kb_learn(
        question="Where is the eConsent HIPAA PDF for an application?",
        answer="Query consentDetails for the arcId; the S3 key pattern is in runbook X.",
        created_by="pytest-live",
        source_refs=[],
    )
    out = build_graph().invoke(
        {
            "raw_text": "What is the wifi password for the 3rd floor conference room?",
            "source_channel": "test",
            "sender": "peer@corp.com",
            "thread_id": None,
        }
    )
    assert out.get("escalation_id"), "unrelated question must escalate, not compose"
