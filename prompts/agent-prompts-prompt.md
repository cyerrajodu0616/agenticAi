# Claude Code Prompt: classify_intent / compose_response Prompt Text

Paste this into Claude Code after the MVP scaffold (`langgraph-personal-assistant-mvp-prompt.md`)
and the PDF resolver prompt (`pdf-location-resolver-prompt.md`) have been applied. This fills in
the one piece that was only described conceptually so far: the actual system prompt text and
structured-output schemas for the two LLM nodes in `assistant/graph.py`.

---

## Context

MVP prompt Task 8 described `classify_intent` and `compose_response` behaviorally but never wrote
literal prompt text. This prompt creates `assistant/prompts.py` with real, ready-to-use system
prompts and Pydantic schemas, then wires them into the two nodes. Nothing here depends on the
still-unconfirmed external integrations (arcCenter endpoints, Azure backend) — this is fully
implementable now.

## Task 1 — `assistant/prompts.py`

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field


class IntentClassification(BaseModel):
    intent: Literal["structured_lookup", "kb_semantic_search", "escalate"]
    reasoning: str = Field(
        description="One sentence, internal-only, explaining the classification. Not shown to the requester."
    )
    confidence: float = Field(ge=0, le=1)


CLASSIFY_INTENT_SYSTEM_PROMPT = """\
You are the intent router for Afficiency's internal RCA/Q&A assistant. Peers, helpdesk, and \
business team members send questions that would normally go to a specific engineer. Your only \
job is to classify the question into exactly one of three categories — you do not answer the \
question yourself.

Categories:

1. structured_lookup — The question asks for a specific, deterministic fact about a specific, \
identified record (an arcId, application, policy, or similar identifier is present or clearly \
implied) that can be answered by calling a known internal tool.
   Example: "Where's the eConsent PDF for arcId ARCF25344h646?"
   Example: "What status is application ARCF25344h697 in?"

2. kb_semantic_search — The question is a general how-to, process, or policy question that isn't \
tied to one specific record, and is the kind of thing likely to have been asked and answered \
before.
   Example: "How do I reset an agent's MFA?"
   Example: "What's our SLA for underwriting decisions?"

3. escalate — Anything else: the question is ambiguous, is missing an identifier a \
structured_lookup would need, asks for a judgment call or approval, involves a topic with no \
defined tool or knowledge source, or you are not confident which of the above two categories it \
belongs to.
   Example: "Can you approve this policy exception for this client?"
   Example: "The consent form looks wrong for this app, what should I do?"
   Example: "Where's the PDF?" (no identifier given — missing what structured_lookup needs)

Rules:
- When genuinely unsure between two categories, choose escalate. A wrong auto-answer is worse \
than a delayed human answer.
- Do not attempt to answer the question. Only classify it.
- Base your classification only on the text given — do not assume facts not stated.
"""


class ComposedAnswer(BaseModel):
    answer: str = Field(description="The drafted reply text, ready to send once approved.")
    cited_sources: list[str] = Field(
        description="Short references to what grounded this answer, e.g. tool/table names or knowledge base entry ids."
    )
    flagged_for_review: bool = Field(
        description="True if the grounding data looked incomplete, contradictory, or error-like and a human should double check before this goes out."
    )
    flag_reason: Optional[str] = Field(
        default=None, description="Required if flagged_for_review is true."
    )


