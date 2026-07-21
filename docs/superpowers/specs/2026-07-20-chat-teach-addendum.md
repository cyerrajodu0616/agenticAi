# Chat Teach & Task Interface — Spec Addendum

**Date:** 2026-07-20 · **Extends:** `2026-07-20-personal-assistant-local-first-design.md` · **Status:** Approved (user, this session)

## Goal

A conversational terminal interface (`python -m assistant.chat`) to teach the assistant, query/maintain the KB, and work the task queue — replacing none of the existing review CLI, layering on top of the same tables.

## Behavior

Each user message is PII-redacted, then routed by a chat-intent classifier (same `classify` model role, structured output) to:

| action | behavior |
|---|---|
| `teach` | LLM extracts a proposed `{question, answer}` pair from the statement; shown to user; saved via `kb_learn(created_by='chat')` ONLY on explicit confirm (y / edit / n) |
| `ask` | `kb_search` + grounded, source-cited answer (compose role); no writes |
| `tasks` | one view listing pending `agent_escalations` + pending `review_items` |
| `resolve` | user supplies the answer for escalation N; LLM extracts resolution text; on confirm: learn Q→A into KB **first**, then mark escalation resolved (closes the learn loop the core build left open) |
| `edit_kb` / `delete_kb` | search shows matching entries with ids; update re-embeds if the question changed; delete requires typed confirmation |
| `other` | help text; never guesses |

## Invariants (unchanged from base spec)

- LLM output is always a **proposal** (structured data); every DB write requires explicit user confirmation in the terminal.
- `redact()` before every LLM call; raw text never reaches a model.
- Learn-before-mark ordering on resolve (retryable on embedding failure — same atomicity rule as `review.approve`).
- No new tables; uses `agent_knowledge`, `agent_escalations`, `review_items`.

## Out of scope

Voice, web UI, multi-turn memory beyond the current session, auto-teach without confirmation.
