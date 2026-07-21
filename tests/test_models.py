import pytest


def test_unknown_role_raises(monkeypatch):
    monkeypatch.setattr("assistant.config.MODEL_BACKEND", "cloud")
    from assistant.models import get_model

    with pytest.raises(ValueError, match="unknown role"):
        get_model("translate")


@pytest.mark.parametrize(
    "backend,role,expected",
    [
        ("cloud", "classify", "groq:qwen/qwen3-32b"),
        ("cloud", "compose", "google_genai:gemini-2.5-flash"),
        ("cloud", "coder", "google_genai:gemini-2.5-flash"),
        ("local", "classify", "ollama:qwen3:8b"),
        ("local", "compose", "ollama:qwen3:8b"),
        ("local", "coder", "ollama:qwen2.5-coder:7b"),
    ],
)
def test_role_model_mapping(monkeypatch, backend, role, expected):
    # Don't build real clients in unit tests: capture what init_chat_model is asked for.
    calls = {}

    def fake_init(model, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("assistant.models.init_chat_model", fake_init)
    monkeypatch.setattr("assistant.config.MODEL_BACKEND", backend)
    from assistant.models import get_model

    get_model(role)
    assert calls["model"] == expected
    if backend == "local":
        assert calls["kwargs"]["base_url"]
