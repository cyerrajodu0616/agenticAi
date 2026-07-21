"""Feed one question (or the fixture set) through the graph and print the trace.

    python -m assistant.run_local "Where is the eConsent PDF for ARCF25344h646?"
    python -m assistant.run_local --fixtures
"""
import argparse
import json
from pathlib import Path

from assistant import config
from assistant.db.client import init_schema
from assistant.graph import build_graph

FIXTURES = Path(__file__).parent.parent / "fixtures" / "questions.json"


def run_one(app, text: str) -> dict:
    out = app.invoke(
        {"raw_text": text, "source_channel": "cli", "sender": "me", "thread_id": None}
    )
    print(f"\nQ: {text}")
    print(f"  intent: {out.get('intent')}")
    if out.get("review_item_id"):
        print(f"  -> draft reply queued: review item #{out['review_item_id']}")
        print(f"     (run: python -m assistant.review show {out['review_item_id']})")
    if out.get("escalation_id"):
        print(f"  -> escalated to you: escalation #{out['escalation_id']}")
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="assistant.run_local")
    p.add_argument("question", nargs="?")
    p.add_argument("--fixtures", action="store_true")
    args = p.parse_args()

    config.validate()
    init_schema()
    app = build_graph()

    if args.fixtures:
        cases = json.loads(FIXTURES.read_text())
        results = [run_one(app, c["text"]) for c in cases]
        expected = [c["expected_intent"] for c in cases]
        actual = [r.get("intent") for r in results]
        matches = sum(e == a for e, a in zip(expected, actual))
        print(f"\nrouting: {matches}/{len(cases)} matched expected intent")
    elif args.question:
        run_one(app, args.question)
    else:
        p.error("give a question or --fixtures")


if __name__ == "__main__":
    main()
