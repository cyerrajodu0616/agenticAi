"""Local web UI: a thin JSON API over the existing assistant/ package.

Every route calls an existing kb/tasks/chat/review function and shapes JSON —
no new business logic lives here. Binds 127.0.0.1 only (see __main__.py); no
auth is added because of that binding. Writes are two-step: a proposal
endpoint (read-only) followed by a confirm endpoint that takes the exact
data the browser displayed, never just an id to "re-derive" from.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from assistant import config
from assistant.chat import answer_from_kb, classify_chat, extract_resolution, extract_teach_pair
from assistant.db.client import init_schema
from assistant.kb import kb_delete, kb_find, kb_learn, kb_list_recent, kb_update
from assistant.redact import redact
from assistant.tasks import get_escalation


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config.validate()
    init_schema()
    yield


app = FastAPI(title="assistant web UI", lifespan=_lifespan)
_STATIC_DIR = Path(__file__).parent / "static"


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": str(exc)})


class ChatRequest(BaseModel):
    text: str


class TeachConfirmRequest(BaseModel):
    question: str
    answer: str


class KbUpdateRequest(BaseModel):
    question: str | None = None
    answer: str | None = None


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest) -> dict:
    intent = classify_chat(req.text)
    if intent.action == "ask":
        return {"action": "ask", "answer": answer_from_kb(req.text)}
    if intent.action == "teach":
        pair = extract_teach_pair(req.text)
        return {"action": "teach", "question": pair.question, "answer": pair.answer}
    if intent.action in ("edit_kb", "delete_kb"):
        redacted, _ = redact(req.text)
        matches = kb_find(redacted)
        return {"action": intent.action, "matches": matches}
    if intent.action == "resolve":
        if intent.ref_id is None:
            return {"action": "resolve", "error": "no escalation id given"}
        esc = get_escalation(intent.ref_id)
        if esc is None or esc["status"] != "pending":
            return {"action": "resolve", "error": f"escalation {intent.ref_id} not found or not pending"}
        resolution = extract_resolution(req.text, esc["question_text"])
        return {
            "action": "resolve",
            "escalation_id": esc["id"],
            "question": esc["question_text"],
            "resolution": resolution,
        }
    if intent.action == "tasks":
        return {"action": "tasks", "message": "see the Tasks tab"}
    return {"action": "other", "message": intent.reasoning}


@app.post("/api/teach/confirm")
def teach_confirm(req: TeachConfirmRequest) -> dict:
    new_id = kb_learn(
        question=req.question, answer=req.answer, created_by="web", source_refs=["web"]
    )
    return {"id": new_id}


@app.get("/api/kb")
def list_kb(q: str | None = None) -> dict:
    entries = kb_find(q) if q else kb_list_recent()
    return {"entries": entries}


@app.patch("/api/kb/{entry_id}")
def update_kb(entry_id: int, req: KbUpdateRequest) -> dict:
    ok = kb_update(entry_id, question=req.question, answer=req.answer)
    if not ok:
        raise HTTPException(404, f"KB entry {entry_id} not found")
    return {"ok": True}


@app.delete("/api/kb/{entry_id}")
def delete_kb(entry_id: int) -> dict:
    ok = kb_delete(entry_id)
    if not ok:
        raise HTTPException(404, f"KB entry {entry_id} not found")
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
