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
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from assistant import config
from assistant.chat import answer_from_kb, classify_chat, extract_resolution, extract_teach_pair
from assistant.chat_history import get_chat, list_recent, save_chat
from assistant.db.client import init_schema
from assistant.ingest import ingest_text
from assistant.kb import kb_delete, kb_find, kb_learn, kb_learn_pending, kb_list_recent, kb_update
from assistant.redact import redact
from assistant.review import approve as review_approve, reject as review_reject, show as review_show
from assistant.tasks import get_escalation, list_open, resolve_escalation

_log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config.validate()
    init_schema()
    yield


app = FastAPI(title="YC PersonalAgent", lifespan=_lifespan)
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


class IngestRequest(BaseModel):
    text: str


class ChatCorrectRequest(BaseModel):
    mode: Literal["pick", "write"]
    source_index: int | None = None
    answer: str | None = None


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest, request: Request) -> dict:
    try:
        intent = classify_chat(req.text)
    except Exception:
        return {"action": "other", "message": "Sorry, I couldn't understand that — try rephrasing."}
    if intent.action == "ask":
        try:
            answer, hits = answer_from_kb(req.text)
        except Exception:
            return {"action": "other", "message": "Sorry, I couldn't understand that — try rephrasing."}
        redacted_question, _ = redact(req.text)
        created_by = "local" if _is_local_request(request) else "peer"
        try:
            chat_id = save_chat(
                question=redacted_question, answer=answer, sources=hits, created_by=created_by
            )
        except Exception as e:
            _log.warning("Failed to persist chat history: %s", e)
            chat_id = None
        return {"action": "ask", "answer": answer, "chat_id": chat_id, "sources": hits}
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


@app.get("/api/chat/history")
def chat_history_endpoint() -> dict:
    return {"entries": list_recent()}


@app.post("/api/chat/{chat_id}/correct")
def correct_chat(chat_id: int, req: ChatCorrectRequest, request: Request) -> dict:
    chat = get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"chat {chat_id} not found")
    if req.mode == "pick":
        if req.source_index is None or not (0 <= req.source_index < len(chat["sources"])):
            raise HTTPException(400, "source_index out of range")
        answer = chat["sources"][req.source_index]["content"]
    else:
        if not req.answer:
            raise HTTPException(400, "answer required for write mode")
        answer = req.answer
    if _is_local_request(request):
        new_id = kb_learn(
            question=chat["question"], answer=answer,
            created_by="chat-correction", source_refs=[f"chat:{chat_id}"],
        )
        return {"status": "learned", "id": new_id}
    new_id = kb_learn_pending(question=chat["question"], answer=answer)
    return {"status": "pending_approval", "id": new_id}


def _is_local_request(request: Request) -> bool:
    """True only if the request reached this server from localhost. Requires BOTH of two
    independent signals to agree — each defeats a different forgery angle, neither is
    sufficient alone (both gaps were found by background security review, 2026-07-22):

    1. request.client.host in (127.0.0.1, ::1) — the TCP-derived client address, NOT the
       Host header (Host is entirely client-controlled by an API client like curl; the
       first version of this function trusted it and was a real auth-bypass — a remote
       peer could just send `Host: 127.0.0.1`). uvicorn's ProxyHeadersMiddleware
       (forwarded_allow_ips="127.0.0.1") substitutes X-Forwarded-For into request.client
       for connections genuinely from 127.0.0.1 (what ngrok's local agent does), so this
       reflects the real remote client. Verified live: a forged X-Forwarded-For sent
       through the tunnel is overwritten by ngrok's edge with the real address first.

    2. Host header literally matches 127.0.0.1[:port] or localhost[:port] — defeats DNS
       rebinding, a DIFFERENT attack signal (1) alone can't catch: a malicious webpage
       (open in the user's own browser, unrelated to this app) whose hostname re-resolves
       to 127.0.0.1 causes the browser to make a genuinely socket-local request — signal
       (1) would say "local" since the TCP connection really is from 127.0.0.1 — but a
       real browser cannot forge its own Host header to match the request's true
       destination; it always sends the hostname it believes it navigated to (the
       attacker's rebound domain), not "127.0.0.1". An API client (curl) COULD fake this
       header, which is exactly why signal (1) exists too — neither check is sufficient
       alone, both must agree."""
    client = request.client.host if request.client else ""
    host = request.headers.get("host", "").split(":")[0]
    return client in ("127.0.0.1", "::1") and host in ("127.0.0.1", "localhost")


@app.post("/api/teach/confirm")
def teach_confirm(req: TeachConfirmRequest, request: Request) -> dict:
    if _is_local_request(request):
        new_id = kb_learn(
            question=req.question, answer=req.answer, created_by="web", source_refs=["web"]
        )
        return {"status": "learned", "id": new_id}
    # Peer-submitted (reached us through ngrok or similar): gate behind local-user
    # approval instead of writing to agent_knowledge directly.
    new_id = kb_learn_pending(question=req.question, answer=req.answer)
    return {"status": "pending_approval", "id": new_id}


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


def _shape_kb_entry_row(row: tuple) -> dict:
    return {"id": row[0], "question": row[1], "answer": row[2], "created_at": row[3].isoformat()}


@app.get("/api/tasks")
def list_tasks() -> dict:
    open_items = list_open()
    return {
        "escalations": [_shape_task_row(r) for r in open_items["escalations"]],
        "drafts": [_shape_task_row(r) for r in open_items["drafts"]],
        "pending_kb_entries": [
            _shape_kb_entry_row(r) for r in open_items["pending_kb_entries"]
        ],
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


@app.post("/api/ingest")
def ingest_endpoint(req: IngestRequest) -> dict:
    if not req.text.strip():
        raise HTTPException(400, "no text provided")
    return ingest_text(req.text)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
