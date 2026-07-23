"""Optional knowledge source: the arcCenter Config Resolution Engine ("Graphify").

Live, read-only SQL query against arc_config_kb on the arcCenter dev Postgres server
(see assistant/arc_config_db.py for connection/credential resolution) -- no separate
service to run. This is an ADDITIVE source: any connectivity or query failure degrades
every function here to an empty result rather than raising, so kb_search keeps working
off agent_knowledge/product_knowledge alone. Disabled by default (GRAPHIFY_ENABLED=false)
since it touches a remote corporate DB and needs `az login`.

Three lookup paths, merged and sorted by similarity:
  - exact match: question tokens that literally are a known entity_id (function_id,
    product_id, prs_code, rule_id, ...) in arc_config_kb.kb_embeddings
  - product-scoped match: when the question names a real product_id, functions actually
    trigger-linked to that product (via trigger_entries), re-ranked by relevance to the
    rest of the question. Needed because a function's kb_embeddings.content usually
    never mentions which product(s) invoke it -- that link only lives in
    trigger_entries, so plain full-text search structurally can't connect "product X"
    to "the functions that run for product X". Live-verified: asking "the pdf location
    for product 314005" retrieved zero of the 16 real PDF-generating functions
    trigger-linked to 314005 without this path, because none of their content mentions
    "314005" and unrelated rows that happened to mention the id outranked them. Product-
    scoped scores also get a flat confidence boost (_PRODUCT_SCOPED_BOOST) -- being
    verifiably linked to the product is stronger evidence than raw cosine similarity
    alone reflects; see _product_scoped_matches' docstring.
  - semantic match: full-text-narrowed candidates from kb_embeddings, re-ranked by
    cosine similarity against this project's own 768-dim query embedding. Graphify's
    embeddings are 1536-dim (full text-embedding-3-small); each candidate's vector is
    truncated to its first 768 values and L2-renormalized before comparing -- valid for
    this model family (Matryoshka-trained). See docs/superpowers/specs/
    2026-07-22-graphify-live-integration-design.md for why this doesn't need a schema
    migration on either side.

    Both the semantic and product-scoped candidate pools are ranked with the
    "attributes=" segment of a function's content weighted far above the rest (Postgres
    tsvector weight 'A' vs 'D') -- without this, a function that merely *reads* an
    attribute in its condition text (e.g. "if applicantAge > 60") outranks the function
    that actually *produces* it (whose content only mentions the attribute once, in
    "attributes=applicantAge"), because plain ts_rank rewards raw mention count.
    Live-verified against a real question ("how is applicantAge generated in 614004"):
    without this weighting the producing function (F0100) ranked 1840th of 2218
    matches; with it, 1st.

The same entity can now legitimately surface from more than one path (e.g. a function
that's both product-scoped-linked and a strong unscoped semantic match) -- deduped by
(entity_type, entity_id) before the final sort+limit, keeping the highest score.

Function-type hits get a "[Triggered for products: ...]" suffix appended to their content,
sourced from arc_config_kb.trigger_entries -- a function's kb_embeddings.content never
says which product(s) actually invoke it, so without this a question like "does F0100
run for product 614004" can't be confirmed from the retrieved snippet text alone. Done
once, after hits are narrowed to the final `limit` (not per-candidate).

IMPORTANT: The product-scoped and semantic match paths only run when
config.MODEL_BACKEND == "cloud" (i.e., when the query embedder is text-embedding-3-small)
-- both re-rank their candidates by cosine similarity against Graphify's stored
embeddings, which requires the same model family. Under MODEL_BACKEND == "local" (e.g.,
ollama nomic-embed-text), both are skipped entirely and search falls back to
exact-match-only results, since comparing vectors from different model families would
produce meaningless similarity scores. See "Embedding compatibility" in the design spec
for details.
"""
import logging
import math
import re

from assistant import arc_config_db, config
from assistant.models import get_embeddings

_log = logging.getLogger(__name__)

_EXACT_MATCH_SIMILARITY = 0.95  # fixed confidence: a literal entity_id match
_CODE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{3,20}\b")
_PRODUCT_ID_TOKEN_RE = re.compile(r"\b\d{3,8}\b")  # numeric, product_id-shaped tokens
_SEMANTIC_CANDIDATE_LIMIT = 50  # full-text-narrowed pool re-ranked by truncated cosine
_PRODUCT_SCOPED_BOOST = 0.15  # see _product_scoped_matches docstring


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
            "_entity_type": r[0],
            "_entity_id": r[1],
        }
        for r in rows
    ]


