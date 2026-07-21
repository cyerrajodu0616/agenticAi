"""Conversational teach/ask/tasks interface — LLM proposal layer.

Every function here redacts input before any LLM call, and returns PROPOSALS.
Writes happen only in the REPL handlers (Task 3) after explicit user confirmation.
"""
from typing import Literal

from pydantic import BaseModel, Field

from assistant.kb import kb_search
from assistant.models import get_model
from assistant.redact import redact


class ChatIntent(BaseModel):
    action: Literal["teach", "ask", "tasks", "resolve", "edit_kb", "delete_kb", "other"] = Field(
        description="teach: user states a fact/procedure to remember. "
        "ask: user asks a question. tasks: user asks what's pending. "
        "resolve: user provides the answer for a pending escalation. "
        "edit_kb: user corrects stored knowledge. delete_kb: user wants knowledge removed. "
        "other: anything else."
    )
    ref_id: int | None = Field(
        default=None, description="Escalation or KB entry id if the user referenced one."
    )
    reasoning: str


class TeachPair(BaseModel):
    question: str = Field(description="Canonical question this knowledge answers, ending in '?'")
    answer: str = Field(description="The answer, stated from the user's message only")


def classify_chat(text: str) -> ChatIntent:
    redacted, _ = redact(text)
    llm = get_model("classify").with_structured_output(ChatIntent)
    return llm.invoke(
        [
            ("system", "Classify the user's chat message for a personal-assistant REPL."),
            ("human", redacted),
        ]
    )


def extract_teach_pair(text: str) -> TeachPair:
    redacted, _ = redact(text)
    llm = get_model("classify").with_structured_output(TeachPair)
    return llm.invoke(
        [
            (
                "system",
                "Extract ONE canonical question+answer pair from the user's statement."
                " Use only facts present in the statement — do not add your own knowledge.",
            ),
            ("human", redacted),
        ]
    )


def extract_resolution(text: str, escalation_question: str) -> str:
    redacted, _ = redact(text)
    red_q, _ = redact(escalation_question)
    llm = get_model("compose")
    result = llm.invoke(
        [
            (
                "system",
                "The user is answering a colleague's escalated question. Rewrite the user's"
                " input as a clean, professional answer to store and send. Content only from"
                " the user's input — no invented facts.",
            ),
            ("human", f"Escalated question:\n{red_q}\n\nUser's answer:\n{redacted}"),
        ]
    )
    return result.content


def answer_from_kb(text: str) -> str:
    hits = kb_search(text)
    if not hits:
        return "I don't have anything in the knowledge base about that yet."
    redacted, _ = redact(text)
    context = "\n\n".join(
        f"[{h['source']}:{redact(h['title'])[0]} sim={h['similarity']:.2f}]\n{redact(h['content'])[0]}"
        for h in hits
    )
    llm = get_model("compose")
    result = llm.invoke(
        [
            (
                "system",
                "Answer the user's question grounded ONLY in the knowledge snippets."
                " Cite which snippet you used. If they don't answer it, say you couldn't find it.",
            ),
            ("human", f"Question:\n{redacted}\n\nKnowledge:\n{context}"),
        ]
    )
    return result.content
