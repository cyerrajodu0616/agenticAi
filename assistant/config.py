"""Central config. Import-time reads env; call validate() before running the app."""
import os

from dotenv import load_dotenv

if not os.getenv("PYTEST_VERSION"):
    load_dotenv()

MODEL_BACKEND = os.getenv("MODEL_BACKEND", "cloud")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://assistant:assistant@localhost:5433/assistant"
)
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.55"))

_REQUIRED_CLOUD_KEYS = ("GROQ_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")


def validate() -> None:
    if MODEL_BACKEND not in ("cloud", "local"):
        raise RuntimeError(
            f"MODEL_BACKEND must be 'cloud' or 'local', got {MODEL_BACKEND!r}"
        )
    if MODEL_BACKEND == "cloud":
        missing = [k for k in _REQUIRED_CLOUD_KEYS if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"MODEL_BACKEND=cloud but missing env: {', '.join(missing)}")
