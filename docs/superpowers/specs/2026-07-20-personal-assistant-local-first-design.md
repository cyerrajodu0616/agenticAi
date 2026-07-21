# Personal Assistant Agent — Local-First Design (A+C Hybrid)

**Date:** 2026-07-20
**Status:** Draft for review
**Supersedes model layer of:** `docs/personal-assistant-plan.md` (graph, KB schema, and review-gate concepts from that plan are carried forward)

## 1. Goal

A personal assistant agent that:

1. Reads incoming Teams messages, emails, and dropped files.
2. Drafts replies and shows them to the user for approval **before** anything is sent to the original person.
3. Can fix data-sync issues by calling a known set of internal APIs reachable from the user's machine (with approval).
4. Can perform ad-hoc analysis by drafting scripts the user reviews and approves before execution.
5. Continuously grows a knowledge base from files, Teams messages, and emails, so repeat questions get answered without the user.

**Privacy requirement:** target state is strictly local LLM inference (RTX 4060 PC on the LAN). Cloud LLM APIs (Groq/Gemini/Claude keys already in `.env`) are permitted **only during initial setup/development**, and always behind local PII redaction. A single config switch flips backends.

## 2. Hardware / deployment topology

| Machine | Runs |
|---|---|
| RTX 4060 PC (Windows/Linux, on LAN) | Ollama server (models below), Open WebUI for manual chat/testing |
| Mac (this repo) | LangGraph agent service (FastAPI), Postgres + pgvector (Docker), drop-folder watcher (Python first, n8n later), review inbox, tool registry executor, script workbench |

The 4060 box exposes Ollama's OpenAI-compatible API on the LAN (e.g., `http://<4060-ip>:11434/v1`). Nothing on the Mac depends on *which* backend serves the API — cloud and local are interchangeable behind the model factory.

### Models (local backend)

| Role | Model | Why |
|---|---|---|
| classify | `qwen3:8b` (Q4) | Fits 8GB VRAM fully offloaded; strong instruction following for structured output |
| compose | `qwen3:8b` (Q4) | Same model, different prompt/role |
| coder | `qwen2.5-coder:7b` (Q4) | Best small coder model for the script workbench |
| embed | `nomic-embed-text` | Local embeddings for pgvector |

8GB VRAM constraint: 7–8B @ Q4 (~5–6GB) runs fully on GPU. 14B+ spills to CPU and becomes 5–10× slower — the design uses small models for narrow jobs instead of one large model.

## 3. Model layer

`assistant/models.py` — single factory `get_model(role)` driven by `.env`:

```
MODEL_BACKEND=cloud | local      # cloud for initial setup, local is target
OLLAMA_BASE_URL=http://<4060-ip>:11434
```

| Role | cloud (initial) | local (target) |
|---|---|---|
| classify | Groq `qwen/qwen3-32b` | Ollama `qwen3:8b` |
| compose | Gemini `gemini-2.5-flash` | Ollama `qwen3:8b` |
| coder | Claude Haiku / Gemini | Ollama `qwen2.5-coder:7b` |
| embed | Gemini embedding | Ollama `nomic-embed-text` |

Rules:

- `redact_pii` runs before **every** LLM call in both modes. Cloud mode never sees unredacted text.
- Unknown role → `ValueError` (no silent fallback).
- Embedding backends have different dimensions: raw text is always stored next to vectors, and a `reembed` batch command re-vectorizes the whole KB when the embed backend changes. The vector column dimension is configured, not hardcoded.

## 4. Agent graph (LangGraph)

Carried forward from `personal-assistant-plan.md`, extended with the two new action routes:

```
ingest → redact_pii → classify_intent → route:
   ├─ kb_answer      — pgvector search over agent_knowledge + product_knowledge
   │                    → compose draft reply (grounded ONLY in retrieved rows)
   ├─ sync_fix       — match request against tool registry → prepare exact API call
   ├─ analysis_task  — coder model drafts a script into the workbench
   └─ escalate       — none of the above / low confidence → straight to user
all paths → review_inbox → (user approves/edits/rejects) → dispatch → learn
```

- LLM nodes only: `classify_intent`, `compose`, script drafting. Everything else is deterministic Python.
- `review_inbox` is a hard gate: **nothing** is sent, executed, or run without explicit approval. No auto-send in v1; relaxing per question-type is a later, earned change.
- `learn`: every approved answer is embedded and upserted into `agent_knowledge` with `source_refs`, so the same question next time resolves without the user.
- `dispatch` in phase 1 = copy approved reply to clipboard / show it for manual paste into Teams/Outlook. Sending via Graph API is a later phase.

## 5. Action layer

### 5.1 Tool registry (known sync-fix APIs)

`tools/registry.yaml` — one entry per internal API:

```yaml
- name: resync_application
  description: "Re-runs data sync for an application id"
  method: POST
  url: "http://internal-host/api/resync/{arc_id}"
  params:
    arc_id: {type: string, pattern: "ARC.*"}
  risk: medium            # low | medium | high
```

- The LLM never constructs URLs or free-form requests. It selects a registry entry and extracts parameters via structured output (Pydantic).
- Deterministic code validates params against the schema, renders the call, and shows it in the review inbox.
- v1: **all** risk levels require approval. Auto-execute for `risk: low` is a config flag left off until trust is earned.
- Execution results (status, response body summary) are attached to the draft reply for the requester.

