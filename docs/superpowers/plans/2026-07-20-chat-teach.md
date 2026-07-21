# Chat Teach & Task Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m assistant.chat` — a conversational REPL to teach the KB, ask it questions, list pending tasks, resolve escalations (learning from them), and edit/delete KB entries, with explicit confirmation before every write.

**Architecture:** Three layers on the existing core: (1) deterministic data helpers in `kb.py` + new `tasks.py`; (2) LLM proposal functions in `chat.py` (classify intent, extract teach-pair/resolution) — structured output only, redaction first; (3) a REPL whose handlers take injected `ask_fn`/`say_fn` so unit tests run scripted.

**Tech Stack:** Same as core loop (LangChain, psycopg/pgvector, pytest). No new dependencies, no schema changes.

**Spec:** `docs/superpowers/specs/2026-07-20-chat-teach-addendum.md`

## Global Constraints

- Every DB write goes through an explicit user confirmation; the LLM only proposes structured data.
- `redact()` before every LLM call (import from `assistant.redact`).
- Resolve/teach ordering: `kb_learn` FIRST, then status update (same atomicity rule as `review.approve`).
- `embedding_model_name()` from `assistant.models` is recorded on any (re)embedding.
- Integration tests marked `@pytest.mark.integration`, run with `uv run --env-file .env pytest -m integration ...`; unit tests must run offline with fakes.
- Test rows must be scoped for teardown via `source_refs` containment (lesson from the core-loop C1 finding), never broad `created_by` deletes.

---

### Task 1: Data helpers — KB mutations + task queue

**Files:**
- Modify: `assistant/kb.py` (append functions)
- Create: `assistant/tasks.py`
- Test: `tests/test_kb_mutations.py`, `tests/test_tasks.py` (both integration)

**Interfaces:**
- Consumes: `_embed`, `kb_learn`, `get_connection` (existing), `embedding_model_name()` from `assistant.models`.
- Produces:
  - `kb.kb_find(text: str, limit: int = 5) -> list[dict]` — agent_knowledge only, dicts `{id, question, answer, similarity}` best-first.
  - `kb.kb_get(entry_id: int) -> dict | None` — `{id, question, answer, created_by, source_refs}`.
  - `kb.kb_update(entry_id: int, question: str | None = None, answer: str | None = None) -> bool` — re-embeds iff question changed.
  - `kb.kb_delete(entry_id: int) -> bool`.
  - `tasks.list_open() -> dict` — `{"escalations": [(id, sender, question80, created_at)], "drafts": [(id, sender, question80, created_at)]}`.
  - `tasks.get_escalation(esc_id: int) -> dict | None` — `{id, sender, question_text, status}`.
  - `tasks.resolve_escalation(esc_id: int, resolution_text: str, resolved_by: str = "chat") -> bool` — learn first, then mark resolved; False if missing/not pending.

- [ ] **Step 1: Write the failing tests** — `tests/test_kb_mutations.py`:

```python
import pytest

pytestmark = pytest.mark.integration

REF = "pytest:kb-mutations"


@pytest.fixture()
def entry_id():
    from assistant.db.client import get_connection, init_schema
    from assistant.kb import kb_learn

    init_schema()
    eid = kb_learn(
        question="What is the deploy window for portal releases?",
        answer="Wednesdays 6pm ET.",
        created_by="pytest",
        source_refs=[REF],
    )
    yield eid
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_knowledge WHERE %s = ANY(source_refs)", (REF,))


def test_find_get_roundtrip(entry_id):
    from assistant.kb import kb_find, kb_get

    hits = kb_find("when can I deploy the portal?")
    assert any(h["id"] == entry_id for h in hits)
    got = kb_get(entry_id)
    assert got["answer"] == "Wednesdays 6pm ET."
    assert kb_get(99999999) is None


def test_update_answer_only_keeps_embedding(entry_id):
    from assistant.db.client import get_connection
    from assistant.kb import kb_get, kb_update

    with get_connection() as conn:
        before = conn.execute(
            "SELECT question_embedding::text FROM agent_knowledge WHERE id=%s", (entry_id,)
        ).fetchone()[0]
    assert kb_update(entry_id, answer="Thursdays 6pm ET.") is True
    with get_connection() as conn:
        after = conn.execute(
            "SELECT question_embedding::text FROM agent_knowledge WHERE id=%s", (entry_id,)
        ).fetchone()[0]
    assert kb_get(entry_id)["answer"] == "Thursdays 6pm ET."
    assert before == after  # answer-only update must not re-embed


def test_update_question_reembeds(entry_id):
    from assistant.db.client import get_connection
    from assistant.kb import kb_update

    with assistant_embedding_snapshot(entry_id) as before:
        assert kb_update(entry_id, question="What is the portal release window?") is True
    with assistant_embedding_snapshot(entry_id) as after:
        pass
    assert before != after


from contextlib import contextmanager


@contextmanager
def assistant_embedding_snapshot(entry_id):
    from assistant.db.client import get_connection

    with get_connection() as conn:
        yield conn.execute(
            "SELECT question_embedding::text FROM agent_knowledge WHERE id=%s", (entry_id,)
        ).fetchone()[0]


def test_delete(entry_id):
    from assistant.kb import kb_delete, kb_get

    assert kb_delete(entry_id) is True
    assert kb_get(entry_id) is None
    assert kb_delete(entry_id) is False
```

