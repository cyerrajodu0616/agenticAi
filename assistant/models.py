"""Single switch point between cloud (initial setup) and local (4060/Ollama) backends."""
from langchain.chat_models import init_chat_model

from assistant import config

_ROLES_CLOUD = {
    "classify": "groq:qwen/qwen3.6-27b",
    "compose": "google_genai:gemini-2.5-flash",
    "coder": "google_genai:gemini-2.5-flash",
}
_ROLES_LOCAL = {
    "classify": "ollama:qwen3:8b",
    "compose": "ollama:qwen3:8b",
    "coder": "ollama:qwen2.5-coder:7b",
}


def get_model(role: str):
    table = _ROLES_CLOUD if config.MODEL_BACKEND == "cloud" else _ROLES_LOCAL
    if role not in table:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(table)}")
    if config.MODEL_BACKEND == "local":
        return init_chat_model(table[role], base_url=config.OLLAMA_BASE_URL, temperature=0)
    return init_chat_model(table[role], temperature=0)


class _CloudEmbeddings:
    """gemini-embedding-001 truncated to EMBED_DIM (this account lacks text-embedding-004)."""

    def __init__(self):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        self._inner = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")

    def embed_query(self, text: str) -> list[float]:
        return self._inner.embed_query(text, output_dimensionality=config.EMBED_DIM)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed_documents(texts, output_dimensionality=config.EMBED_DIM)


def get_embeddings():
    if config.MODEL_BACKEND == "cloud":
        return _CloudEmbeddings()
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model="nomic-embed-text", base_url=config.OLLAMA_BASE_URL)


def embedding_model_name() -> str:
    """The actual embedding model identifier to record on agent_knowledge.embedding_model.

    config.MODEL_BACKEND ("cloud"/"local") is a routing switch, not a model name — this
    maps it to the real model so rows stay unambiguous across model changes (Plan 4's
    reembed needs to know which rows were embedded with what).
    """
    if config.MODEL_BACKEND == "cloud":
        return f"gemini-embedding-001@{config.EMBED_DIM}"
    return "nomic-embed-text"
