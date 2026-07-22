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
