"""Unit tests -- the arc_config_kb DB connection is faked, no real network/Azure."""


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Returns a canned response keyed by a distinguishing substring found in the SQL
    text, not call order -- graphify_search issues a variable number of queries
    depending on what the question actually contains (exact / product-scoped /
    semantic / enrichment), so a fixed positional queue is too fragile. Pass responses
    by name, e.g. _FakeConn(exact=[...], semantic=[...]); an unspecified or unmatched
    query returns an empty result by default."""

    _MARKERS = {
        "exact": "entity_id = ANY",
        "product_detect": "FROM arc_config_kb.products",
        "product_scoped": "te.product_id = ANY",
        "semantic": "SELECT entity_type, entity_id, label, content, embedding",
        "enrichment": "array_agg(DISTINCT product_id",
    }

    def __init__(self, **responses):
        self._responses = responses
        self.closed = False
        self.queries = []  # raw SQL text of every query, for assertions on what ran

    def execute(self, sql, params=None):
        self.queries.append(sql)
        for name, marker in self._MARKERS.items():
            if marker in sql:
                return _FakeCursor(self._responses.get(name, []))
        return _FakeCursor([])

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
    conn = _FakeConn(
        exact=[("function", "614004", "rateCalc", "Calculates the rate for product 614004.")],
    )
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


def test_function_hit_gets_product_context_appended(monkeypatch):
    """A function hit's content gets "[Triggered for products: ...]" appended, sourced
    from trigger_entries -- kb_embeddings.content alone never says which product(s)
    invoke a function, so without this a question like "does F0100 run for 614004"
    can't be answered from the snippet text alone."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0, 0.0])
    conn = _FakeConn(
        exact=[("function", "F0100", None, "Calculates the applicant's nearest age.")],
        enrichment=[("F0100", ["614004", "811401"])],
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    hits = graphify.graphify_search("how is applicantAge generated")
    assert hits[0]["content"] == (
        "Calculates the applicant's nearest age. [Triggered for products: 614004, 811401]"
    )
    assert "_entity_type" not in hits[0] and "_entity_id" not in hits[0]


def test_semantic_match_truncates_and_ranks_by_similarity(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify.config, "EMBED_DIM", 2)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])
    conn = _FakeConn(
        semantic=[
            ("function", "C1094", "resolveConsentPdf",
             "resolves the consent PDF S3 key", _FakeVector([1.0, 0.0, 0.0])),
            ("service", "eapp_url", None,
             "eapp base URL", _FakeVector([0.0, 1.0, 0.0])),
        ],
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    hits = graphify.graphify_search("where is the eConsent PDF?", limit=2)
    assert hits[0]["title"] == "function:resolveConsentPdf"
    assert hits[0]["similarity"] > hits[1]["similarity"]


def test_respects_limit(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0])
    exact_rows = [("function", str(i), f"fn{i}", f"content {i}") for i in range(5)]
    conn = _FakeConn(exact=exact_rows)
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


def test_local_backend_skips_product_scoped_and_semantic(monkeypatch):
    """Under MODEL_BACKEND='local', both the product-scoped and semantic paths are
    skipped entirely (different embedding model family) -- search degrades to
    exact-match-only. Exact matches still work normally, and neither skipped path
    should issue so much as a product-lookup query, let alone an embedding call."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "local")
    embed_call_count = []
    monkeypatch.setattr(
        graphify, "_embed", lambda text: embed_call_count.append(1) or [1.0, 0.0]
    )

    conn = _FakeConn(
        exact=[("function", "614004", "rateCalc", "Calculates the rate for product 614004.")],
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)

    hits = graphify.graphify_search("how is the rate for product 614004 calculated")

    assert len(hits) == 1
    assert hits[0]["title"] == "function:rateCalc"
    assert embed_call_count == []
    assert not any("FROM arc_config_kb.products" in q for q in conn.queries), (
        "product-scoped path must short-circuit on MODEL_BACKEND before even "
        "checking whether the question names a real product"
    )
    assert conn.closed


def test_cloud_backend_runs_semantic_path(monkeypatch):
    """Under MODEL_BACKEND='cloud', semantic path should run normally."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify.config, "EMBED_DIM", 2)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])

    conn = _FakeConn(
        semantic=[
            ("function", "C1094", "resolveConsentPdf",
             "resolves the consent PDF S3 key", _FakeVector([1.0, 0.0, 0.0])),
        ],
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)

    hits = graphify.graphify_search("where is the eConsent PDF?")

    assert len(hits) >= 1
    assert hits[0]["title"] == "function:resolveConsentPdf"
    assert conn.closed


def test_product_scoped_match_surfaces_function_not_reachable_otherwise(monkeypatch):
    """Regression pin: a function trigger-linked to a named product, whose content
    never mentions that product id, is only reachable via the product-scoped path --
    not exact match (no literal id match) and not semantic (nothing in its content
    connects it to "314005"). Live-verified: without this path, "the pdf location for
    product 314005" retrieved zero of the 16 real PDF functions linked to it."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify.config, "EMBED_DIM", 2)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])
    conn = _FakeConn(
        product_detect=[("314005",)],
        product_scoped=[
            ("function", "C4401e", None,
             "Generates a PDF document using pdf-html-utility.", _FakeVector([1.0, 0.0, 0.0])),
        ],
        enrichment=[("C4401e", ["314005"])],
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    hits = graphify.graphify_search("the pdf location for product 314005")
    assert hits[0]["title"] == "function:C4401e"
    assert hits[0]["content"] == (
        "Generates a PDF document using pdf-html-utility. [Triggered for products: 314005]"
    )


def test_dedup_keeps_highest_score_across_paths(monkeypatch):
    """The same entity can legitimately surface from more than one lookup path (e.g.
    product-scoped AND semantic both find it, as F0100 did live for the applicantAge
    question) -- must appear once in the final result, keeping its highest score."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify.config, "EMBED_DIM", 2)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])
    conn = _FakeConn(
        product_detect=[("614004",)],
        product_scoped=[
            ("function", "F0100", None, "Calculates nearest age.", _FakeVector([1.0, 0.0, 0.0])),
        ],
        semantic=[
            ("function", "F0100", None, "Calculates nearest age.", _FakeVector([0.5, 0.5, 0.0])),
        ],
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    hits = graphify.graphify_search("how is applicantAge generated in 614004", limit=5)
    matches = [h for h in hits if h["title"] == "function:F0100"]
    assert len(matches) == 1, f"expected F0100 exactly once, got {len(matches)}: {hits}"
    assert matches[0]["similarity"] > 0.9  # the boosted product-scoped score, not semantic's


def test_product_scoped_skipped_when_token_not_a_real_product_id(monkeypatch):
    """A numeric-looking token that ISN'T actually a product_id (e.g. an arcId or any
    other number mentioned in the question) must not trigger the product-scoped JOIN
    query -- only tokens verified against arc_config_kb.products do."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])
    conn = _FakeConn(
        product_detect=[],  # DB confirms "123456" is not a real product_id
    )
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)
    graphify.graphify_search("status for arcId 123456")
    assert not any("te.product_id = ANY" in q for q in conn.queries), (
        "an unverified numeric token must not reach the product-scoped JOIN query"
    )
