"""New Prompt and Follow-up Prompt flows - spec Sections 3 and 7.

Conversation state: a user's next plain-text message is either a brand new
prompt or a follow-up to a specific project, decided by whether
context.user_data["awaiting_prompt_for"] is set (a project_id) or None.
Set by the home:new_prompt / project:followup callbacks below, consumed and
cleared by handle_text_message once the reply arrives.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from api_client.client import AgentAPIError
from bot.keyboards.inline import back_to_home
from bot.models.schemas import LogLevel
from bot.services import formatter
from bot.services.auth import require_authorized

logger = logging.getLogger(__name__)


@require_authorized
async def home_new_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """home:new_prompt - asks the user to type their task description next."""
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_prompt_for"] = None  # None means "new project", not a follow-up
    await query.edit_message_text(
        "📝 Send me a description of what you'd like built, e\\.g\\.:\n\n"
        "_Build a FastAPI authentication system using JWT\\._",
        parse_mode="MarkdownV2",
    )


@require_authorized
async def project_followup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """project:followup:{project_id} - asks for a follow-up message that
    continues that specific project's own conversation context."""
    query = update.callback_query
    await query.answer()
    project_id = query.data.split(":", 2)[2]
    context.user_data["awaiting_prompt_for"] = project_id
    await query.edit_message_text(
        f"💬 Send your follow\\-up for `{project_id}` \\(e\\.g\\. _Add Redis caching\\._\\)",
        parse_mode="MarkdownV2",
    )


@require_authorized
async def newprompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/newprompt - a direct command shortcut to the same flow as the
    home:new_prompt button, per the spec's "or use /newprompt directly"."""
    context.user_data["awaiting_prompt_for"] = None
    await update.effective_message.reply_text(
        "📝 Send me a description of what you'd like built."
    )


@require_authorized
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text messages (no leading /) are treated as a prompt - either
    for a brand new project, or a follow-up to a project the user was just
    asked about, per the awaiting_prompt_for state set above."""
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    project_id = context.user_data.get("awaiting_prompt_for")
    client = context.bot_data["api_client"]
    db = context.bot_data["db"]

    thinking = await update.effective_message.reply_text("⏳ Sending to agent…")

    try:
        if project_id:
            # Ensure the project actually exists in our LOCAL cache before
            # logging against it - logs.project_id has a foreign key to
            # projects.id, and there's no guarantee this project was ever
            # cached locally (e.g. a follow-up on a project from a previous
            # bot session). Refreshing here also keeps the cache honest
            # about the project's current state before we act on it.
            current = await client.get_project(project_id)
            await db.upsert_project(current)
            resp = await client.send_followup_prompt(project_id, text)
            await db.add_log(project_id, LogLevel.INFO, f"Follow-up: {text}")
        else:
            resp = await client.send_prompt(text)
            # Cache immediately, same reasoning as the follow-up path above -
            # keeps the local mirror honest from the moment a project exists,
            # rather than only catching up the next time something else
            # happens to fetch and upsert it.
            created = await client.get_project(resp.project_id)
            await db.upsert_project(created)
    except AgentAPIError as e:
        await thinking.edit_text(f"⚠️ Failed to reach the agent: {e}")
        return
    finally:
        context.user_data["awaiting_prompt_for"] = None

    text_out = formatter.prompt_response_text(
        resp.project_id, resp.project_name, resp.status.value, resp.estimated_steps,
    )
    await thinking.edit_text(text_out, parse_mode="MarkdownV2", reply_markup=back_to_home())

    # Start live-streaming this project's progress into THIS same message,
    # per the spec's "edit continuously" requirement (Section 6).
    listener = context.bot_data.get("ws_listener")
    if listener is not None:
        listener.watch(resp.project_id, chat_id=update.effective_chat.id, message_id=thinking.message_id)
