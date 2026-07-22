# Personal RCA/Q&A Assistant — Architecture Plan

Goal: an agent that reads incoming questions from peers/helpdesk/business (Teams, Email, ticketing), answers the ones it already knows, and escalates the rest to you — capturing your answer so it never has to escalate that question again.

Pilot case: "Where's the eConsent/HIPAA PDF for arcId X" (ARCF25344h646, ARCF25344h697).

## 0. What you already have

`agenticAi` repo: `langchain`, `langchain-anthropic`, `langchain-groq`, `langchain-google-genai` installed, `.env` with GROQ/GOOGLE/CLAUDE keys, and working notebooks calling `init_chat_model` for Groq/Gemini with structured output. No `langgraph` yet, no persistence layer, no channel adapters.

Need to add: `langgraph`, `langgraph-checkpoint-postgres`, `psycopg2-binary` (or `psycopg[binary]`), `pgvector`, plus whichever SDK matches your channels (Microsoft Graph SDK for Teams/Email, Jira/Asana SDK for tickets).

## 1. Scope the MVP tightly

Don't build all three channels + a general knowledge base at once. Start with one channel (pick the one with the most volume — email or Teams) and one question type (the consent-PDF lookup). Prove the loop end to end, then widen.

## 2. PII handling (this drove your "local LLM" instinct — here's how to satisfy it without giving up cloud model quality)

- Redact before any cloud call: run a local regex/NER pass on the incoming question text (SSNs, DOB, full names, policy numbers) before it reaches Groq/Gemini/Haiku. Keep the reversible mapping only in local memory for that request.
- Never put document content in a prompt. Data lookups (e.g., "get the consent PDF for this arcId") are deterministic tool calls straight to Postgres/S3 — the LLM only ever sees the tool's structured result (a link + status), never the PDF or raw DB row.
- If a question can't be redacted safely (e.g., it's fundamentally about a person's PII), route it to a local model (Ollama running Llama/Qwen) instead of a cloud API, or straight to human escalation.
- This means "PII-safe" isn't a separate system — it's just node ordering: redact → classify → tool-call-or-search → compose, with raw PII never crossing the redact boundary.

## 3. Knowledge store: Postgres + pgvector (your call, and it fits — you already run Postgres on portal-backend-az)

Two tables, one Postgres instance:

- `agent_knowledge` — id, question_embedding (vector), canonical_question, canonical_answer, tags[], source_refs, created_by, confidence, hit_count, last_used_at. Semantic memory for "how do I / where is" style questions.
- `agent_escalations` — id, source_channel, thread_id, sender, question_text, status (pending/resolved), resolution_text, resolved_by, resolved_at. This is your human-in-the-loop queue AND doubles as an audit log — useful the next time CG needs proof of how/when a question was answered.

Structured lookups (like "consent PDF path for arcId X") don't belong in the vector table — they're a direct parameterized query against your real schema, registered as a LangGraph tool.

## 4. LangGraph graph

```
ingest -> redact_pii -> classify_intent -> route
                                             ├─ structured_lookup (tool call, e.g. get_consent_pdf(arcId))
                                             ├─ kb_semantic_search (pgvector, threshold match)
                                             └─ escalate (write agent_escalations, notify you)
        structured_lookup / kb_semantic_search -> compose_response -> human_review_gate -> dispatch_reply
        escalate -> [wait for your reply] -> learn (embed Q+A into agent_knowledge) -> dispatch_reply
```

- `classify_intent` and `compose_response` are the only LLM nodes. Everything else is deterministic code/tool calls.
- `human_review_gate`: for the first few weeks, every auto-drafted answer goes to you for a thumbs-up before sending. Once confidence is proven for a question type, flip that type to auto-send. This matches "zero breakage tolerance" — nothing goes out unreviewed until it's earned trust.
- `learn`: whenever you resolve an escalation, that Q+A gets embedded and upserted into `agent_knowledge`. This is the "keep adding knowledge" loop you asked for.

## 5. Model routing (matches your dev/prod split)

A single `get_model(role, env)` factory:

- `classify` role: Groq/Gemini in dev, Claude Haiku in prod.
- `compose` role: Groq/Gemini in dev, Claude Haiku in prod.
- Both roles only ever see redacted text.

Driven by an `ENV=dev|prod` var in `.env`, alongside the keys you already have there.

## 6. Channel adapters

Keep these thin — each just maps a webhook/poll event into a common `IncomingQuestion` schema and calls the graph; all logic stays in the graph, not the adapter.

- Teams: Microsoft Graph API / Bot Framework webhook.
- Email: Microsoft Graph mail subscription (since Teams implies Microsoft 365).
- Ticketing: Jira/Asana webhook on comment/assignment (you already have an Asana MCP connected in this session, worth checking if it can serve this instead of raw webhooks).

## 7. Closing the loop on the 2 audit PDFs

I don't have live access to arc369, your DB, or S3/Blob in this session, so I can't pull these myself. Manual step to unblock the audit now, and to seed `agent_knowledge` #1 once resolved:

1. Query whatever schema holds these two apps (likely `arcCenter_apps_PID` per your pipeline docs) — `consentDetails` and `signatureDetails` tables — for `arcId IN ('ARCF25344h646','ARCF25344h697')`. Look for any column beyond timestamps/booleans: a path, URL, key, docId, s3Key, blobPath, or fileName. I don't know the real column names — don't guess, check the actual schema.
2. If a doc reference exists in the DB but nothing lives at that path in S3/Blob, that's your RCA: PDF generation ran (or was recorded as run) but the file itself was never written — flag to engineering, not just to the audit as "missing."
3. If no doc-reference column exists at all, check whether PDF generation was even wired for the product/version these two apps went through — some flows only store a consent flag in the DB, no physical PDF, which would explain why neither you nor CG can find one.

