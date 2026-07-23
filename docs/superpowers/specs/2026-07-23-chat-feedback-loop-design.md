# Chat Feedback Loop — Design

**Date:** 2026-07-23 · **Extends:** the chat "ask" flow (`assistant/chat.py`'s `answer_from_kb`, `assistant/web/app.py`'s `/api/chat`) and the existing teach pipeline (`kb_learn`/`kb_learn_pending`). Status: Approved (user, this session).

## Goal

Tonight's Graphify work found three real ranking/retrieval bugs, each requiring me to manually reproduce, diagnose, and fix the underlying query. There is no way for the user (or a peer) to flag a wrong answer themselves — `answer_from_kb()` computes the full ranked hit list (`kb_search()`) but only returns the final composed answer string, discarding the hits; nothing about a chat "ask" interaction is persisted anywhere. Close both gaps: save every ask interaction, and let the user correct a wrong one directly from the chat, feeding the correction back into the KB so similar future questions get it right.

## Non-goals (explicitly out of scope for this pass)

- **No new override/boost ranking system for Graphify search.** Considered and rejected in favor of reusing the existing local-KB pipeline (see Correction mechanism below) — a second parallel ranking mechanism to keep in sync with tonight's already-tuned Graphify ranking is unnecessary complexity.
- **No LLM-composed rewrite of a picked source's content.** "Pick from candidates" uses the chosen hit's `content` field verbatim as the new KB answer — no extra compose call. The user can edit it afterward via the KB tab's existing inline-edit, same as any other KB entry.
- **No feedback on "teach"/"edit_kb"/"resolve" chat actions.** Scoped to "ask" only, where the retrieval-ranking problem actually lives.
- **No changes to Graphify's own ranking logic** (exact/product-scoped/semantic paths, weighting, boosts) — this feature works entirely through the existing local KB + Graphify merge in `kb_search()`, which ranks both together by similarity.

## Data model

New table, `chat_history`:

```sql
CREATE TABLE IF NOT EXISTS chat_history (
    id          BIGSERIAL PRIMARY KEY,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    sources     JSONB NOT NULL,   -- the full hit list answer_from_kb was grounded in:
                                  -- [{source, title, content, similarity}, ...]
    created_by  TEXT NOT NULL,    -- 'local' | 'peer', same distinction as kb_learn's
                                  -- created_by and the existing peer-gate pattern
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Every "ask" interaction is persisted automatically and silently — no user action needed for the row to exist, matching "the chats are not saved" being the first problem to close. `sources` stores full hit dicts (not just ids) so a later correction never needs to re-query Graphify's live remote DB for content that may have changed or become unreachable by then.

## Correction mechanism

**Reuses the existing teach pipeline instead of a new system.** Both correction modes ultimately call the existing `kb_learn()` (local) or `kb_learn_pending()` (peer):

- **"Pick a different source"** — user selects one of the hits already shown (from `chat_history.sources`). The new KB entry is `question=<original question>`, `answer=<picked hit's content, verbatim>`.
- **"Write the correct answer"** — identical to the existing teach flow; the new KB entry is `question=<original question>`, `answer=<user-typed text>`.

Because `kb_search()` merges local `agent_knowledge` and Graphify hits and sorts by raw similarity (`assistant/kb.py:39`), a correction's near-1.0 self-similarity makes it reliably outrank the original wrong Graphify hit for identical or near-identical future questions. For genuinely different phrasings of a similar question, both the correction and the bad Graphify hit compete on similarity — the correction usually helps and often dominates, but this is a probabilistic improvement rather than a hard override. No new ranking/override table required; the correction still reaches the LLM as context via `answer_from_kb`'s existing multi-hit grounding. Product-specificity (a concern raised this session: the same conceptual question can have a different correct answer per product) comes from the corrected question's own text carrying the product context, and from embedding similarity naturally separating differently-scoped questions — the same mechanism that already keeps unrelated `agent_knowledge` entries apart today.

**Peer-gating stays consistent with the existing pattern.** A correction submitted through a peer's ngrok session is gated behind local-user approval before it reaches `agent_knowledge`, exactly like peer-submitted KB entries (`assistant/web/app.py`'s `_is_local_request` gate, already built and tested). A local correction writes directly.

## API changes

- **`assistant/chat.py`'s `answer_from_kb(text: str)`** changes its return type from `str` to `tuple[str, list[dict]]` (`answer`, `hits`) — the hits were already being computed, just discarded. Both call sites need updating: `run_repl` (prints `answer`, ignores `hits` — the REPL has no correction UI) and `web/app.py`'s `chat_endpoint`.
- **`chat_endpoint`'s "ask" response** gains two fields: `{"action": "ask", "answer": ..., "chat_id": <int>, "sources": [...]}`. `chat_id` is the new `chat_history` row's id, needed to reference it in a later correction call. Persistence happens inside `chat_endpoint`, right after composing the answer — a new small helper in `assistant/kb.py` (or a new `assistant/chat_history.py`, matching this project's one-file-per-responsibility pattern) does the insert.
- **New endpoint: `POST /api/chat/{chat_id}/correct`** — body is one of:
  - `{"mode": "pick", "source_index": <int>}` — looks up `chat_history.sources[source_index]`, calls `kb_learn`/`kb_learn_pending` with that hit's content as the answer.
  - `{"mode": "write", "answer": "<text>"}` — calls `kb_learn`/`kb_learn_pending` with the given text.
  - Returns `{"status": "learned", "id": ...}` or `{"status": "pending_approval", "id": ...}`, mirroring `/api/teach/confirm`'s existing response shape.
- **New endpoint: `GET /api/chat/history`** — most-recent N rows from `chat_history` (question, answer, sources, created_at), so a wrong answer can be corrected after the fact, not only in the same browser session it was asked in.

## Frontend

- **Chat tab:** after an "ask" response renders, show a small "Was this right?" affordance with two actions — "Pick a different source" (expands `sources` as a radio-button list) and "Write the correct answer" (reuses the existing teach-form styling). Both submit to `/api/chat/{chat_id}/correct`.
- **New "History" section** (in the Chat tab or its own small tab, consistent with the existing Knowledge/Tasks tab pattern): a simple list from `GET /api/chat/history`, each row showing question/answer/timestamp with the same "Was this right?" affordance.

## Testing

- Unit tests for `answer_from_kb`'s new `(answer, hits)` return shape, and for `run_repl` still working with the tuple.
- Unit tests for chat persistence (mocked DB) — confirm every "ask" response, including the "nothing found" case, gets a `chat_history` row.
- Unit tests for `/api/chat/{chat_id}/correct`: local pick-mode and write-mode write directly via `kb_learn`; peer pick-mode and write-mode gate via `kb_learn_pending`, mirroring the existing `test_teach_confirm_*` tests' local-vs-peer pattern (including the DNS-rebinding case already covered there).
- Unit test for `GET /api/chat/history` shaping.
