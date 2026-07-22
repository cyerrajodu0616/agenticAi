# Graphify Live Integration — Design

**Date:** 2026-07-22 · **Extends:** `assistant/graphify.py`'s existing (currently nonfunctional) additive knowledge source. Status: Approved (user, this session).

## Goal

`assistant/graphify.py` today calls a local HTTP service (`Graphify-ArcCode`'s `run_local_poc.sh`) that requires a separate `az login`'d process to be running — in practice it's never running, so `graphify_search()` silently returns `[]` and the arcCenter config knowledge it should provide never surfaces. Replace the HTTP call with a live, read-only SQL query against the arcCenter dev Postgres, which was confirmed this session to already hold a full, recently-indexed dataset (`arc_config_kb` schema, last successful index run 2026-07-07: 1,564 functions, 8,592 UW rules, 34,861 graph nodes, 13,139 embedded KB entries, etc.) — no new data pipeline needed, just a different way to reach data that already exists.

## Non-goals (explicitly out of scope for this POC)

- **No local copy/sync of `arc_config_kb`.** Live query only; considered and rejected a `pg_dump`/`pg_restore` snapshot approach — the data would go stale immediately (indexed 2 weeks ago already) and a sync job is more to build/maintain than a live query.
- **No Container App / managed-identity / connection-pooling productionization.** That's the right architecture for an eventual org-wide, hosted deployment (routing all users' Graphify queries through one pooled, managed-identity-scoped backend connection instead of N individual `az`-authenticated processes), but building it now is designing for a requirement that doesn't exist yet. Revisit when this POC is validated and an actual deployment is scheduled.
- **No production DB / log-searching "data agent" connectivity.** A future tool that searches prod logs to diagnose application state needs prod DB access, which is a categorically different, heavier initiative — dev/prod network isolation is a deliberate security boundary, prod logs likely carry real applicant/customer PII requiring much stricter gating than arcCenter's internal config metadata, and it will need its own security review. Not designed here; the connection layer built for this POC is a small, swappable piece that doesn't block scoping that work separately later.
- **No changes to this project's own embedding dimension.** Stays at `EMBED_DIM=768`; see Embedding compatibility below for why matching Graphify's 1536-dim data doesn't require this.

**Known risk, flagged not fixed:** pointing a (future org-wide) production tool's live queries at arcCenter's *dev* Postgres couples this assistant's uptime/behavior to whatever else happens to that dev instance (schema changes, dev-only outages, another team's migration). Acceptable for a local POC; needs revisiting before any wider rollout.

## Connectivity & credentials

Live query, no local copy. This project cannot import `Graphify-ArcCode`'s `resolve_dsn()` — that's a separate, unrelated git repo, not an installed dependency. It needs its own minimal DSN resolution, mirroring the priority chain already proven to work today (verified live this session):

