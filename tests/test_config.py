import importlib
import pytest


def _reload_config(monkeypatch, **env):
    for k in ("MODEL_BACKEND", "EMBED_DIM", "GROQ_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import assistant.config as config
    return importlib.reload(config)


def test_defaults(monkeypatch):
    config = _reload_config(monkeypatch)
    assert config.MODEL_BACKEND == "cloud"
    assert config.EMBED_DIM == 768
    assert 0 < config.SIMILARITY_THRESHOLD < 1


def test_unknown_backend_rejected(monkeypatch):
    config = _reload_config(monkeypatch, MODEL_BACKEND="hybrid")
    with pytest.raises(RuntimeError, match="MODEL_BACKEND"):
        config.validate()


def test_cloud_requires_keys(monkeypatch):
    config = _reload_config(monkeypatch, MODEL_BACKEND="cloud")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        config.validate()


def test_local_needs_no_cloud_keys(monkeypatch):
    config = _reload_config(monkeypatch, MODEL_BACKEND="local")
    config.validate()  # must not raise
