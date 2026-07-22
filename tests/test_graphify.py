"""Unit tests -- the arc_config_kb DB connection is faked, no real network/Azure."""


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
        [],  # trigger_entries lookup for product-context enrichment: no products found
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


def test_function_hit_gets_product_context_appended(monkeypatch):
    """A function hit's content gets "[Triggered for products: ...]" appended, sourced
    from trigger_entries -- kb_embeddings.content alone never says which product(s)
    invoke a function, so without this a question like "does F0100 run for 614004"
    can't be answered from the snippet text alone."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0, 0.0])
    conn = _FakeConn([
        [("function", "F0100", None, "Calculates the applicant's nearest age.")],
        [],
        [("F0100", ["614004", "811401"])],  # trigger_entries: two products trigger it
    ])
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
    conn = _FakeConn([
        [],
        [
            ("function", "C1094", "resolveConsentPdf",
             "resolves the consent PDF S3 key", _FakeVector([1.0, 0.0, 0.0])),
            ("service", "eapp_url", None,
             "eapp base URL", _FakeVector([0.0, 1.0, 0.0])),
        ],
        [],  # trigger_entries lookup for product-context enrichment: no products found
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
    conn = _FakeConn([exact_rows, [], []])  # 3rd: trigger_entries enrichment, no products
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


def test_local_backend_skips_semantic_degrading_to_exact_match_only(monkeypatch):
    """Under MODEL_BACKEND='local', semantic path is skipped (different embedding model
    family); search degrades to exact-match-only. Exact matches still work normally.
    """
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "local")
    # _embed would never be called for semantic path when MODEL_BACKEND != "cloud"
    embed_call_count = []
    monkeypatch.setattr(
        graphify, "_embed", lambda text: embed_call_count.append(1) or [1.0, 0.0]
    )

    # Setup: exact match returns one result, semantic would return something else
    conn = _FakeConn([
        [("function", "614004", "rateCalc", "Calculates the rate for product 614004.")],
        # Semantic path would fetch candidates, but it should not be called
        [],  # trigger_entries lookup for product-context enrichment: no products found
    ])
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)

    hits = graphify.graphify_search("how is the rate for product 614004 calculated")

    # Should have exact match
    assert len(hits) == 1
    assert hits[0]["title"] == "function:rateCalc"

    # _embed should NOT have been called at all (semantic path was skipped)
    assert embed_call_count == []
    assert conn.closed


def test_cloud_backend_runs_semantic_path(monkeypatch):
    """Under MODEL_BACKEND='cloud', semantic path should run normally."""
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify.config, "MODEL_BACKEND", "cloud")
    monkeypatch.setattr(graphify.config, "EMBED_DIM", 2)
    monkeypatch.setattr(graphify, "_embed", lambda text: [1.0, 0.0])

    conn = _FakeConn([
        [],  # No exact matches
        [
            ("function", "C1094", "resolveConsentPdf",
             "resolves the consent PDF S3 key", _FakeVector([1.0, 0.0, 0.0])),
        ],
        [],  # trigger_entries lookup for product-context enrichment: no products found
    ])
    monkeypatch.setattr(graphify.arc_config_db, "get_connection", lambda: conn)

    hits = graphify.graphify_search("where is the eConsent PDF?")

    # Should have semantic match (cloud backend)
    assert len(hits) >= 1
    assert hits[0]["title"] == "function:resolveConsentPdf"
    assert conn.closed