def _build_or_tsquery(question: str) -> str:
    """Extract significant words (len >= 2) and join with OR operator.

    Returns the OR-joined query string, e.g. "how | is | the | rate | for | product | 614004 | calculated".
    Postgres's to_tsquery('english', ...) will apply stemming and drop stopwords like "how"/"is"/"the".
    Returns empty string if no words are extracted (caller handles the short-circuit).
    """
    words = re.findall(r"[A-Za-z0-9]+", question.lower())
    significant_words = [w for w in words if len(w) >= 2]
    return " | ".join(significant_words)


def _score_candidates(query_vec: list[float], candidates) -> list[dict]:
    """Shared by _semantic_matches and _product_scoped_matches: truncate+normalize each
    candidate's stored embedding, cosine-score against the (already normalized)
    query_vec, and return hits sorted best-first."""
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
                "_entity_type": entity_type,
                "_entity_id": entity_id,
            }
        )
    scored.sort(key=lambda h: h["similarity"], reverse=True)
    return scored


def _semantic_matches(conn, question: str, limit: int) -> list[dict]:
    # Semantic matching only works when the query embedder is from the same model family
    # as Graphify's stored embeddings (text-embedding-3-small). Local backends like
    # Ollama use different models; comparing across model families produces noise, not
    # meaningful similarity. Skip semantic path entirely under local backends.
    if config.MODEL_BACKEND != "cloud":
        return []

    or_query = _build_or_tsquery(question)
    if not or_query:
        return []
    # Normalize query vector to unit L2 norm for true cosine similarity
    query_vec = _truncate_and_normalize(_embed(question), config.EMBED_DIM)
    candidates = conn.execute(
        r"""
        SELECT entity_type, entity_id, label, content, embedding
        FROM arc_config_kb.kb_embeddings
        WHERE embedding IS NOT NULL
          AND to_tsvector('english', content) @@ to_tsquery('english', %(q)s)
        ORDER BY ts_rank(
            setweight(to_tsvector('english',
                COALESCE(substring(content from 'attributes=([^.]*)\.'), '')), 'A')
            || setweight(to_tsvector('english', content), 'D'),
            to_tsquery('english', %(q)s)
        ) DESC
        LIMIT %(cand_limit)s
        """,
        {"q": or_query, "cand_limit": _SEMANTIC_CANDIDATE_LIMIT},
    ).fetchall()
    return _score_candidates(query_vec, candidates)[:limit]


def _detect_product_ids(conn, question: str) -> list[str]:
    """Numeric-looking tokens in the question that are ACTUALLY real product_ids (not
    just any number) -- verified against arc_config_kb.products, so an arcId, a dollar
    amount, or a date fragment in the question doesn't get treated as a product."""
    candidates = list(dict.fromkeys(_PRODUCT_ID_TOKEN_RE.findall(question)))
    if not candidates:
        return []
    rows = conn.execute(
        "SELECT product_id FROM arc_config_kb.products WHERE product_id = ANY(%(c)s)",
        {"c": candidates},
    ).fetchall()
    return [r[0] for r in rows]


