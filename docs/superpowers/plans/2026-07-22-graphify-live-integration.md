# Graphify Live Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `assistant/graphify.py`'s currently-nonfunctional HTTP call (to a Graphify service that's never actually running) with a live, read-only SQL query against arcCenter's already-populated dev Postgres schema (`arc_config_kb`), so questions about arcCenter platform config get real answers.

**Architecture:** A new `assistant/arc_config_db.py` resolves a DSN to the remote DB (env var override, else Azure Key Vault via the `az` CLI) and opens a connection, never raising — any failure returns `None`. `assistant/graphify.py` keeps its exact existing `graphify_search(question, limit) -> [{source, title, content, similarity}]` contract but replaces its internals with two SQL lookups against `arc_config_kb.kb_embeddings`: an exact-match path (question tokens that are literally a known `entity_id`) and a semantic path (full-text-narrowed candidates re-ranked by cosine similarity, with each candidate's 1536-dim embedding truncated to this project's 768-dim and L2-renormalized before comparing).

**Tech Stack:** Python, `psycopg` (v3, already a dependency), `pgvector.psycopg`, stdlib `subprocess`/`re`/`math`. No new dependencies.

## Global Constraints

- This project's own embedding dimension (`EMBED_DIM`) stays at 768 — no schema migration, no re-embedding the local KB. (Spec: Embedding compatibility)
- No new Azure SDK dependency (`azure-identity`/`azure-keyvault-secrets`) — credential resolution shells out to the `az` CLI, same as already verified working on this machine. (Spec: Connectivity & credentials)
- Live query only — no local copy/sync of `arc_config_kb`. (Spec: Non-goals)
- `graphify_search()`'s function signature and output shape must not change — `assistant/kb.py`'s call site is untouched by this plan. (Spec: Search implementation)
- Any DSN-resolution, connection, or query failure must degrade to `[]` / `None`, never raise — this stays an optional, additive source. (Spec: Goal, Connectivity & credentials)
- `GRAPHIFY_ENABLED` stays an explicit opt-in (default `false`) — this now touches a real remote corporate DB.
- Unit tests must not touch the real remote DB (mock the connection); exactly one integration test file, marked `pytest.mark.integration` (already excluded from the default run by `pytest.ini`'s `addopts = -m "not integration"`), exercises the real path.

---

### Task 1: Config + `assistant/arc_config_db.py` (DSN resolution & connection)

**Files:**
- Modify: `assistant/config.py:17-21`
- Create: `assistant/arc_config_db.py`
- Test: `tests/test_arc_config_db.py`

**Interfaces:**
- Consumes: `assistant.config.ARC_CONFIG_KB_DSN`, `assistant.config.ARC_CONFIG_KB_ENV`, `assistant.config.GRAPHIFY_TIMEOUT` (all new/modified in this task).
- Produces: `arc_config_db.resolve_dsn() -> str | None`, `arc_config_db.get_connection() -> psycopg.Connection | None` — both used by Task 2.

- [ ] **Step 1: Update `assistant/config.py`**

Replace:
```python
# Optional external knowledge source: the arcCenter Config Resolution Engine (Graphify).
# Start it separately (needs Azure CLI login): bash <Graphify repo>/run_local_poc.sh
GRAPHIFY_BASE_URL = os.getenv("GRAPHIFY_BASE_URL", "http://localhost:8080")
GRAPHIFY_ENABLED = os.getenv("GRAPHIFY_ENABLED", "false").lower() == "true"
GRAPHIFY_TIMEOUT = float(os.getenv("GRAPHIFY_TIMEOUT", "3"))
```
with:
```python
# Optional external knowledge source: the arcCenter Config Resolution Engine
# ("Graphify") -- live read-only SQL query against its arc_config_kb schema on
# the arcCenter dev Postgres server. See assistant/arc_config_db.py for the
# credential-resolution chain.
GRAPHIFY_ENABLED = os.getenv("GRAPHIFY_ENABLED", "false").lower() == "true"
GRAPHIFY_TIMEOUT = float(os.getenv("GRAPHIFY_TIMEOUT", "5"))
ARC_CONFIG_KB_DSN = os.getenv("ARC_CONFIG_KB_DSN", "")
ARC_CONFIG_KB_ENV = os.getenv("ARC_CONFIG_KB_ENV", "dev")
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_arc_config_db.py`:
```python
"""Unit tests -- the `az` CLI is faked via subprocess.run, no real network/Azure."""
import subprocess

import pytest


def test_env_var_override_skips_az_cli(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(
        arc_config_db.config, "ARC_CONFIG_KB_DSN",
        "host=x port=5432 dbname=y user=z password=w",
    )
    called = []
    monkeypatch.setattr(arc_config_db.subprocess, "run", lambda *a, **kw: called.append(1))
    assert arc_config_db.resolve_dsn() == "host=x port=5432 dbname=y user=z password=w"
    assert called == []


def test_resolves_via_az_keyvault(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_DSN", "")
    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_ENV", "dev")

    values = {
        "pg-host": "pg.afficiency-dev.az.intra.afficiency.com",
        "pg-port": "5432",
        "application-db": "arcdb",
        "application-user": "arc_app",
        "application-pwd": "s3cret",
    }

    def fake_run(cmd, **kwargs):
        key = cmd[cmd.index("-n") + 1]
        return subprocess.CompletedProcess(cmd, 0, stdout=values[key] + "\n", stderr="")

    monkeypatch.setattr(arc_config_db.subprocess, "run", fake_run)
    dsn = arc_config_db.resolve_dsn()
    assert dsn == (
        "host=pg.afficiency-dev.az.intra.afficiency.com port=5432 "
        "dbname=arcdb user=arc_app password=s3cret"
    )


def test_az_cli_failure_returns_none(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_DSN", "")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(arc_config_db.subprocess, "run", fake_run)
    assert arc_config_db.resolve_dsn() is None


def test_az_cli_missing_returns_none(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_DSN", "")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("az not found")

    monkeypatch.setattr(arc_config_db.subprocess, "run", fake_run)
    assert arc_config_db.resolve_dsn() is None


def test_get_connection_returns_none_when_dsn_unresolved(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db, "resolve_dsn", lambda: None)
    assert arc_config_db.get_connection() is None


def test_get_connection_returns_none_on_connect_failure(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db, "resolve_dsn", lambda: "host=unreachable")

    def fake_connect(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(arc_config_db.psycopg, "connect", fake_connect)
    assert arc_config_db.get_connection() is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_arc_config_db.py -v`
Expected: FAIL / ERROR on collection — `ModuleNotFoundError: No module named 'assistant.arc_config_db'`

- [ ] **Step 4: Write `assistant/arc_config_db.py`**

```python
"""Read-only connection to arcCenter's Config Resolution Engine ("Graphify") Postgres
schema (arc_config_kb) on the arcCenter dev server. Credential priority, mirroring
Graphify-ArcCode's own resolve_dsn() convention:

  1. ARC_CONFIG_KB_DSN env var (explicit override; also what tests use, so the normal
     suite never touches the real remote DB)
  2. Azure Key Vault afficiency-{ARC_CONFIG_KB_ENV}-kv via the `az` CLI (needs `az login`)

Never raises: this is an optional, additive knowledge source (see assistant/graphify.py)
-- any resolution or connection failure returns None so callers degrade to "no data"
rather than crashing the assistant.
"""
import logging
import subprocess

import psycopg
from pgvector.psycopg import register_vector

from assistant import config

_log = logging.getLogger(__name__)

_PG_KEYS = ["pg-host", "pg-port", "application-db", "application-user", "application-pwd"]


def resolve_dsn() -> str | None:
    if config.ARC_CONFIG_KB_DSN:
        return config.ARC_CONFIG_KB_DSN
    vault = f"afficiency-{config.ARC_CONFIG_KB_ENV}-kv"
    secrets = {}
    for key in _PG_KEYS:
        try:
            result = subprocess.run(
                ["az", "keyvault", "secret", "show", "--vault-name", vault,
                 "-n", key, "--query", "value", "-o", "tsv"],
                capture_output=True, text=True, check=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            _log.debug("arc_config_kb DSN resolution failed fetching %s: %s", key, e)
            return None
        secrets[key] = result.stdout.strip()
    return (
        f"host={secrets['pg-host']} port={secrets['pg-port']} "
        f"dbname={secrets['application-db']} user={secrets['application-user']} "
        f"password={secrets['application-pwd']}"
    )


def get_connection() -> psycopg.Connection | None:
    dsn = resolve_dsn()
    if dsn is None:
        return None
    try:
        conn = psycopg.connect(dsn, connect_timeout=config.GRAPHIFY_TIMEOUT, autocommit=True)
        register_vector(conn)
    except Exception as e:
        _log.debug("arc_config_kb connection failed: %s", e)
        return None
    return conn
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_arc_config_db.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add assistant/config.py assistant/arc_config_db.py tests/test_arc_config_db.py
git commit -m "feat: add arc_config_db DSN resolution + connection helper"
```

---

### Task 2: Rewrite `assistant/graphify.py` for live SQL search

**Files:**
- Modify: `assistant/graphify.py` (full rewrite of internals)
- Modify: `tests/test_graphify.py` (full rewrite — old tests mock the now-removed HTTP layer)

**Interfaces:**
- Consumes: `arc_config_db.get_connection() -> psycopg.Connection | None` (Task 1), `assistant.models.get_embeddings()` (existing), `assistant.config.GRAPHIFY_ENABLED` / `EMBED_DIM` (existing).
- Produces: `graphify.graphify_search(question: str, limit: int = 3) -> list[dict]` — same shape as before (`{source, title, content, similarity}`), consumed unchanged by `assistant/kb.py:38`.

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/test_graphify.py`:
```python
"""Unit tests -- the arc_config_kb DB connection is faked, no real network/Azure."""
import pytest


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Each entry in `responses` is returned in order, one per conn.execute() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False

    def execute(self, sql, params=None):
        return _FakeCursor(self._responses.pop(0))

    def close(self):
        self.closed = True


class _FakeVector:
    def __init__(self, values):
        self._values = values

    def to_list(self):
        return self._values


def test_disabled_returns_empty_without_connecting(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", False)
    called = []
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: called.append(1))
    assert graphify.graphify_search("anything") == []
    assert called == []


def test_connection_unavailable_returns_empty(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: None)
    assert graphify.graphify_search("where is the eConsent PDF?") == []


def test_exact_match_hit(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0, 0.0])
    conn = _FakeConn([
        [("function", "614004", "rateCalc", "Calculates the rate for product 614004.")],
        [],
    ])
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    hits = graphify.graphify_search("how is the rate for product 614004 calculated")
    assert hits == [
        {
            "source": "graphify",
            "title": "function:rateCalc",
            "content": "Calculates the rate for product 614004.",
            "similarity": 0.95,
        }
    ]
    assert conn.closed


