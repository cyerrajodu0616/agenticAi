"""Unit tests — the Graphify HTTP layer is faked via _post_json, no real network."""
import pytest


def test_disabled_returns_empty_without_network(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", False)
    called = []
    monkeypatch.setattr(graphify, "_post_json", lambda *a, **kw: called.append(1))
    assert graphify.graphify_search("anything") == []
    assert called == []


def test_service_unreachable_returns_empty(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(graphify, "_post_json", lambda *a, **kw: None)
    assert graphify.graphify_search("where is the eConsent PDF?") == []


def test_maps_direct_answers_and_semantic_matches(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(
        graphify,
        "_post_json",
        lambda *a, **kw: {
            "direct_answers": [
                {"answer": "Check consentDetails for the arcId.", "source": "runbook",
                 "category": "eConsent"}
            ],
            "semantic_matches": [
                {"type": "function", "id": "FN-42", "label": "resolveConsentPdf",
                 "score": 0.71, "snippet": "resolves the consent PDF S3 key"}
            ],
        },
    )
    hits = graphify.graphify_search("where is the eConsent PDF?")
    assert {"source": "graphify", "title": "eConsent",
            "content": "Check consentDetails for the arcId.",
            "similarity": 0.85} in hits
    assert {"source": "graphify", "title": "function:resolveConsentPdf",
            "content": "resolves the consent PDF S3 key", "similarity": 0.71} in hits
    assert hits[0]["similarity"] >= hits[1]["similarity"]  # best-first


def test_respects_limit(monkeypatch):
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    monkeypatch.setattr(
        graphify,
        "_post_json",
        lambda *a, **kw: {
            "direct_answers": [
                {"answer": f"a{i}", "category": "c"} for i in range(5)
            ],
            "semantic_matches": [],
        },
    )
    assert len(graphify.graphify_search("q", limit=2)) == 2