`tests/test_tasks.py`:

```python
import pytest

pytestmark = pytest.mark.integration

REF = "pytest:tasks"


@pytest.fixture()
def esc_id():
    from assistant.db.client import get_connection, init_schema

    init_schema()
    with get_connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_escalations (source_channel, sender, question_text)"
            " VALUES ('test', 'peer@corp.com', 'How do I rerun the nightly sync?')"
            " RETURNING id"
        ).fetchone()
    yield row[0]
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_escalations WHERE id=%s", (row[0],))
        conn.execute(
            "DELETE FROM agent_knowledge WHERE %s = ANY(source_refs)",
            (f"escalation:{row[0]}",),
        )


def test_list_open_includes_new_escalation(esc_id):
    from assistant.tasks import list_open

    ids = [e[0] for e in list_open()["escalations"]]
    assert esc_id in ids


def test_resolve_learns_then_marks(esc_id):
    from assistant.db.client import get_connection
    from assistant.tasks import get_escalation, resolve_escalation

    assert resolve_escalation(esc_id, "Run scripts/nightly.sh --force", resolved_by="pytest") is True
    assert get_escalation(esc_id)["status"] == "resolved"
    with get_connection() as conn:
        learned = conn.execute(
            "SELECT canonical_answer FROM agent_knowledge WHERE %s = ANY(source_refs)",
            (f"escalation:{esc_id}",),
        ).fetchone()
    assert learned[0] == "Run scripts/nightly.sh --force"
    # second resolve must refuse
    assert resolve_escalation(esc_id, "again") is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --env-file .env pytest -m integration tests/test_kb_mutations.py tests/test_tasks.py -v`
Expected: FAIL — `ImportError` (`kb_find`, `assistant.tasks` missing).

- [ ] **Step 3: Implement** — append to `assistant/kb.py`:

```python
def kb_find(text: str, limit: int = 5) -> list[dict]:
    """Search agent_knowledge only, returning ids — for interactive edit/delete flows."""
    vec = _embed(text)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, canonical_question, canonical_answer,"
            " 1 - (question_embedding <=> %s::vector) AS similarity"
            " FROM agent_knowledge WHERE question_embedding IS NOT NULL"
            " ORDER BY question_embedding <=> %s::vector LIMIT %s",
            (vec, vec, limit),
        ).fetchall()
    return [
        {"id": r[0], "question": r[1], "answer": r[2], "similarity": float(r[3])}
        for r in rows
    ]


def kb_get(entry_id: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT id, canonical_question, canonical_answer, created_by, source_refs"
            " FROM agent_knowledge WHERE id=%s",
            (entry_id,),
        ).fetchone()
    if r is None:
        return None
    return {"id": r[0], "question": r[1], "answer": r[2], "created_by": r[3], "source_refs": r[4]}


def kb_update(entry_id: int, question: str | None = None, answer: str | None = None) -> bool:
    from assistant.models import embedding_model_name

    current = kb_get(entry_id)
    if current is None:
        return False
    new_q = question if question is not None else current["question"]
    new_a = answer if answer is not None else current["answer"]
    with get_connection() as conn:
        if question is not None:
            vec = _embed(new_q)  # question changed -> must re-embed
            conn.execute(
                "UPDATE agent_knowledge SET canonical_question=%s, canonical_answer=%s,"
                " question_embedding=%s, embedding_model=%s WHERE id=%s",
                (new_q, new_a, vec, embedding_model_name(), entry_id),
            )
        else:
            conn.execute(
                "UPDATE agent_knowledge SET canonical_answer=%s WHERE id=%s",
                (new_a, entry_id),
            )
    return True


def kb_delete(entry_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM agent_knowledge WHERE id=%s", (entry_id,))
        return cur.rowcount == 1
```

Create `assistant/tasks.py`:

```python
"""Task-queue helpers: pending escalations + drafts, and escalation resolution."""
from assistant.db.client import get_connection
from assistant.kb import kb_learn


def list_open() -> dict:
    with get_connection() as conn:
        escalations = conn.execute(
            "SELECT id, sender, left(question_text, 80), created_at"
            " FROM agent_escalations WHERE status='pending' ORDER BY id"
        ).fetchall()
        drafts = conn.execute(
            "SELECT id, payload->>'sender', left(payload->>'question', 80), created_at"
            " FROM review_items WHERE status='pending' ORDER BY id"
        ).fetchall()
    return {"escalations": escalations, "drafts": drafts}


def get_escalation(esc_id: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT id, sender, question_text, status FROM agent_escalations WHERE id=%s",
            (esc_id,),
        ).fetchone()
    if r is None:
        return None
    return {"id": r[0], "sender": r[1], "question_text": r[2], "status": r[3]}


def resolve_escalation(esc_id: int, resolution_text: str, resolved_by: str = "chat") -> bool:
    esc = get_escalation(esc_id)
    if esc is None or esc["status"] != "pending":
        return False
    # Learn FIRST: if embedding fails, the escalation stays pending and resolve is retryable.
    kb_learn(
        question=esc["question_text"],
        answer=resolution_text,
        created_by=resolved_by,
        source_refs=[f"escalation:{esc_id}"],
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE agent_escalations SET status='resolved', resolution_text=%s,"
            " resolved_by=%s, resolved_at=now() WHERE id=%s AND status='pending'",
            (resolution_text, resolved_by, esc_id),
        )
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --env-file .env pytest -m integration tests/test_kb_mutations.py tests/test_tasks.py -v`
Expected: 6 passed. Also `uv run pytest` — unit suite unchanged (18 passed).

