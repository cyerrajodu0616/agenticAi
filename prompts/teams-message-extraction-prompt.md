# Claude Code Prompt: Teams Message Extraction for FAQ Mining (Section 8a, chat half)

Paste this into Claude Code inside the `agenticAi` repo, after the MVP scaffold
(`langgraph-personal-assistant-mvp-prompt.md`) has been applied — this adds a new
`assistant/ingestion/` subpackage and depends on `assistant/redact.py` and
`assistant/config.py` existing already.

---

## Context

`docs/personal-assistant-plan.md` Section 8a defines the FAQ-mining pipeline:
`pull_transcripts -> redact_pii -> filter -> embed_questions -> cluster -> canonicalize
-> pending_review -> upsert into agent_knowledge`. This prompt builds the Teams half of
`pull_transcripts` only — it produces redacted candidate Q&A pairs as JSONL, one file per
run. It does **not** do embedding, clustering, canonicalization, or writing to
`agent_knowledge` — those stay separate stages per the plan, so a bad extraction run
never touches the real knowledge table directly.

Two Teams surfaces behave very differently and must be built as two separate modules:

- **1:1 and group chats** — no admin consent needed. A user can register an Entra ID app
  and consent to `Chat.Read` (delegated) themselves via a device-code sign-in. This is
  fully buildable now.
- **Channel messages** — `ChannelMessage.Read.All` (delegated or application, tenant-wide)
  needs a tenant admin to grant consent. There is an admin-free alternative — Teams
  **resource-specific consent (RSC)** with `ChannelMessage.Read.Group` — but that requires
  building and installing an actual Teams app (app manifest, sideloaded or catalog-listed,
  consented to by the *team owner*, not just an Entra app registration) — a materially
  bigger task than a Graph API script. Confirm which route you want before the channel
  module is implemented for real; it's built as a stub below.

Do not invent tenant ID, client ID, or client secret values — none exist in `.env` yet.
Do not assume RSC's exact admin-approval boundary beyond what's stubbed here — Microsoft's
own docs are inconsistent on whether `ChannelMessage.Read.Group` is delegated,
application-only, or both; confirm in the Entra admin center / Teams Developer Portal
before relying on it.

**Housekeeping — flag to the user, don't just fix silently:** `.gitignore` currently does
not exclude `.env`, and `.env` already holds live API keys (OpenAI, Google, Claude, Groq,
xAI, NVIDIA). Before adding Teams client credentials to the same file, add a `.env` line to
`.gitignore` and confirm `.env` was never previously committed (`git log --all --
.env` — if it has been, the keys need rotating, not just gitignoring going forward).

## Task 1 — Dependencies

Add to `pyproject.toml` `dependencies` and `requirements.txt`:
- `msal>=1.31` (Microsoft's auth library, handles the device-code flow and token cache)
- `requests>=2.32`

## Task 2 — Config additions in `assistant/config.py`

Add and validate (raise clear `RuntimeError` naming the missing var, same pattern as
existing `ENV`/`CLAUDE_API_KEY` validation):
- `TEAMS_CLIENT_ID` — Application (client) ID from the Entra app registration (Task 5 below).
- `TEAMS_TENANT_ID` — defaults to `"common"` (multi-tenant / personal+work accounts) if unset;
  use the real tenant ID instead if the user's org restricts this.
- `TEAMS_TOKEN_CACHE_PATH` — defaults to `.teams_token_cache.bin` in repo root; add this
  filename to `.gitignore` too (it will hold a cached refresh token).

## Task 3 — `assistant/ingestion/__init__.py` and `assistant/ingestion/teams_auth.py`

Real, working delegated auth using MSAL's public client device-code flow (no client
secret needed — this is what makes self-consent possible without an admin):

```python
import msal, os

SCOPES = ["Chat.Read", "User.Read"]

def get_token_interactive(cache_path: str, client_id: str, tenant_id: str) -> str:
    """
    Acquires a Graph token via device-code flow, caching to cache_path so the user
    only has to sign in once (subsequent runs use the cached refresh token silently).
    Prints the device-code URL/code to the terminal on first run.
    """
    cache = msal.SerializableTokenCache()
    if os.path.exists(cache_path):
        cache.deserialize(open(cache_path, "r").read())

    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", token_cache=cache
    )
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to create device flow: {flow}")
        print(flow["message"])  # instructs user to visit a URL and enter a code
        result = app.acquire_token_by_device_flow(flow)

    if cache.has_state_changed:
        open(cache_path, "w").write(cache.serialize())

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")
    return result["access_token"]
```

## Task 4 — `assistant/ingestion/teams_chats.py`

Real, working extraction for 1:1/group chats:

