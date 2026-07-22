"""End-to-end against the real arcCenter dev Postgres (arc_config_kb). Requires
`az login` (or ARC_CONFIG_KB_DSN set) and network access to the arcCenter dev VNet.
Skipped by default -- pytest.ini's addopts excludes `integration`-marked tests; run
explicitly with: pytest -m integration tests/test_graphify_live.py
"""
from dotenv import load_dotenv
import pytest

load_dotenv()

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