- [ ] **Step 5: Commit**

```bash
git add assistant/kb.py assistant/tasks.py tests/test_kb_mutations.py tests/test_tasks.py
git commit -m "feat: KB mutation helpers + task queue (learn-first escalation resolve)"
```

---

### Task 2: LLM proposal layer — chat intent + extractors

**Files:**
- Create: `assistant/chat.py` (proposal layer only; REPL arrives in Task 3)
- Test: `tests/test_chat_proposals.py` (unit, fakes)

**Interfaces:**
- Consumes: `get_model` (roles "classify", "compose"), `redact`.
- Produces (all redact input before the LLM sees it):
  - `class ChatIntent(BaseModel)`: `action: Literal["teach","ask","tasks","resolve","edit_kb","delete_kb","other"]`, `ref_id: int | None` (escalation/KB id if the user stated one), `reasoning: str`.
  - `class TeachPair(BaseModel)`: `question: str`, `answer: str`.
  - `classify_chat(text: str) -> ChatIntent`
  - `extract_teach_pair(text: str) -> TeachPair`
  - `extract_resolution(text: str, escalation_question: str) -> str` (the answer text to store)
  - `answer_from_kb(text: str) -> str` (kb_search + compose, cites sources, says so when nothing relevant)

- [ ] **Step 1: Write the failing test** — `tests/test_chat_proposals.py`:

```python
"""Unit tests — LLM faked, offline."""
import pytest


class FakeStructured:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.seen.append(str(messages))
        return self.result


class FakeComposer:
    def __init__(self, text="composed"):
        self.text = text
        self.seen = []

    def invoke(self, messages):
        self.seen.append(str(messages))

        class R:
            content = self.text

        return R()


def test_classify_chat_redacts_before_llm(monkeypatch):
    import assistant.chat as chat

    fake = FakeStructured(chat.ChatIntent(action="teach", ref_id=None, reasoning="x"))
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_chat_proposals.py -v`
Expected: FAIL — `No module named 'assistant.chat'`.

- [ ] **Step 3: Implement** — `assistant/chat.py` (proposal layer):

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_chat_proposals.py -v`
Expected: 5 passed (and full unit suite: `uv run pytest` → 23 passed).

- [ ] **Step 5: Commit**

```bash
git add assistant/chat.py tests/test_chat_proposals.py
git commit -m "feat: chat proposal layer — intent classify + teach/resolve extractors"
```

---

### Task 3: The REPL — handlers with confirm gates

**Files:**
- Modify: `assistant/chat.py` (append REPL section)
- Test: `tests/test_chat_repl.py` (unit, scripted I/O + fakes), plus manual acceptance

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces: `run_repl(ask_fn=input, say_fn=print) -> None` and `python -m assistant.chat` entrypoint. Handlers (each takes `ask_fn`/`say_fn`): `handle_teach(text, ask_fn, say_fn)`, `handle_resolve(intent, text, ask_fn, say_fn)`, `handle_tasks(say_fn)`, `handle_edit_delete(intent, text, ask_fn, say_fn)`.

- [ ] **Step 1: Write the failing test** — `tests/test_chat_repl.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_chat_repl.py -v`
Expected: FAIL — handlers not defined.

- [ ] **Step 3: Implement** — append to `assistant/chat.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_chat_repl.py -v` → 5 passed; `uv run pytest` → 28 passed.

- [ ] **Step 5: Manual acceptance run** (live LLM + DB):

```bash
docker compose up -d
uv run python -m assistant.chat
```

Script: (1) `remember that the eConsent PDFs are under the consent S3 bucket keyed by arcId` → confirm `y`; (2) `where are the eConsent PDFs stored?` → expect grounded answer citing the just-taught entry; (3) `what's pending?` → lists open items; (4) if an escalation is pending, `for escalation <id>: <answer>` → confirm → verify `resolved` status and new KB row; (5) `quit`. Capture the transcript in the task report.

- [ ] **Step 6: Commit**

```bash
git add assistant/chat.py tests/test_chat_repl.py
git commit -m "feat: chat REPL — teach/ask/tasks/resolve/edit with confirm gates"
```

---

## Definition of done (this plan)

`python -m assistant.chat` supports the five conversational flows end-to-end against live models + DB; every write (teach, resolve, edit, delete) is preceded by an explicit confirmation; resolving an escalation both closes it and teaches the KB; unit suite (28) passes offline; integration suite passes with the two new test files.
