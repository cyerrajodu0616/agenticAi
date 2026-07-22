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
    # Normalize query vector to unit L2 norm for true cosine similarity
    query_vec = _truncate_and_normalize(query_vec, len(query_vec))
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
        try:
            conn.close()
        except Exception as e:
            _log.debug("Failed to close Graphify connection: %s", e)
    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[:limit]
