"""The human approval gate. Usage:

    python -m assistant.review list
    python -m assistant.review show 3
    python -m assistant.review approve 3            # approve draft as-is
    python -m assistant.review approve 3 --edit     # opens $EDITOR to fix the draft first
    python -m assistant.review reject 3

Approving a reply: marks approved, copies final text to the clipboard for manual paste
into Teams/Outlook (phase-1 dispatch), and learns question -> final answer into the KB.
"""
import argparse
import json
import os
import subprocess
import tempfile

from assistant.db.client import get_connection
from assistant.kb import kb_learn


def _to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


def list_pending() -> list[tuple]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT id, kind, payload->>'sender', left(payload->>'question', 80), created_at"
            " FROM review_items WHERE status='pending' ORDER BY id"
        ).fetchall()


def show(item_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT kind, payload, status FROM review_items WHERE id=%s", (item_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"no review item {item_id}")
    return {"kind": row[0], "payload": row[1], "status": row[2]}


def approve(item_id: int, edited_text: str | None = None) -> dict:
    item = show(item_id)
    if item["status"] != "pending":
        raise SystemExit(f"item {item_id} is already {item['status']}")
    payload = item["payload"]
    kind = item["kind"]

    if kind == "kb_entry":
        # Peer-submitted KB entry (gated in app.py's /api/teach/confirm — anything not
        # coming from localhost lands here instead of writing to agent_knowledge
        # directly). Approving is what actually calls kb_learn.
        final_text = edited_text if edited_text is not None else payload["answer"]
        kb_learn(
            question=payload["question"],
            answer=final_text,
            created_by="peer-approved",
            source_refs=[f"review_item:{item_id}"],
        )
    else:
        final_text = edited_text if edited_text is not None else payload["draft"]
        if kind == "reply":
            kb_learn(
                question=payload["question"],
                answer=final_text,
                created_by="review-cli",
                source_refs=[f"review_item:{item_id}"],
            )

    with get_connection() as conn:
        conn.execute(
            "UPDATE review_items SET status='approved',"
            " resolution=%s, resolved_at=now() WHERE id=%s",
            (json.dumps({"final_text": final_text}), item_id),
        )
    if kind == "reply":
        _to_clipboard(final_text)
    return {"final_text": final_text}


def reject(item_id: int) -> bool:
    with get_connection() as conn:
        result = conn.execute(
            "UPDATE review_items SET status='rejected', resolved_at=now()"
            " WHERE id=%s AND status='pending'",
            (item_id,),
        )
        return result.rowcount > 0


def _edit_in_editor(initial: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(initial)
        path = f.name
    subprocess.run([editor, path], check=True)
    with open(path) as f:
        return f.read().strip()


def main() -> None:
    p = argparse.ArgumentParser(prog="assistant.review")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "approve", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
        if name == "approve":
            sp.add_argument("--edit", action="store_true")
    args = p.parse_args()

    if args.cmd == "list":
        for row in list_pending():
            print(f"#{row[0]:>4} [{row[1]}] from {row[2]}: {row[3]} ({row[4]:%m-%d %H:%M})")
    elif args.cmd == "show":
        item = show(args.id)
        print(f"kind={item['kind']} status={item['status']}")
        print(json.dumps(item["payload"], indent=2))
    elif args.cmd == "approve":
        item = show(args.id)
        text_key = "answer" if item["kind"] == "kb_entry" else "draft"
        edited = _edit_in_editor(item["payload"][text_key]) if args.edit else None
        result = approve(args.id, edited)
        if item["kind"] == "reply":
            print("approved — final text copied to clipboard:\n")
        else:
            print("approved:\n")
        print(result["final_text"])
    elif args.cmd == "reject":
        if reject(args.id):
            print("rejected")
        else:
            print("nothing to reject (item not pending)")


if __name__ == "__main__":
    main()