def test_semantic_match_truncates_and_ranks_by_similarity(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "EMBED_DIM", 2)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])
    conn = _FakeConn([
        [],
        [
            ("function", "C1094", "resolveConsentPdf",
             "resolves the consent PDF S3 key", _FakeVector([1.0, 0.0, 0.0])),
            ("service", "eapp_url", None,
             "eapp base URL", _FakeVector([0.0, 1.0, 0.0])),
        ],
    ])
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    hits = graphify.graphify_search("where is the eConsent PDF?", limit=2)
    assert hits[0]["title"] == "function:resolveConsentPdf"
    assert hits[0]["similarity"] > hits[1]["similarity"]


def test_respects_limit(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0])
    exact_rows = [("function", str(i), f"fn{i}", f"content {i}") for i in range(5)]
    conn = _FakeConn([exact_rows, []])
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    assert len(graphify.graphify_search("100 200 300 400 500", limit=2)) == 2


def test_query_failure_returns_empty(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)

    class _BoomConn:
        def execute(self, *a, **kw):
            raise RuntimeError("boom")

        def close(self):
            pass

    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: _BoomConn())
    assert graphify.graphify_search("anything") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_graphify.py -v`
Expected: FAIL — `AttributeError: module 'assistant.graphify' has no attribute 'arc_config_db'` (or similar; the old HTTP-based implementation is still in place)

- [ ] **Step 3: Replace the full contents of `assistant/graphify.py`**

```python
"""Optional knowledge source: the arcCenter Config Resolution Engine ("Graphify").

Live, read-only SQL query against arc_config_kb on the arcCenter dev Postgres server
(see assistant/arc_config_db.py for connection/credential resolution) -- no separate
service to run. This is an ADDITIVE source: any connectivity or query failure degrades
every function here to an empty result rather than raising, so kb_search keeps working
off agent_knowledge/product_knowledge alone. Disabled by default (GRAPHIFY_ENABLED=false)
since it touches a remote corporate DB and needs `az login`.

Two lookup paths, merged and sorted by similarity:
  - exact match: question tokens that literally are a known entity_id (function_id,
    product_id, prs_code, rule_id, ...) in arc_config_kb.kb_embeddings
  - semantic match: full-text-narrowed candidates from kb_embeddings, re-ranked by
    cosine similarity against this project's own 768-dim query embedding. Graphify's
    embeddings are 1536-dim (full text-embedding-3-small); each candidate's vector is
    truncated to its first 768 values and L2-renormalized before comparing -- valid for
    this model family (Matryoshka-trained). See docs/superpowers/specs/
    2026-07-22-graphify-live-integration-design.md for why this doesn't need a schema
    migration on either side.
"""
import logging
import math
import re