### 5.2 Script workbench (ad-hoc analysis)

- The coder model writes a self-contained script into `workbench/<task-id>/` (script + a `TASK.md` describing intent, inputs, expected output).
- The user reviews the actual code in the inbox.
- On approval, it runs in a Docker container: **no network**, read-only mount of only the input data it needs, writable `out/` directory, CPU/memory limits, timeout.
- Output lands back in the draft reply. Scripts are kept (not deleted) — recurring analyses graduate into the tool registry as named tools.

Safety principle throughout: **LLM proposes, code disposes.** Model output is always data (a registry choice, a draft, a script awaiting review) — never a directly executed side effect.

## 6. Knowledge base

Postgres + pgvector (Docker on the Mac). Tables carried from the prior plan, plus a raw-document store:

- `agent_knowledge` — canonical Q→A pairs learned from approved answers (schema per `personal-assistant-plan.md` §3, with `embedding_model` + configurable vector dimension added).
- `product_knowledge` — reference material: specs, docs, curated skill content (`source_type IN ('skill','code','spec','doc')`).
- `agent_escalations` — pending/resolved human-in-the-loop queue; doubles as audit log.
- `raw_documents` — every ingested item's parsed text + metadata (source, sender, date, thread id, file hash for dedup). Vectors reference this so re-embedding is always possible.

`kb_answer` searches `agent_knowledge` and `product_knowledge`, merges by cosine similarity with a threshold (module constant, tune via fixtures), and `compose` must cite which rows it drew from.

## 7. Ingestion pipeline (drop folder → Graph API later)

### Phase 1 — drop folder (no permissions needed)

Watched folder `~/assistant-inbox/`:

```
new file → detect type (.eml, .msg, .txt, .md, .pdf, pasted Teams export)
  → parse to common schema {source, sender, date, thread_id, body, attachments}
  → redact → dedup (file hash) → chunk → embed → raw_documents + vectors
  → route: is it a QUESTION addressed to the user?  → also enters the agent graph (§4)
           is it REFERENCE material?                → product_knowledge only
```

Watcher is plain Python (`watchdog`) first; n8n can take over the watching/parsing plumbing at any point without changing the downstream API (the watcher just POSTs the common schema to the LangGraph service).

### Phase 2 — Microsoft Graph via n8n

n8n's Microsoft Outlook / Teams trigger nodes replace manual drops. Requires an Azure AD app registration on the afficiency.com tenant — ask IT for **delegated** permissions: `Mail.Read`, `Chat.Read`, `ChannelMessage.Read.All`, and later `Mail.Send` / `ChatMessage.Send` for dispatch. This is the long-pole item; request it in parallel early. Everything downstream of the common schema is unchanged.

### Division of labor (the A+C hybrid)

- **n8n**: plumbing — watching, fetching, OAuth-heavy connectors, delivery.
- **LangGraph**: thinking — classify, route, compose, learn.
- **User**: approval gate for anything that leaves a machine or changes state.
- **Open WebUI** (4060 box): manual chat with the local models; used for prompt experiments and side-by-side model comparison, not part of the pipeline.

## 8. Error handling

- Any node failure routes to `escalate` — fallback is always "a human sees it," never a dropped message.
- Ollama unreachable (4060 off): queue as pending + notify; **no silent fallback to cloud** in local mode.
- DB unreachable: fail loudly at startup, not per-message.
- Unparseable dropped file: moved to `~/assistant-inbox/failed/` with a reason file.
- Registry call failures: full request/response captured into the escalation record.

## 9. Testing

- `fixtures/` — ~10 real (redacted) messages the user actually received; pytest runs each through the graph and asserts routing (`kb_answer` / `sync_fix` / `analysis_task` / `escalate`).
- The same fixture suite is the cloud-vs-local comparison harness when flipping `MODEL_BACKEND` — routing decisions should not regress.
- Unit tests: redaction patterns, registry param validation, parser per file type, chunking.
- Sandbox test: a deliberately malicious fixture script (attempts network + writes outside mount) must fail inside the container.

## 10. Build order

1. **Scaffold** — `assistant/` package, config, model factory (cloud backend), redaction, graph skeleton, hardcoded test questions. Runnable day one.
2. **KB** — Postgres+pgvector Docker compose, schema DDL, drop-folder watcher + parsers, ingestion to `raw_documents`/vectors.
3. **Review loop** — CLI review inbox, `learn` node, clipboard dispatch.
4. **Actions** — tool registry + the first real sync-fix API.
5. **Go local** — Ollama on the 4060, `MODEL_BACKEND=local`, `reembed`, fixture comparison run; Open WebUI on the 4060.
6. **Workbench** — Docker sandbox + coder-model script drafting.
7. **Graph API** — n8n + Azure app registration (requested from IT back at step 1).

## 11. Out of scope (v1)

- Auto-send without review (earned later, per question-type).
- Sending replies via Graph API (phase-1 dispatch is manual paste).
- Multi-user support; this is a single-operator personal assistant.
- Fine-tuning local models.
- HubSpot/ticketing channels (the prior plan's FAQ-mining pipeline remains a compatible later addition).
