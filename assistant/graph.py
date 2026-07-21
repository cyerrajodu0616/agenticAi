"""The agent brain: ingest -> redact -> classify -> route -> draft/escalate -> review queue.

LLM nodes: classify_intent and compose ONLY. Everything else is deterministic.
Every path terminates in review_items or agent_escalations — nothing auto-sends.
"""
import json
from typing import Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from assistant import config
from assistant.db.client import get_connection
from assistant.kb import kb_search
from assistant.models import get_model
from assistant.redact import redact


class Intent(BaseModel):
    intent: Literal["kb_answer", "sync_fix", "analysis_task", "escalate"] = Field(
        description="kb_answer: a question answerable from the knowledge base. "
        "sync_fix: asks to re-run/repair a data sync. "
        "analysis_task: asks for ad-hoc data analysis. "
        "escalate: anything else or unclear."
    )
    reasoning: str


class State(TypedDict, total=False):
    raw_text: str
    source_channel: str
    sender: str
    thread_id: str | None
    redacted_text: str
    pii_map: dict
    intent: str
    kb_hits: list
    draft: str
    review_item_id: int
    escalation_id: int


def _save_review_item(kind: str, payload: dict) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO review_items (kind, payload) VALUES (%s, %s) RETURNING id",
            (kind, json.dumps(payload)),
        ).fetchone()
    return row[0]


def _save_escalation(*, source_channel, thread_id, sender, question_text, reason) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_escalations"
            " (source_channel, thread_id, sender, question_text, reason)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (source_channel, thread_id, sender, question_text, reason),
        ).fetchone()
    return row[0]


def redact_pii(state: State) -> State:
    redacted, mapping = redact(state["raw_text"])
    return {"redacted_text": redacted, "pii_map": mapping}


def classify_intent(state: State) -> State:
    llm = get_model("classify").with_structured_output(Intent)
    result = llm.invoke(
        [
            ("system", "Classify the incoming work message. Respond with the schema."),
            ("human", state["redacted_text"]),
        ]
    )
    return {"intent": result.intent}


def kb_answer(state: State) -> State:
    hits = kb_search(state["redacted_text"])
    return {"kb_hits": hits}


def compose(state: State) -> State:
    context = "\n\n".join(
        f"[{h['source']}:{h['title']} sim={h['similarity']:.2f}]\n{redact(h['content'])[0]}"
        for h in state["kb_hits"]
    )
    llm = get_model("compose")
    result = llm.invoke(
        [
            (
                "system",
                "Draft a short, professional reply to a colleague. Ground the answer ONLY"
                " in the knowledge snippets provided. Cite which snippet you used."
                " If the snippets don't answer the question, say you couldn't find it.",
            ),
            ("human", f"Question:\n{state['redacted_text']}\n\nKnowledge:\n{context}"),
        ]
    )
    draft = result.content
    item_id = _save_review_item(
        "reply",
        {
            "draft": draft,
            "question": state["redacted_text"],
            "sender": state["sender"],
            "source_channel": state["source_channel"],
            "thread_id": state.get("thread_id"),
            "kb_sources": [h["title"] for h in state["kb_hits"]],
        },
    )
    return {"draft": draft, "review_item_id": item_id}


def escalate(state: State) -> State:
    intent = state.get("intent")
    kb_hits = state.get("kb_hits")
    if intent in ("sync_fix", "analysis_task"):
        # Action layer arrives in Plan 3; until then a human handles these.
        reason = f"intent={intent} but action layer not built yet"
    elif intent == "kb_answer" and kb_hits is not None:
        reason = "no KB match above threshold"
    else:
        reason = "classifier chose escalate"
    esc_id = _save_escalation(
        source_channel=state["source_channel"],
        thread_id=state.get("thread_id"),
        sender=state["sender"],
        question_text=state["redacted_text"],
        reason=reason,
    )
    return {"escalation_id": esc_id}


def route_after_classify(state: State) -> str:
    intent = state["intent"]
    if intent == "kb_answer":
        return "kb_answer"
    return "escalate"


def route_after_kb(state: State) -> str:
    hits = state["kb_hits"]
    if hits and hits[0]["similarity"] >= config.SIMILARITY_THRESHOLD:
        return "compose"
    return "escalate"


def build_graph():
    g = StateGraph(State)
    g.add_node("redact_pii", redact_pii)
    g.add_node("classify_intent", classify_intent)
    g.add_node("kb_answer", kb_answer)
    g.add_node("compose", compose)
    g.add_node("escalate", escalate)

    g.set_entry_point("redact_pii")
    g.add_edge("redact_pii", "classify_intent")
    g.add_conditional_edges(
        "classify_intent", route_after_classify,
        {"kb_answer": "kb_answer", "escalate": "escalate"},
    )
    g.add_conditional_edges(
        "kb_answer", route_after_kb, {"compose": "compose", "escalate": "escalate"}
    )
    g.add_edge("compose", END)
    g.add_edge("escalate", END)
    return g.compile()
