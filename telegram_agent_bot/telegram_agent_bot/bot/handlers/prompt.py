"""New Prompt, Follow-up Prompt, and Voice Message flows - spec Sections 3
and 7, plus voice input.

Conversation state: a user's next plain-text message is either a brand new
prompt or a follow-up to a specific project, decided by whether
context.user_data["awaiting_prompt_for"] is set (a project_id) or None.
Set by the home:new_prompt / project:followup callbacks below, consumed and
cleared by _send_prompt_text once a prompt is actually sent.

Voice notes go through the same _send_prompt_text body after a
transcribe-then-confirm step: context.user_data["pending_voice_transcript"]
holds {"text": ..., "project_id": ...} between transcription finishing and
the user tapping one of the confirm/edit/discard buttons - never sent to
the agent automatically, since voice recognition on technical vocabulary
(library names, flags, "JWT", "Redis", camelCase identifiers) will misfire
sometimes, and this transcript becomes a prompt that writes real files.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional

from telegram import Message, Update
from telegram.ext import ContextTypes

from api_client.client import AgentAPIError
from bot.keyboards.inline import back_to_home, voice_confirm_keyboard
from bot.models.schemas import LogLevel
from bot.services import formatter
from bot.services.auth import require_authorized
from bot.services.formatter import esc

logger = logging.getLogger(__name__)


@require_authorized
async def home_new_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """home:new_prompt - asks the user to type their task description next."""
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_prompt_for"] = None  # None means "new project", not a follow-up
    await query.edit_message_text(
        "📝 Send me a description of what you'd like built, e\\.g\\.:\n\n"
        "_Build a FastAPI authentication system using JWT\\._\n\n"
        "You can also send a voice note instead of typing\\.",
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
        f"💬 Send your follow\\-up for `{project_id}` \\(e\\.g\\. _Add Redis caching\\._\\) "
        f"\\- typed or as a voice note\\.",
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


async def _send_prompt_text(text: str, project_id: Optional[str], thinking: Message,
                             chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared body: given resolved prompt text, an optional project_id
    (None = new project, else follow-up), and a Message we can keep editing
    in place - sends the prompt to the agent, caches the project locally,
    and starts the live-progress watcher on that same message.

    This is the ONE place "what happens once we have a prompt string" is
    implemented - both the typed-text flow (handle_text_message) and the
    voice-confirmation flow (voice_send_callback) call this rather than
    each keeping their own copy that could silently drift out of sync.
    """
    client = context.bot_data["api_client"]
    db = context.bot_data["db"]

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
        context.user_data.pop("pending_voice_transcript", None)

    text_out = formatter.prompt_response_text(
        resp.project_id, resp.project_name, resp.status.value, resp.estimated_steps,
    )
    await thinking.edit_text(text_out, parse_mode="MarkdownV2", reply_markup=back_to_home())

    # Start live-streaming this project's progress into THIS same message,
    # per the spec's "edit continuously" requirement (Section 6).
    listener = context.bot_data.get("ws_listener")
    if listener is not None:
        listener.watch(resp.project_id, chat_id=chat_id, message_id=thinking.message_id)


@require_authorized
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text messages (no leading /) are treated as a prompt - either
    for a brand new project, or a follow-up to a project the user was just
    asked about, per the awaiting_prompt_for state set above."""
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    project_id = context.user_data.get("awaiting_prompt_for")
    thinking = await update.effective_message.reply_text("⏳ Sending to agent…")
    await _send_prompt_text(text, project_id, thinking, update.effective_chat.id, context)


@require_authorized
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice notes: reject anything over the configured duration limit
    (before ever downloading - a long ramble transcribed as one prompt is
    rarely what's wanted, and long-audio CPU transcription is slow), then
    download, transcribe, and show the transcript with confirm/edit/discard
    buttons - never sent to the agent automatically, see the module
    docstring for why."""
    voice = update.effective_message.voice
    settings = context.bot_data["settings"]

    if voice.duration > settings.voice_max_duration_seconds:
        limit = int(settings.voice_max_duration_seconds)
        await update.effective_message.reply_text(
            f"⚠️ That voice note is {voice.duration}s long, longer than the {limit}s limit. "
            f"Send a shorter one, or just type your prompt instead."
        )
        return

    transcription = context.bot_data.get("transcription")
    if transcription is None:
        await update.effective_message.reply_text("⚠️ Voice transcription isn't available right now.")
        return

    placeholder = await update.effective_message.reply_text("🎙️ Transcribing…")

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        file = await voice.get_file()
        await file.download_to_drive(custom_path=str(tmp_path))
        text = await transcription.transcribe(tmp_path)
    except Exception as e:
        logger.exception("Voice transcription failed")
        await placeholder.edit_text(f"⚠️ Couldn't transcribe that voice note: {e}")
        return
    finally:
        # Always clean up - on success, on transcription failure, and on a
        # download failure that happened after the temp file was created.
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    text = text.strip()
    if not text:
        await placeholder.edit_text(
            "⚠️ Couldn't make out any speech in that voice note. Try again, or type your prompt."
        )
        return

    project_id = context.user_data.get("awaiting_prompt_for")
    context.user_data["pending_voice_transcript"] = {"text": text, "project_id": project_id}

    label = "follow-up" if project_id else "new prompt"
    await placeholder.edit_text(
        f"🎙️ Heard this \\({esc(label)}\\):\n\n_{esc(text)}_\n\nSend it?",
        parse_mode="MarkdownV2",
        reply_markup=voice_confirm_keyboard(),
    )


@require_authorized
async def voice_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """voice:send - the transcript was confirmed correct; send it exactly
    like a typed prompt would be, through the same shared body."""
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_voice_transcript")
    if not pending:
        await query.edit_message_text(
            "⚠️ This transcript is no longer available \\- send a new voice note\\.",
            parse_mode="MarkdownV2",
        )
        return
    thinking = await query.edit_message_text("⏳ Sending to agent…")
    await _send_prompt_text(pending["text"], pending["project_id"], thinking,
                             update.effective_chat.id, context)


@require_authorized
async def voice_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """voice:edit - the transcript was wrong; fall back to the normal typed
    flow instead of sending a misheard prompt. Reuses the same
    awaiting_prompt_for state the button/command flows already set, so the
    next plain-text message goes through handle_text_message exactly as if
    "New Prompt" or "Send Follow-up" had been tapped directly - no
    double-send risk, since nothing was sent to the agent yet."""
    query = update.callback_query
    await query.answer()
    pending = context.user_data.pop("pending_voice_transcript", None)
    project_id = pending["project_id"] if pending else None
    context.user_data["awaiting_prompt_for"] = project_id
    if project_id:
        await query.edit_message_text(
            f"⌨️ OK \\- type your follow\\-up for `{project_id}` instead\\.",
            parse_mode="MarkdownV2",
        )
    else:
        await query.edit_message_text("⌨️ OK - type your prompt instead.")


@require_authorized
async def voice_discard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """voice:discard - drop the transcript, nothing further happens."""
    query = update.callback_query
    await query.answer("Discarded")
    context.user_data.pop("pending_voice_transcript", None)
    await query.edit_message_text("❌ Discarded.")