COMPOSE_RESPONSE_SYSTEM_PROMPT = """\
You are drafting a reply to a colleague's question for Afficiency's internal RCA/Q&A assistant. \
You are given the original (PII-redacted) question and a grounding source — either a tool_result \
(from a live structured lookup) or a kb_match (a previously-approved knowledge base answer).

Hard rules:
1. Use ONLY facts present in the grounding source. Never state a fact, path, filename, date, or \
status that isn't explicitly in the source, even if it seems like a reasonable inference.
2. If the grounding source indicates something was expected but not found (e.g. a mapping \
resolved a path but the file doesn't exist there), say so plainly and note it's worth flagging \
for follow-up — do not present it as a simple "not found" dead end, and do not guess an \
alternative location.
3. Never restate personally identifiable information beyond what's operationally necessary \
(e.g. include a file path/link, not the applicant's name, DOB, or SSN, even if present in the \
source).
4. Cite what you're grounding the answer in, briefly (e.g. "based on the application's current \
status (final)..."), so the reader can verify it themselves.
5. Match the tone of a concise, direct colleague answering in chat — no preamble, no \
over-explaining, no filler like "I hope this helps."
6. If the grounding source itself looks incomplete, contradictory, or error-like rather than a \
real answer, do not compose a confident-sounding answer — set flagged_for_review=true and explain \
why in flag_reason instead of guessing.

## Examples

Example A — structured_lookup, forms found:
Source: {{"arc_id": "ARCF25344h646", "status": "final", "forms": [
  {{"form_number": "910", "form_name": "eConsent form", "bucket": "arc369", "key": "appsign/ARCF25344h646910.pdf", "exists": true}},
  {{"form_number": "911", "form_name": "HIPAA form", "bucket": "arc369", "key": "appsign/ARCF25344h646911.pdf", "exists": true}}
]}}
Answer: "Application ARCF25344h646 is in 'final' status. Both forms are in the appsign folder: \
eConsent (910) at arc369/appsign/ARCF25344h646910.pdf, HIPAA (911) at \
arc369/appsign/ARCF25344h646911.pdf."
cited_sources: ["resolve_pdf_locations"], flagged_for_review: false

Example B — structured_lookup, form expected but missing (diagnostic, not a dead end):
Source: {{"arc_id": "ARCF25344h697", "status": "final", "forms": [
  {{"form_number": "911", "form_name": "HIPAA form", "bucket": "arc369", "key": "appsign/ARCF25344h697911.pdf", "exists": false}}
]}}
Answer: "Application ARCF25344h697 is in 'final' status, and the HIPAA form (911) should be at \
arc369/appsign/ARCF25344h697911.pdf per the current mapping — but nothing exists at that path. \
That's likely a PDF generation gap rather than a missing record; worth flagging to engineering \
rather than treating as a simple 'not found'."
cited_sources: ["resolve_pdf_locations"], flagged_for_review: true,
flag_reason: "Mapping resolved a path but no file exists there — possible generation failure, not a normal not-found case."

Example C — kb_semantic_search:
Source: {{"canonical_answer": "Reset an agent's MFA from arcCenter Portal > Admin > Users > select agent > Reset MFA. Takes effect immediately, agent must re-enroll on next login.", "source_refs": ["agent_knowledge#142"]}}
Answer: "From arcCenter Portal: Admin > Users > select the agent > Reset MFA. Takes effect \
immediately — they'll need to re-enroll on next login."
cited_sources: ["agent_knowledge#142"], flagged_for_review: false
"""
```

## Task 2 — Wire into `assistant/graph.py`

Update `classify_intent`:
```python
model = get_model("classify").with_structured_output(IntentClassification)
result = model.invoke([
    SystemMessage(CLASSIFY_INTENT_SYSTEM_PROMPT),
    HumanMessage(state["redacted_text"]),
])
# store result.intent, result.reasoning, result.confidence in state for downstream nodes / audit logging
```

Update `compose_response`:
```python
model = get_model("compose").with_structured_output(ComposedAnswer)
grounding = state["tool_result"] or state["kb_match"]
result = model.invoke([
    SystemMessage(COMPOSE_RESPONSE_SYSTEM_PROMPT),
    HumanMessage(f"Original question: {state['redacted_text']}\n\nGrounding source: {grounding}"),
])
# store result.answer as draft_answer, result.flagged_for_review as needs_human_review
# (OR with the existing human_review_gate default of True — whichever is stricter wins)
```

`needs_human_review` in state should end up `True` if EITHER the existing `human_review_gate` stub
says so OR `ComposedAnswer.flagged_for_review` is true — never let a flagged answer skip review.

## Task 3 — Update `assistant/run_local.py`

For each test question, print: the classified intent, its reasoning and confidence, and — if the
graph reaches `compose_response` — the drafted answer, `cited_sources`, and
`flagged_for_review`/`flag_reason`. This makes the reasoning visible during local testing, not
just the final state dict.

## Acceptance check

Running `python -m assistant.run_local` should show, for the eConsent/HIPAA test question, an
`intent: structured_lookup` classification with reasoning, then (since `resolve_pdf_locations`
still raises `NotImplementedError` per the PDF resolver prompt's open items) a clean route to
`escalate` — not a crash, and not a fabricated `compose_response` answer.

## Explicitly out of scope

Tuning prompt wording against real traffic, adding more few-shot examples per question type, and
any model-specific prompt adjustments (e.g. if Groq's qwen3-32b needs different phrasing than
Haiku for reliable structured output) — validate once the graph is actually running against real
questions.
