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
    redacted, _ = redact(text)
    hits = kb_search(redacted)
    if not hits:
        return "I don't have anything in the knowledge base about that yet."
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


# --- REPL layer -----------------------------------------------------------
# Handlers take ask_fn/say_fn so tests can script them. All writes gated here.
from assistant.kb import kb_delete, kb_find, kb_learn, kb_update  # noqa: E402
from assistant.tasks import get_escalation, list_open, resolve_escalation  # noqa: E402

HELP = """I route what you type:
  teach     — "remember that <fact>"           (confirms before saving)
  ask       — any question
  tasks     — "what's pending?"
  resolve   — "for escalation N: <answer>"     (learns + resolves, confirms first)
  edit/del  — "that answer about X is wrong / remove it"
  quit      — exit"""


def handle_teach(text: str, ask_fn, say_fn) -> None:
    pair = extract_teach_pair(text)
    say_fn(f"Proposed knowledge:\n  Q: {pair.question}\n  A: {pair.answer}")
    choice = ask_fn("Save? [y = save / e = edit / n = discard] ").strip().lower()
    if choice == "e":
        q = ask_fn(f"Question [{pair.question}]: ").strip() or pair.question
        a = ask_fn(f"Answer [{pair.answer}]: ").strip() or pair.answer
        pair = TeachPair(question=q, answer=a)
        choice = ask_fn("Save now? [y/n] ").strip().lower()
    if choice == "y":
        kb_learn(question=pair.question, answer=pair.answer, created_by="chat", source_refs=["chat"])
        say_fn("Learned.")
    else:
        say_fn("Discarded — nothing saved.")


def handle_tasks(say_fn) -> None:
    open_items = list_open()
    if not open_items["escalations"] and not open_items["drafts"]:
        say_fn("Nothing pending.")
        return
    for e in open_items["escalations"]:
        say_fn(f"escalation #{e[0]} from {e[1]}: {e[2]}")
    for d in open_items["drafts"]:
        say_fn(f"draft #{d[0]} for {d[1]}: {d[2]}  (approve via: python -m assistant.review)")


def handle_resolve(intent: ChatIntent, text: str, ask_fn, say_fn) -> None:
    if intent.ref_id is None:
        say_fn("Which escalation? Say e.g. 'for escalation 3: <answer>'.")
        return
    esc = get_escalation(intent.ref_id)
    if esc is None or esc["status"] != "pending":
        say_fn(f"Escalation {intent.ref_id} not found or not pending.")
        return
    resolution = extract_resolution(text, esc["question_text"])
    say_fn(f"Resolution for #{esc['id']} ({esc['question_text'][:60]}):\n  {resolution}")
    if ask_fn("Resolve & learn this? [y/n] ").strip().lower() == "y":
        ok = resolve_escalation(esc["id"], resolution, resolved_by="chat")
        say_fn("Resolved and learned." if ok else "Could not resolve (already handled?).")
    else:
        say_fn("Left pending.")


def handle_edit_delete(intent: ChatIntent, text: str, ask_fn, say_fn) -> None:
    hits = kb_find(text)
    if not hits:
        say_fn("No matching knowledge entries found.")
        return
    for h in hits:
        say_fn(f"#{h['id']} (sim {h['similarity']:.2f}) Q: {h['question']}\n    A: {h['answer']}")
    chosen = ask_fn("Which entry id? (blank to cancel) ").strip()
    if not chosen.isdigit():
        say_fn("Cancelled.")
        return
    entry_id = int(chosen)
    if intent.action == "delete_kb":
        if ask_fn(f"Type 'delete' to permanently remove #{entry_id}: ").strip() == "delete":
            say_fn("Deleted." if kb_delete(entry_id) else "Not found.")
        else:
            say_fn("Not deleted.")
        return
    q = ask_fn("New question (blank = keep): ").strip() or None
    a = ask_fn("New answer (blank = keep): ").strip() or None
    if q is None and a is None:
        say_fn("Nothing to change.")
        return
    say_fn("Updated (re-embedded)." if kb_update(entry_id, question=q, answer=a) else "Not found.")


def run_repl(ask_fn=input, say_fn=print) -> None:
    from assistant import config
    from assistant.db.client import init_schema

    config.validate()
    init_schema()
    say_fn("assistant chat — type 'help' or 'quit'")
    while True:
        try:
            text = ask_fn("> ").strip()
        except (EOFError, KeyboardInterrupt):
            say_fn("\nbye")
            return
        if not text:
            continue
        if text.lower() in ("quit", "exit"):
            say_fn("bye")
            return
        if text.lower() == "help":
            say_fn(HELP)
            continue
        intent = classify_chat(text)
        if intent.action == "teach":
            handle_teach(text, ask_fn, say_fn)
        elif intent.action == "ask":
            say_fn(answer_from_kb(text))
        elif intent.action == "tasks":
            handle_tasks(say_fn)
        elif intent.action == "resolve":
            handle_resolve(intent, text, ask_fn, say_fn)
        elif intent.action in ("edit_kb", "delete_kb"):
            handle_edit_delete(intent, text, ask_fn, say_fn)
        else:
            say_fn(HELP)


if __name__ == "__main__":
    run_repl()