```python
import requests
from datetime import datetime, timedelta

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

def list_chats(token: str) -> list[dict]:
    """GET /me/chats, paginating via @odata.nextLink. Returns raw chat objects
    (id, topic, chatType: 'oneOnOne'|'group')."""
    ...

def list_chat_messages(token: str, chat_id: str) -> list[dict]:
    """GET /me/chats/{chat_id}/messages, paginating via @odata.nextLink.
    Returns messages sorted oldest-first (Graph returns newest-first by default —
    reverse before pairing). Each item: id, from.user.displayName, createdDateTime,
    body.content (HTML — strip tags), messageType (only keep 'message', skip
    system/event messages)."""
    ...

QUESTION_MARKERS = ("?",)  # simple heuristic; extend if needed after reviewing real data

def pair_questions_and_answers(messages: list[dict], max_gap: timedelta = timedelta(hours=24)) -> list[dict]:
    """
    Heuristic pairing (chats have no reply-threading, unlike channels):
    a message is a candidate 'question' if its text ends with '?' or contains a
    question marker; its 'answer' is the next message in the same chat from a
    DIFFERENT sender within max_gap. Skips a question if no qualifying reply
    follows before the next question or the gap gets too large.
    This is a first-pass heuristic — flag pairs where the 'answer' looks like it
    might just be another question, or where multiple people replied, for human
    review rather than silently picking one.
    Returns: [{"question": str, "answer": str, "asked_by": str, "answered_by": str,
               "chat_id": str, "question_msg_id": str, "answer_msg_id": str,
               "asked_at": iso str, "needs_review": bool, "review_reason": str|None}]
    """
    ...

def extract_all_chats(token: str) -> list[dict]:
    """Orchestrates list_chats -> list_chat_messages (per chat) -> pair_questions_and_answers,
    flattens to one list of candidate pairs across all chats."""
    ...
```

Strip HTML tags from `body.content` (Graph returns chat message bodies as HTML) using
`html.parser` or a minimal regex strip — do not pull in a new heavy dependency (e.g.
BeautifulSoup) for this alone unless one is already in the repo.

## Task 5 — `assistant/ingestion/teams_channels.py` — STUB, do not implement for real yet

```python
def extract_all_channels(token: str) -> list[dict]:
    """
    STUB — blocked on a consent-model decision, not a coding gap.

    Two ways to get channel access, pick one before implementing:
    1. Ask a tenant admin to grant ChannelMessage.Read.All (delegated or application)
       consent for the app registered in Task 6. Simplest code path (same
       list-then-paginate shape as teams_chats.py, plus GET /teams/{id}/channels
       and GET /teams/{id}/channels/{id}/messages), but you said you don't have
       tenant admin access.
    2. Build a real Teams app (manifest.json, sideloaded or catalog-listed) using
       resource-specific consent (ChannelMessage.Read.Group), which a TEAM OWNER
       (not tenant admin) can grant per-team. Avoids the admin bottleneck but is a
       bigger build — a Teams app package, not just a Graph API call — and
       Microsoft's own docs disagree on whether this permission is delegated,
       application-only, or both, so confirm the exact flow in the Teams Developer
       Portal before committing to this path.

    Channel messages DO support real reply-threading (replyToId on each message),
    so once access exists, pairing is more reliable than the chat heuristic —
    each reply already names its parent message.
    """
    raise NotImplementedError(
        "Channel access path not yet chosen — see docstring. Needs either tenant-admin "
        "consent for ChannelMessage.Read.All, or a Teams app built for resource-specific "
        "consent (ChannelMessage.Read.Group), granted by a team owner."
    )
```

## Task 6 — Entra ID app registration (manual step, not code — do this before running Task 3/4)

Tell the user to do this (do not attempt it programmatically):
1. https://entra.microsoft.com > **App registrations** > **New registration**.
2. Name it (e.g. `personal-assistant-teams-ingest`), leave redirect URI blank (device-code
   flow doesn't need one), register as a **public client**.
3. **Authentication** tab > enable "Allow public client flows" = Yes.
4. **API permissions** > Add a permission > Microsoft Graph > Delegated permissions >
   add `Chat.Read` and `User.Read`. Since these are user-consentable (no admin needed for
   `Chat.Read` in most tenants), the "Grant admin consent" button is not required — consent
   happens automatically on first device-code sign-in.
5. Copy the **Application (client) ID** and the **Directory (tenant) ID** from the
   Overview page into `.env` as `TEAMS_CLIENT_ID` and `TEAMS_TENANT_ID`.

## Task 7 — `assistant/ingestion/run_extract_teams.py`

CLI entrypoint:
- Calls `get_token_interactive(...)`, then `extract_all_chats(token)`.
- Runs each candidate pair's `question` and `answer` through the existing `redact()`
  from `assistant/redact.py` before writing anything to disk — raw PII must never reach
  the output file.
- Writes to `data/teams_candidate_qa_<run-timestamp>.jsonl`, one JSON object per line,
  matching the `pair_questions_and_answers` return shape plus the redaction's PII-mapping
  key count (not the mapping itself — that stays in memory only, per `redact.py`'s existing
  contract) so a reviewer can see how much was redacted without seeing what.
- Add `data/*.jsonl` to `.gitignore` — this file will contain real (redacted) colleague
  questions, not synthetic data.
- Prints a summary: chats scanned, messages scanned, candidate pairs found, pairs flagged
  `needs_review=True`.

## Explicitly out of scope / blocked on external info

- `teams_channels.py` real implementation — blocked on the consent-model decision in Task 5.
- Embedding, clustering, canonicalization, and the `pending_review` → `agent_knowledge`
  upsert — these are the next stage of Section 8a's pipeline, a separate prompt, once this
  extraction step has run at least once and the output shape has been eyeballed against
  real data.
- Tuning the `QUESTION_MARKERS` heuristic — validate against a real (redacted) sample
  before trusting it; questions that don't end in `?` (e.g. "wondering where the PDF is
  for X") will currently be missed.