from assistant import arc_config_db, config
from assistant.models import get_embeddings

_log = logging.getLogger(__name__)

_EXACT_MATCH_SIMILARITY = 0.95  # fixed confidence: a literal entity_id match
_CODE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{3,20}\b")
_SEMANTIC_CANDIDATE_LIMIT = 50  # full-text-narrowed pool re-ranked by truncated cosine


def _embed(text: str) -> list[float]:
    return get_embeddings().embed_query(text)


def _truncate_and_normalize(values: list[float], dim: int) -> list[float]:
    truncated = values[:dim]
    norm = math.sqrt(sum(x * x for x in truncated))
    if norm == 0:
        return truncated
    return [x / norm for x in truncated]


def _exact_matches(conn, question: str, limit: int) -> list[dict]:
    tokens = list(dict.fromkeys(_CODE_TOKEN_RE.findall(question)))
    if not tokens:
        return []
    rows = conn.execute(
        """
        SELECT entity_type, entity_id, label, content
        FROM arc_config_kb.kb_embeddings
        WHERE entity_id = ANY(%(tokens)s)
        LIMIT %(limit)s
        """,
        {"tokens": tokens, "limit": limit},
    ).fetchall()
    return [
        {
            "source": "graphify",
            "title": f"{r[0]}:{r[2] or r[1]}",
            "content": r[3],
            "similarity": _EXACT_MATCH_SIMILARITY,
        }
        for r in rows
    ]


