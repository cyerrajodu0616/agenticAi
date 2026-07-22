"""End-to-end against the real arcCenter dev Postgres (arc_config_kb). Requires
`az login` (or ARC_CONFIG_KB_DSN set) and network access to the arcCenter dev VNet.
Skipped by default -- pytest.ini's addopts excludes `integration`-marked tests; run
explicitly with: pytest -m integration tests/test_graphify_live.py
"""
from dotenv import load_dotenv
import pytest

pytestmark = pytest.mark.integration


def test_dsn_resolves_via_real_az_cli():
    load_dotenv()
    from assistant import arc_config_db

    dsn = arc_config_db.resolve_dsn()
    assert dsn is not None, "expected `az` CLI + afficiency-dev-kv to resolve a DSN"
    assert "host=" in dsn and "password=" in dsn


def test_known_product_id_returns_a_hit(monkeypatch):
    load_dotenv()
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    hits = graphify.graphify_search("how is the rate for product 614004 calculated", limit=3)
    assert hits, "expected at least one real hit from arc_config_kb for product 614004"
    assert all(h["source"] == "graphify" for h in hits)


def test_attribute_generation_question_ranks_producer_over_consumer(monkeypatch):
    """Regression pin: plain ts_rank ranked the functions that PRODUCE applicantAge
    (e.g. F0100, mentioning it once in "attributes=applicantAge") 1840th of 2218
    full-text matches for this exact question, because a downstream CONSUMER (F1044,
    which reads applicantAge three times in a threshold condition) scored higher on raw
    mention count. The weighted rank (attributes= segment at tsvector weight 'A') fixes
    this -- found and fixed live, 2026-07-22. Several near-duplicate age-calculator
    functions exist (F0100/F1000/F10000/F100002) with near-identical scores, so this
    checks the top hit is A producer (content declares it via "attributes="), not one
    specific function id, and that the consumer F1044 isn't first."""
    load_dotenv()
    import assistant.graphify as graphify

    monkeypatch.setattr(graphify.config, "GRAPHIFY_ENABLED", True)
    hits = graphify.graphify_search("how is the applicantAge generated in 614004", limit=3)
    assert hits, "expected at least one real hit from arc_config_kb"
    assert "attributes=applicantage" in hits[0]["content"].lower(), (
        f"expected a function that PRODUCES applicantAge to rank first, "
        f"got {hits[0]['title']!r}: {hits[0]['content'][:100]!r}"
    )
    assert "F1044" not in hits[0]["title"]