Once you get the real answer, that becomes the first seeded row in `agent_knowledge` (canonical_question: "where is the eConsent/HIPAA PDF for a given arcId", canonical_answer: the actual retrieval procedure/path pattern).

## 8. Knowledge base bootstrapping (don't start from zero)

There are two distinct knowledge sources — keep them in separate tables, don't blend them:

### 8a. FAQ mining from historical chat (Teams / helpdesk tool / HubSpot)

Status as of this session:
- HubSpot: a HubSpot connector is already active in this workspace, but pulling conversation data failed with `INSUFFICIENT_SCOPE` — the connected app is missing the `conversations.read` permission ("View details about threads in the conversations inbox"). Fix: in HubSpot, go to the app's settings → Scopes, enable `conversations.read` (add `conversations.write` too if you later want the agent to post replies from HubSpot directly), then reauthorize. Once that's on, `search_conversations` can pull historical threads by time range or text query, and `get_conversation_channel_metadata` lists the actual inboxes/channels to filter on — no connector build needed, it's ready to use.
- Microsoft Teams: no MCP connector available in the registry. Pulling Teams chat history means a custom Microsoft Graph API integration (app registration + `Chat.Read`/`ChannelMessage.Read.All` permissions). This is a build item, not a connect-and-go.
- "Dedicated helpdesk/ticketing tool": not yet named — tell me which one (Zendesk, Freshdesk, Jira Service Management, etc.) and I'll check the connector registry for it specifically.

Pipeline (same shape regardless of source, per the best practices above):
```
pull_transcripts (source-specific: HubSpot API / Graph API / ticketing API)
   -> redact_pii
   -> filter (resolved threads only, drop internal noise)
   -> embed_questions
   -> cluster (similarity threshold, e.g. 0.85 cosine)
   -> canonicalize (LLM proposes 1 question + 1 answer per cluster, grounded in the actual resolved answers in that cluster — not its own knowledge)
   -> pending_review (human approves/edits)
   -> upsert into agent_knowledge (confidence=1.0, source_refs=[thread ids], created_by=you)
```
Run this as a batch job first (one-time historical backfill), then as the same recurring job used for the live `learn` node — the live and bootstrapped paths converge on the same `pending_review` gate.

### 8b. Product knowledge from specs + codebase

You already have the first version of this: the `arcenter-engine`, `add-allapps-column`, `unified-portal`, and `module5-dev-guide` skills are hand-curated, reviewed product knowledge. Best practice is to treat curated docs like these as ground truth, and auto-indexed code as a supplementary layer underneath it — never the other way around.

- Bootstrap step 1 (free): load those four skills' markdown directly into a new `product_knowledge` table as pre-approved entries (confidence=1.0, source_type='skill', created_by=<skill owner>). No extraction needed, they're already reviewed.
- Bootstrap step 2 (build): a lightweight codebase indexer — chunk by logical unit (function/class/endpoint/config block), embed each chunk with its docstring + signature + file path, tag with `source_type='code'`, `source_path`, `last_verified_commit`. Store separately from step 1 so a retrieval query can't confuse "reviewed fact" with "raw code guess."
- Include specs too (PRDs, ADRs, Confluence/Notion pages) as `source_type='spec'`, attributed to a section/URL — every canonical answer should be traceable to a real source, not just LLM synthesis, the same auditability instinct as the CG audit case.
- Refresh on change, not on a timer: incremental re-index on merge-to-main (CI hook, changed files only) for freshness, plus a weekly full re-sync as a safety net via the `schedule` skill. Attach `last_verified_commit` to every entry so a changed source file flags that entry as stale instead of silently serving outdated info.

`product_knowledge` schema (parallel to `agent_knowledge`):
```sql
CREATE TABLE IF NOT EXISTS product_knowledge (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('skill','code','spec')),
    source_path TEXT NOT NULL,
    symbol TEXT,
    snippet TEXT NOT NULL,
    snippet_embedding VECTOR(768),
    last_verified_commit TEXT,
    verified_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
```
`kb_semantic_search` queries both `agent_knowledge` and `product_knowledge`, merges/ranks by similarity, and `compose_response` cites which table/source it drew from.

## 9. Build order

1. `langgraph` + Postgres/pgvector setup, schema DDL for the two tables (`agent_knowledge`, `agent_escalations`).
2. `get_model()` factory + redaction util.
3. Graph skeleton with stub tools (no real channel yet) — test with a hardcoded question list including the PDF-lookup case.
4. Bootstrap `product_knowledge` from the 4 existing skills (free, no extraction) — Section 8b step 1.
5. Fix the HubSpot `conversations.read` scope, then build the FAQ-mining batch job (Section 8a) and run it once as a historical backfill.
6. One real tool: `get_consent_pdf(arcId)` once you've confirmed the actual schema/column.
7. One real channel adapter (whichever you pick first — Teams needs a Graph API build, the named helpdesk tool may have a ready connector).
8. `human_review_gate` on, watch it for a few weeks, then start relaxing per question-type.

See `langgraph-personal-assistant-mvp-prompt.md` for the Claude Code prompt to scaffold steps 1–3.
