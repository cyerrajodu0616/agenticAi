"""Optional knowledge source: the arcCenter Config Resolution Engine ("Graphify").

Queries the locally-run Graphify API (POST /query/ask) for arcCenter platform
questions (e.g. "where's the eConsent PDF for arcId X") — it already has curated
helpdesk answers and a semantic index over the whole platform that this project
shouldn't duplicate. This is an ADDITIVE source: if the service isn't running
(the common case — it requires a separate Azure-authenticated process), every
function here degrades to an empty result rather than raising, so kb_search
keeps working off agent_knowledge/product_knowledge alone.

Start it: bash /Users/ycaffdevice/dev/Graphify-ArcCode/run_local_poc.sh
(needs `az login` and a personal OPENAI_API_KEY in that shell — see the script).
"""
import json
import urllib.error
import urllib.request

from assistant import config

_DIRECT_ANSWER_SIMILARITY = 0.85  # fixed confidence for curated helpdesk-reference matches


def _post_json(path: str, payload: dict) -> dict | None:
    try:
        req = urllib.request.Request(
            f"{config.GRAPHIFY_BASE_URL}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.GRAPHIFY_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def graphify_search(question: str, limit: int = 3) -> list[dict]:
    """Same shape as assistant.kb.kb_search: [{source, title, content, similarity}]."""
    if not config.GRAPHIFY_ENABLED:
        return []
    data = _post_json("/query/ask", {"question": question})
    if data is None:
        return []

    hits = [
        {
            "source": "graphify",
            "title": a.get("category") or "helpdesk reference",
            "content": a["answer"],
            "similarity": _DIRECT_ANSWER_SIMILARITY,
        }
        for a in data.get("direct_answers", [])
    ]
    hits += [
        {
            "source": "graphify",
            "title": f"{m['type']}:{m['label']}",
            "content": m["snippet"],
            "similarity": float(m["score"]),
        }
        for m in data.get("semantic_matches", [])
    ]
    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[:limit]
