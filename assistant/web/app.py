"""Local web UI: a thin JSON API over the existing assistant/ package.

Every route calls an existing kb/tasks/chat/review function and shapes JSON —
no new business logic lives here. Binds 127.0.0.1 only (see __main__.py); no
auth is added because of that binding. Writes are two-step: a proposal
endpoint (read-only) followed by a confirm endpoint that takes the exact
data the browser displayed, never just an id to "re-derive" from.
"""
import logging
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
from assistant.review import approve as review_approve, reject as review_reject, show as review_show
from assistant.tasks import get_escalation, list_open, resolve_escalation

_log = logging.getLogger(__name__)


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
    _log.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"error": "internal server error"})


class ChatRequest(BaseModel):
    text: str


class TeachConfirmRequest(BaseModel):
    question: str
    answer: str


class KbUpdateRequest(BaseModel):
    question: str | None = None
    answer: str | None = None


class ApproveRequest(BaseModel):
    edited_text: str | None = None


class DraftResolutionRequest(BaseModel):
    text: str


class ResolveRequest(BaseModel):
    resolution_text: str


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest) -> dict:
    try:
        intent = classify_chat(req.text)
    except Exception:
        return {"action": "other", "message": "Sorry, I couldn't understand that — try rephrasing."}
    if intent.action == "ask":
        try:
            answer = answer_from_kb(req.text)
        except Exception:
            return {"action": "other", "message": "Sorry, I couldn't understand that — try rephrasing."}
        return {"action": "ask", "answer": answer}
    if intent.action == "teach":
        try:
            pair = extract_teach_pair(req.text)
        except Exception:
            return {"action": "other", "message": "Sorry, I couldn't understand that — try rephrasing."}
        return {"action": "teach", "question": pair.question, "answer": pair.answer}
    if intent.action in ("edit_kb", "delete_kb"):
        redacted, _ = redact(req.text)
        matches = kb_find(redacted)
        return {"action": intent.action, "matches": matches}
    if intent.action == "resolve":
        if not intent.ref_id:
            return {"action": "resolve", "error": "no escalation id given"}
        esc = get_escalation(intent.ref_id)
        if esc is None or esc["status"] != "pending":
            return {"action": "resolve", "error": f"escalation {intent.ref_id} not found or not pending"}
        try:
            resolution = extract_resolution(req.text, esc["question_text"])
        except Exception:
            return {"action": "other", "message": "Sorry, I couldn't understand that — try rephrasing."}
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
    entries = kb_find(redact(q)[0]) if q else kb_list_recent()
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


def _shape_task_row(row: tuple) -> dict:
    return {"id": row[0], "sender": row[1], "question": row[2], "created_at": row[3].isoformat()}


@app.get("/api/tasks")
def list_tasks() -> dict:
    open_items = list_open()
    return {
        "escalations": [_shape_task_row(r) for r in open_items["escalations"]],
        "drafts": [_shape_task_row(r) for r in open_items["drafts"]],
    }


@app.get("/api/review/{item_id}")
def get_review_item(item_id: int) -> dict:
    try:
        return review_show(item_id)
    except SystemExit:
        raise HTTPException(404, f"review item {item_id} not found")


@app.post("/api/review/{item_id}/approve")
def approve_review_item(item_id: int, req: ApproveRequest) -> dict:
    try:
        return review_approve(item_id, req.edited_text)
    except SystemExit as e:
        if str(e).startswith("no review item"):
            raise HTTPException(404, str(e))
        raise HTTPException(409, str(e))


@app.post("/api/review/{item_id}/reject")
def reject_review_item(item_id: int) -> dict:
    ok = review_reject(item_id)
    if not ok:
        raise HTTPException(409, f"review item {item_id} is not pending")
    return {"ok": True}


@app.post("/api/escalation/{esc_id}/draft-resolution")
def draft_resolution(esc_id: int, req: DraftResolutionRequest) -> dict:
    esc = get_escalation(esc_id)
    if esc is None or esc["status"] != "pending":
        raise HTTPException(404, f"escalation {esc_id} not found or not pending")
    return {"resolution": extract_resolution(req.text, esc["question_text"])}


@app.post("/api/escalation/{esc_id}/resolve")
def resolve_escalation_endpoint(esc_id: int, req: ResolveRequest) -> dict:
    ok = resolve_escalation(esc_id, req.resolution_text, resolved_by="web")
    if not ok:
        raise HTTPException(409, f"escalation {esc_id} not found or not pending")
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