1. `ARC_CONFIG_KB_DSN` env var (explicit override — also what tests use, so CI/unit tests never touch the real remote DB).
2. Shell out to `az keyvault secret show` against `afficiency-dev-kv` (same Key Vault, same secrets `Graphify-ArcCode`'s own `resolve_dsn()` reads) — reuses the `az` CLI auth already confirmed working on this machine, no new Azure SDK dependency (`azure-identity`/`azure-keyvault-secrets`) for a POC.

New config in `assistant/config.py`: `ARC_CONFIG_KB_DSN` (optional, empty by default), `GRAPHIFY_ENABLED` stays as the master on/off switch. If DSN resolution fails for any reason (no `az` login, Key Vault unreachable, VPN off), `graphify_search()` degrades to `[]` exactly like the current HTTP-failure path — this is still an optional, additive source, never a hard dependency.

Connects with the project's existing `psycopg` (v3) driver — already a dependency (`assistant/db/client.py`) — opening a short-lived connection per call rather than a persistent pool (matches POC scope; pooling is part of the deferred productionization work above).

## Search implementation

Replaces the internals of `graphify_search()` in `assistant/graphify.py`; the function signature and output contract stay identical (`list[{source, title, content, similarity}]`, sorted, capped at `limit`, silent-degrade-to-`[]` on any failure) so `assistant/kb.py`'s `kb_search` caller needs no changes.

Two lookup paths feeding the same result list, replacing the old HTTP API's `direct_answers` + `semantic_matches` split:

- **Exact/structured match** — if the question contains something that looks like a prs code, product id, or function id, a plain SQL lookup against `functions` / `trigger_entries` / `products` / `uw_rules` (`WHERE code = %s` style, no embedding) returns high-confidence hits first. Cheap, no API call.
- **Semantic fallback** — embed the question with this project's existing 768-dim embedder, fetch candidate rows from `arc_config_kb.kb_embeddings` (1536-dim), truncate each fetched vector to its first 768 values and L2-renormalize, then cosine-compare against the query embedding in Python. Because `text-embedding-3-small` is Matryoshka-trained, a truncated-and-renormalized prefix of the full 1536-dim vector is (per OpenAI's own published benchmarks) a valid embedding in its own right, with only a small, well-characterized quality cost versus full 1536-dim comparison — see Embedding compatibility below.
  - The SQL-side candidate pool (full-text-narrowed before the Python cosine re-rank) is ordered by a weighted `ts_rank`, not a plain one: the `attributes=` segment of a function's `content` (its `result_attributes` — what the function *produces*) is weighted at tsvector rank 'A', the rest of `content` (which includes the raw condition/formula text, where an attribute may merely be *read*) at 'D'. Found live, post-launch: for "how is applicantAge generated in product 614004", plain `ts_rank` ranked the actual producing function (`F0100`) 1840th of 2218 full-text matches, because a downstream *consumer* function (`F1044`, which reads `applicantAge` three times in a threshold condition) scored higher purely on mention count. The weighted rank fixes this — `F0100` ranks 1st once matches inside `attributes=` are prioritized.

## Embedding compatibility

Decision: keep this project's `EMBED_DIM=768` unchanged; truncate Graphify's 1536-dim vectors down to 768 at query time instead of expanding this project's embeddings up to 1536.

- **Why not raise this project to 1536:** would require a schema migration (`vector(768)` → `vector(1536)` on the local KB tables) and re-embedding the local KB — avoidable cost.
- **Why not shrink Graphify's stored embeddings to 768:** `arc_config_kb.kb_embeddings` is shared infrastructure other Graphify consumers depend on; altering it is out of scope and not this project's data to unilaterally change.
- **Why truncate-at-query-time is strictly better than either:** truncation-and-renormalization of an already-computed 1536-dim vector is mathematically equivalent (for this Matryoshka-trained model) to having requested the shorter dimension from the API directly — same quality characteristics, zero migration, zero re-embedding, zero new API calls.
- **Embedding-backend coupling:** the semantic-match path only runs when `config.MODEL_BACKEND == "cloud"` (the only backend where the query embedder is `text-embedding-3-small`, matching Graphify's stored model family). Under `MODEL_BACKEND == "local"` (e.g., Ollama), the semantic path is skipped and search falls back to exact-match-only results, since comparing vectors from different model families produces meaningless similarity scores instead of real semantic ranking.
- **Revisit trigger:** this is provisional. If real usage feedback shows Graphify-sourced answers are noticeably worse than expected, reconsider (e.g. compare against full 1536-dim comparison to isolate whether truncation is actually the cause before changing anything).

## Testing

- `graphify_search()` unit-tested with the DB call mocked/monkeypatched — no live remote dependency in the normal suite, matching how every other LLM/DB call in this project is tested.
- One real integration test against the actual dev DB, gated behind an env var so it doesn't run by default (mirrors `tests/test_graph_live.py`'s existing pattern for live-LLM tests) — confirms the DSN resolution chain and a real query still work, runnable manually.