def _semantic_matches(conn, question: str, limit: int) -> list[dict]:
    query_vec = _embed(question)
    candidates = conn.execute(
        """
        SELECT entity_type, entity_id, label, content, embedding
        FROM arc_config_kb.kb_embeddings
        WHERE embedding IS NOT NULL
          AND to_tsvector('english', content) @@ plainto_tsquery('english', %(q)s)
        ORDER BY ts_rank(to_tsvector('english', content), plainto_tsquery('english', %(q)s)) DESC
        LIMIT %(cand_limit)s
        """,
        {"q": question, "cand_limit": _SEMANTIC_CANDIDATE_LIMIT},
    ).fetchall()
    scored = []
    for entity_type, entity_id, label, content, embedding in candidates:
        truncated = _truncate_and_normalize(embedding.to_list(), config.EMBED_DIM)
        similarity = sum(a * b for a, b in zip(query_vec, truncated))
        scored.append(
            {
                "source": "graphify",
                "title": f"{entity_type}:{label or entity_id}",
                "content": content,
                "similarity": similarity,
            }
        )
    scored.sort(key=lambda h: h["similarity"], reverse=True)
    return scored[:limit]


def graphify_search(question: str, limit: int = 3) -> list[dict]:
    """Same shape as assistant.kb.kb_search: [{source, title, content, similarity}]."""
    if not config.GRAPHIFY_ENABLED:
        return []
    conn = arc_config_db.get_connection()
    if conn is None:
        return []
    try:
        hits = _exact_matches(conn, question, limit) + _semantic_matches(conn, question, limit)
    except Exception as e:
        _log.debug("Graphify query failed: %s", e)
        return []
    finally:
        conn.close()
    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[:limit]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_graphify.py -v`
Expected: 6 passed

- [ ] **Step 5: Run the full unit suite to confirm no regression in `kb.py`'s caller**

Run: `python3 -m pytest tests/ -v` (integration-marked tests are excluded by default per `pytest.ini`)
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add assistant/graphify.py tests/test_graphify.py
git commit -m "feat: replace Graphify HTTP call with live arc_config_kb SQL search"
```

