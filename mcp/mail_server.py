"""A local, offline mock mail MCP server.

Exposes four tools over HTTP (streamable-http transport) on port 5002:

    mail_list_messages(query)             -> summaries of inbox mail
    mail_read_message(message_id)         -> the full body of one message
    mail_create_draft(to, subject, body)  -> save a reply to the drafts folder
    mail_schedule_followup(message_id)    -> schedule a reminder, ASKING the
                                             human for the details (elicitation)

State is a small JSON file managed by mail_store.py. Started by `make mail`
before the API, so build_agent() can discover the tools at startup.

--------------------------------------------------------------------------
Two very different ways of asking a human, and why this file has both
--------------------------------------------------------------------------
`mail_create_draft` is gated by a **human-in-the-loop interrupt**, which is a
LangGraph concept living entirely on the *client* side: the agent decides to
call the tool, the HITL middleware pauses the graph BEFORE the call, and the
server never learns any of it happened.

`mail_schedule_followup` uses **MCP elicitation**, which is a protocol feature
living on the *server* side: the tool starts running, discovers it is missing
something only a human can supply, and sends an `elicitation/create` request
back down the same connection — mid-call. The tool function is literally
suspended at the `await ctx.elicit(...)` line until the client answers.

The practical difference that matters: the interrupt is *durable* (it is
written into the LangGraph checkpoint, so the process can restart while it is
pending), while the elicitation is *in-flight* (it only exists as long as this
tool call is open). See api/elicitation.py for the client half.
"""

from __future__ import annotations

from mail_store import load_store, next_id, save_store
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("mock-mail", host="127.0.0.1", port=5002)

@mcp.custom_route("/ping", methods=["GET"])
async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "pong"})


@mcp.tool()
def mail_list_messages(query: str = "") -> list[dict]:
    """List messages in the inbox.

    Returns a summary (id, sender, subject, date, snippet) for each message —
    not the full body. Use read_message to open one. The optional ``query`` is
    a case-insensitive substring matched against the subject and sender, mostly
    to mirror Gmail's search box; leave it empty to list everything.
    """
    store = load_store()
    q = query.strip().lower()
    out = []
    for m in store["inbox"]:
        haystack = f"{m.get('subject', '')} {m.get('from', '')}".lower()
        if q and q not in haystack:
            continue
        body = m.get("body", "")
        out.append(
            {
                "id": m.get("id"),
                "from": m.get("from"),
                "subject": m.get("subject"),
                "date": m.get("date"),
                "snippet": body[:140] + ("…" if len(body) > 140 else ""),
            }
        )
    return out


@mcp.tool()
def mail_read_message(message_id: str) -> dict:
    """Return the full message (sender, subject, date, complete body) by id."""
    store = load_store()
    for m in store["inbox"]:
        if m.get("id") == message_id:
            return m
    return {"error": f"No message with id {message_id!r}."}


@mcp.tool()
def mail_create_draft(to: str, subject: str, body: str) -> dict:
    """Save a reply to the drafts folder. Does NOT send.

    Mirrors a real Gmail "create draft" call: the message is staged for the
    human to review and send later. In this course a human-in-the-loop gate
    runs before this tool, so a draft is only written after explicit approval.
    """
    store = load_store()
    draft = {
        "id": next_id(store["drafts"], "draft"),
        "to": to,
        "subject": subject,
        "body": body,
    }
    store["drafts"].append(draft)
    save_store(store)
    return {"status": "draft_saved", "draft_id": draft["id"], "to": to, "subject": subject}


# The shape of the answer the server wants back from the human.
#
# This becomes the `requestedSchema` of the elicitation request, so the client
# can render a form from it. The MCP spec allows only FLAT objects of primitive
# fields here (string / number / integer / boolean / enum) — no nested objects,
# no arrays of objects. That restriction is deliberate: it keeps an elicitation
# renderable as a plain form by any client, including ones that are not an LLM.
#
# NOTE: deliberately no docstring. Pydantic copies a model's docstring into the
# JSON Schema `description`, which would then be sent over the wire on every
# elicitation and shown in the client's form. Explanations for humans reading
# the code belong in comments like this one; only text meant for the END USER
# belongs in `description=`.
class FollowUpDetails(BaseModel):
    days: int = Field(description="Em quantos dias fazer o follow-up (1 a 90)", ge=1, le=90)
    note: str = Field(default="", description="Nota curta sobre o que cobrar")


@mcp.tool()
async def mail_schedule_followup(message_id: str, ctx: Context) -> dict:
    """Schedule a follow-up reminder for an inbox message.

    The agent only supplies WHICH message. The scheduling details (how many
    days out, and the note) are asked of the human at call time through MCP
    elicitation — the tool suspends at `await ctx.elicit(...)` until the client
    answers, then finishes with the answer in hand.

    Use this when Jane asks to be reminded about a message later. Do not guess
    the number of days yourself — the whole point is that she picks it.
    """
    store = load_store()

    message = next((m for m in store["inbox"] if m.get("id") == message_id), None)
    if message is None:
        return {"error": f"No message with id {message_id!r}."}

    # ---- the elicitation itself -------------------------------------------
    # Everything before this line already ran. Everything after it waits for a
    # human. If the client cannot ask anyone (no UI attached), it answers
    # "decline" and we degrade gracefully instead of hanging or inventing a date.
    result = await ctx.elicit(
        message=(
            f"Agendar follow-up de “{message.get('subject', '(sem assunto)')}” "
            f"(de {message.get('from', 'desconhecido')}). Em quantos dias?"
        ),
        schema=FollowUpDetails,
    )

    # Three possible outcomes, and each one means something different:
    #   accept  -> the human filled the form; result.data is a FollowUpDetails
    #   decline -> the human said no (or nobody could be asked)
    #   cancel  -> the human dismissed it / it timed out without a choice
    # Returning a plain dict for the non-accept cases (instead of raising)
    # lets the AGENT read what happened and tell the user, which is what you
    # want: a declined elicitation is a normal outcome, not a tool failure.
    if result.action != "accept":
        return {
            "status": f"followup_{result.action}",
            "message_id": message_id,
            "detail": "Jane não informou os detalhes do follow-up.",
        }

    followups = store.setdefault("followups", [])
    followup = {
        "id": next_id(followups, "followup"),
        "message_id": message_id,
        "subject": message.get("subject"),
        "days": result.data.days,
        "note": result.data.note,
    }
    followups.append(followup)
    save_store(store)

    return {
        "status": "followup_scheduled",
        "followup_id": followup["id"],
        "message_id": message_id,
        "days": followup["days"],
        "note": followup["note"],
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