def _product_scoped_matches(conn, question: str, limit: int) -> list[dict]:
    """Functions actually trigger-linked to a product named in the question, ranked by
    relevance to the rest of the question. See the module docstring for why this exists
    -- a function's content usually never says which product invokes it.

    Scores get a flat _PRODUCT_SCOPED_BOOST: being structurally, verifiably linked to
    the named product (proven via trigger_entries, not just guessed from text) is
    stronger evidence than an unscoped semantic match, but raw cosine similarity alone
    doesn't reflect that -- a function's terse description ("Generates PDF documents by
    calling...") often scores lower against a natural-language question than a merely
    topically-adjacent row that happens to spell out the product id as text. Live-
    verified: without the boost, the actual PDF-generating functions linked to product
    314005 (~0.42-0.43 similarity) still lost to api_contract rows that only coincidentally
    contain "314005" as a substring (~0.48-0.50)."""
    if config.MODEL_BACKEND != "cloud":
        return []

    product_ids = _detect_product_ids(conn, question)
    if not product_ids:
        return []
    # The JOIN below already scopes results to these products structurally; including
    # the id token(s) as text-search terms too would just reintroduce noise from
    # unrelated rows that happen to mention the same number elsewhere.
    remaining_question = _PRODUCT_ID_TOKEN_RE.sub(" ", question)
    or_query = _build_or_tsquery(remaining_question)
    if not or_query:
        return []
    query_vec = _truncate_and_normalize(_embed(question), config.EMBED_DIM)
    candidates = conn.execute(
        r"""
        SELECT ke.entity_type, ke.entity_id, ke.label, ke.content, ke.embedding
        FROM arc_config_kb.kb_embeddings ke
        WHERE ke.entity_type = 'function'
          AND EXISTS (
              SELECT 1 FROM arc_config_kb.trigger_entries te
              WHERE te.function_id = ke.entity_id AND te.product_id = ANY(%(product_ids)s)
          )
          AND ke.embedding IS NOT NULL
          AND to_tsvector('english', ke.content) @@ to_tsquery('english', %(q)s)
        ORDER BY ts_rank(
            setweight(to_tsvector('english',
                COALESCE(substring(ke.content from 'attributes=([^.]*)\.'), '')), 'A')
            || setweight(to_tsvector('english', ke.content), 'D'),
            to_tsquery('english', %(q)s)
        ) DESC
        LIMIT %(cand_limit)s
        """,
        {"product_ids": product_ids, "q": or_query, "cand_limit": _SEMANTIC_CANDIDATE_LIMIT},
    ).fetchall()
    scored = _score_candidates(query_vec, candidates)[:limit]
    for h in scored:
        h["similarity"] = min(h["similarity"] + _PRODUCT_SCOPED_BOOST, 1.0)
    return scored


def _attach_product_context(conn, hits: list[dict]) -> None:
    """Append which products actually trigger a function hit's content, e.g.
    "[Triggered for products: 614004]" -- kb_embeddings.content for a function never
    says which product(s) invoke it (that link lives in trigger_entries), so without
    this a question like "how is X generated for product 614004" retrieves the right
    function but the answer can't confirm the product link and has to hedge. Runs once,
    after hits are already narrowed to the final `limit`, not on every candidate."""
    function_ids = [h["_entity_id"] for h in hits if h.get("_entity_type") == "function"]
    if not function_ids:
        return
    rows = conn.execute(
        """
        SELECT function_id, array_agg(DISTINCT product_id ORDER BY product_id) AS product_ids
        FROM arc_config_kb.trigger_entries
        WHERE function_id = ANY(%(function_ids)s)
        GROUP BY function_id
        """,
        {"function_ids": function_ids},
    ).fetchall()
    products_by_function = {r[0]: r[1] for r in rows}
    for h in hits:
        products = products_by_function.get(h.get("_entity_id"))
        if products:
            h["content"] = f"{h['content']} [Triggered for products: {', '.join(products)}]"


def _dedup_by_entity(hits: list[dict]) -> dict:
    """The same entity can legitimately surface from more than one lookup path now
    (e.g. a function that's both product-scoped-linked AND a strong unscoped semantic
    match) -- keep only its highest-scoring occurrence, keyed by (entity_type,
    entity_id). Entities without both keys (shouldn't happen, but never trust a dict
    blindly) are kept as-is, keyed by their own object id so they can't collide."""
    best = {}
    for h in hits:
        entity_type, entity_id = h.get("_entity_type"), h.get("_entity_id")
        key = (entity_type, entity_id) if entity_type and entity_id else id(h)
        if key not in best or h["similarity"] > best[key]["similarity"]:
            best[key] = h
    return best


def graphify_search(question: str, limit: int = 3) -> list[dict]:
    """Same shape as assistant.kb.kb_search: [{source, title, content, similarity}]."""
    if not config.GRAPHIFY_ENABLED:
        return []
    conn = arc_config_db.get_connection()
    if conn is None:
        return []
    try:
        hits = (
            _exact_matches(conn, question, limit)
            + _product_scoped_matches(conn, question, limit)
            + _semantic_matches(conn, question, limit)
        )
        hits = list(_dedup_by_entity(hits).values())
        hits.sort(key=lambda h: h["similarity"], reverse=True)
        hits = hits[:limit]
        _attach_product_context(conn, hits)
    except Exception as e:
        _log.debug("Graphify query failed: %s", e)
        return []
    finally:
        try:
            conn.close()
        except Exception as e:
            _log.debug("Failed to close Graphify connection: %s", e)
    for h in hits:
        h.pop("_entity_type", None)
        h.pop("_entity_id", None)
    return hits