---

### Task 3: Live integration test against the real dev DB

**Files:**
- Create: `tests/test_graphify_live.py`

**Interfaces:**
- Consumes: `arc_config_db.resolve_dsn()` (Task 1), `graphify.graphify_search()` (Task 2) — exercised against the real remote DB, no mocking.
- Produces: nothing consumed elsewhere — this is a leaf verification test.

- [ ] **Step 1: Write the test**

Create `tests/test_graphify_live.py`:
```python
"""End-to-end against the real arcCenter dev Postgres (arc_config_kb). Requires
`az login` (or ARC_CONFIG_KB_DSN set) and network access to the arcCenter dev VNet.
Skipped by default -- pytest.ini's addopts excludes `integration`-marked tests; run
explicitly with: pytest -m integration tests/test_graphify_live.py
"""
import pytest

pytestmark = pytest.mark.integration


def test_dsn_resolves_via_real_az_cli():
    from assistant import arc_config_db

    dsn = arc_config_db.resolve_dsn()
    assert dsn is not None, "expected `az` CLI + afficiency-dev-kv to resolve a DSN"
    assert "host=" in dsn and "password=" in dsn


def test_known_product_id_returns_a_hit(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    hits = graphify.graphify_search("how is the rate for product 614004 calculated", limit=3)
    assert hits, "expected at least one real hit from arc_config_kb for product 614004"
    assert all(h["source"] == "graphify" for h in hits)
```

- [ ] **Step 2: Run it manually against the real DB**

Run: `python3 -m pytest tests/test_graphify_live.py -v -m integration`
Expected: 2 passed (requires `az login` to have been run on this machine — already confirmed working this session)

- [ ] **Step 3: Confirm it's excluded from the default run**

Run: `python3 -m pytest tests/ -v`
Expected: `test_graphify_live.py`'s tests do not appear in the run (excluded by `pytest.ini`'s `addopts = -m "not integration"`)

- [ ] **Step 4: Commit**

```bash
git add tests/test_graphify_live.py
git commit -m "test: add live integration test for Graphify arc_config_kb search"
```
