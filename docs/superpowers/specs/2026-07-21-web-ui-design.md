# Local Web UI — Design

**Date:** 2026-07-21 · **Extends:** the core loop + chat-teach features already on `main`. Status: Approved (user, this session).

## Goal

A browser-based alternative to `python -m assistant.chat` / `python -m assistant.review`: ask/teach/correct via a chat box, browse and edit the KB directly, and work the review inbox — all on `localhost`, no new infra.

## Non-goals

No auth (single trusted local user; server binds `127.0.0.1` only, never `0.0.0.0`). No build tooling (no React/Vue/npm) — one static HTML page, vanilla JS, inline CSS. No new persistence — reuses the existing Postgres tables and `assistant/kb.py`, `assistant/tasks.py`, `assistant/review.py`, `assistant/chat.py`'s proposal functions as-is. Does not replace the CLIs; all three surfaces (web, chat REPL, review CLI) stay usable.

## Architecture

```
assistant/web/
  __init__.py
  app.py            # FastAPI app + all routes
  static/
    index.html      # single page: 3 tabs (Chat / Knowledge Base / Tasks), vanilla JS + inline CSS
```

Entry point: `python -m assistant.web` starts uvicorn on `127.0.0.1:${WEB_PORT}` (config default `8765` — Graphify already owns `8080`).

**Confirm-before-write, adapted for HTTP.** The CLI's `handle_teach`/`handle_edit_delete` block on `input()` for their multi-step confirm dialogs — that doesn't translate to request/response. The web UI does NOT call those REPL handlers. It calls the proposal layer they're built on (`classify_chat`, `extract_teach_pair`, `extract_resolution`, `answer_from_kb` from `chat.py`; `kb_find`/`kb_get`/`kb_update`/`kb_delete`/`kb_learn` from `kb.py`; `list_open`/`get_escalation`/`resolve_escalation` from `tasks.py`) directly, and implements its own two-step pattern: one endpoint returns a *proposal* (pure read, no write), a second endpoint — hit only when the browser's Confirm button is clicked, carrying the exact confirmed values — performs the write. This preserves the "nothing writes without an explicit user action for that exact data" invariant from every other surface in this system.

## API surface

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/chat` | `{text}` → runs `classify_chat` server-side, returns a JSON proposal shaped by `action` (`ask`→`{action, answer}`; `teach`→`{action, question, answer}`; `edit_kb`/`delete_kb`→`{action, matches: [...]}`; other→`{action, message}`). Never writes. |
| POST | `/api/teach/confirm` | `{question, answer}` → `kb_learn(created_by="web", source_refs=["web"])` |
| GET | `/api/kb` | `?q=` optional → `kb_find(q)` if given, else most-recent N rows (new small helper, see Task 1) |
| PATCH | `/api/kb/{id}` | `{question?, answer?}` → `kb_update` |
| DELETE | `/api/kb/{id}` | → `kb_delete` |
| GET | `/api/tasks` | → `list_open()` shaped as JSON (escalations + drafts); polled every 10s by the Tasks tab |
| GET | `/api/review/{id}` | → `review.show(id)` |
| POST | `/api/review/{id}/approve` | `{edited_text?}` → `review.approve` |
| POST | `/api/review/{id}/reject` | → `review.reject` |
| POST | `/api/escalation/{id}/draft-resolution` | `{text}` (body — free text doesn't belong in a query string) → `extract_resolution(text, escalation.question_text)`, read-only despite the verb |
| POST | `/api/escalation/{id}/resolve` | `{resolution_text}` → `resolve_escalation(id, resolution_text, resolved_by="web")` |
| GET | `/` | serves `static/index.html` |

All write endpoints require the exact data the browser already showed the user (no server-side "re-derive and trust" step) — e.g. `/api/teach/confirm` takes the literal `question`/`answer` strings the proposal returned (possibly hand-edited in the browser first), not just an id referencing server-held state.

## Frontend

Single `index.html`, three tabs via simple JS show/hide (no router library):
- **Chat tab**: text input + send. Renders the proposal inline with Confirm/Discard (teach) or a picker + inline edit fields (correct). Ask responses just render as text with citation.
- **Knowledge Base tab**: table (question, answer, id), a search box (calls `GET /api/kb?q=`), inline edit (click a row → editable fields → Save/Cancel) and a Delete button requiring a confirm dialog (`window.confirm`, consistent with the CLI's typed-"delete" friction).
- **Tasks tab**: two lists — Drafts (Approve / Edit-then-approve / Reject buttons) and Escalations (textarea + "Draft resolution" button that calls the LLM-polish endpoint, then a Confirm & Resolve button). Polls `GET /api/tasks` every 10s and re-renders only if the payload changed (avoid flicker).

## Error handling

Any backend exception → JSON `{"error": "..."}` with a 4xx/5xx status; the frontend shows it inline near the action that failed, never a silent failure. `config.validate()` + `init_schema()` run at app startup, matching `run_local.py`/`chat.py`'s pattern — the server refuses to start with a clear error if the DB or required keys aren't configured.

## Testing

FastAPI's `TestClient` (starlette) for every endpoint — mock the underlying `kb`/`tasks`/`chat`/`review` functions the same way existing unit tests mock `get_model`/`kb_search`/etc. No browser/JS test framework introduced; the frontend is simple enough that endpoint coverage plus one manual click-through acceptance pass (documented, like the chat-teach plan's Task 3) is the bar.

## Out of scope (future)

Multi-user auth, remote access, websockets/SSE (polling is enough at this scale), editing `product_knowledge` (KB tab is `agent_knowledge` only, matching `kb_find`'s existing scope), a build step / component framework.
