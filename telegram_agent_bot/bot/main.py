"""Bot entry point - wires configuration, the API client, the database, the
WebSocket listener, and every handler together, then starts polling.

Handler registration uses python-telegram-bot's native pattern-matched
CallbackQueryHandler - each handler module owns a specific callback_data
namespace (see the docstrings in bot/keyboards/inline.py and each handler
module), registered here with an explicit regex per pattern rather than a
single hand-rolled dispatch table.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from api_client.client import AgentAPIClient
from bot.handlers import files, logs, projects, prompt, start, system
from bot.services.transcription import TranscriptionService
from bot.services.websocket_listener import WebSocketListener
from config.settings import Settings
from database.db import Database
from utils.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """Runs once after the Application is built but before polling starts -
    this is where long-lived resources (API client, database, WebSocket
    listener) are created and stashed in bot_data, and the WebSocket
    listener's background task is launched."""
    settings: Settings = application.bot_data["settings"]

    api_client = AgentAPIClient(
        settings.agent_api_base_url,
        timeout=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        retry_backoff=settings.http_retry_backoff_seconds,
    )
    db = await Database.connect(settings.database_path)
    ws_listener = WebSocketListener(settings.agent_ws_url, application.bot)

    transcription = TranscriptionService(
        model_size=settings.whisper_model_size, language=settings.whisper_language,
    )
    # Model loading reads/decodes real weights off disk - genuinely slow and
    # blocking, same reasoning as transcribe() itself running off the event
    # loop. Doing it here, once, at startup means every voice note after
    # this point transcribes against an already-warm model.
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, transcription.load)
        application.bot_data["transcription"] = transcription
    except Exception:
        logger.exception(
            "Failed to load the faster-whisper model (%s) - voice messages will be "
            "unavailable this session, everything else still works.",
            settings.whisper_model_size,
        )
        application.bot_data["transcription"] = None

    application.bot_data["api_client"] = api_client
    application.bot_data["db"] = db
    application.bot_data["ws_listener"] = ws_listener

    application.bot_data["ws_task"] = asyncio.create_task(ws_listener.run_forever())
    logger.info("Bot initialized - agent API: %s, agent WS: %s",
                settings.agent_api_base_url, settings.agent_ws_url)


async def _post_shutdown(application: Application) -> None:
    """Mirror of _post_init - closes everything that was opened, so a clean
    shutdown (Ctrl+C) doesn't leak the HTTP client, the WS connection, or
    the database connection."""
    ws_listener: WebSocketListener = application.bot_data.get("ws_listener")
    if ws_listener:
        ws_listener.stop()
    ws_task = application.bot_data.get("ws_task")
    if ws_task and not ws_task.done():
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass

    api_client: AgentAPIClient = application.bot_data.get("api_client")
    if api_client:
        await api_client.close()

    db: Database = application.bot_data.get("db")
    if db:
        await db.close()

    logger.info("Bot shut down cleanly")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler - logs every uncaught exception from any
    handler, and tries to tell the user something went wrong rather than
    leaving them looking at a stuck "⏳" message forever."""
    logger.exception("Unhandled error while processing update: %s", update, exc_info=context.error)
    if isinstance(update, Update):
        try:
            if update.effective_message:
                await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
            elif update.callback_query:
                await update.callback_query.answer("⚠️ Something went wrong.", show_alert=True)
        except Exception:
            pass  # even the error-reporting path can fail (e.g. chat no longer reachable) - don't cascade


def build_application(settings: Settings) -> Application:
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings

    # Commands
    application.add_handler(CommandHandler("start", start.start_command))
    application.add_handler(CommandHandler("newprompt", prompt.newprompt_command))

    # Home menu callbacks
    application.add_handler(CallbackQueryHandler(start.home_root_callback, pattern=r"^home:root$"))
    application.add_handler(CallbackQueryHandler(prompt.home_new_prompt_callback, pattern=r"^home:new_prompt$"))
    application.add_handler(CallbackQueryHandler(projects.home_projects_callback, pattern=r"^home:projects$"))
    application.add_handler(CallbackQueryHandler(projects.home_projects_callback, pattern=r"^home:active_tasks$"))
    application.add_handler(CallbackQueryHandler(projects.home_projects_callback, pattern=r"^home:project_status$"))
    application.add_handler(CallbackQueryHandler(logs.home_logs_callback, pattern=r"^home:logs$"))
    application.add_handler(CallbackQueryHandler(system.home_system_callback, pattern=r"^home:system$"))

    # Project dashboard / detail / controls
    application.add_handler(CallbackQueryHandler(projects.project_list_page_callback, pattern=r"^project:list_page:"))
    application.add_handler(CallbackQueryHandler(projects.project_view_callback, pattern=r"^project:view:"))
    application.add_handler(CallbackQueryHandler(projects.project_pause_callback, pattern=r"^project:pause:"))
    application.add_handler(CallbackQueryHandler(projects.project_resume_callback, pattern=r"^project:resume:"))
    application.add_handler(CallbackQueryHandler(projects.project_stop_callback, pattern=r"^project:stop:"))
    application.add_handler(CallbackQueryHandler(prompt.project_followup_callback, pattern=r"^project:followup:"))

    # Logs
    application.add_handler(CallbackQueryHandler(logs.project_logs_callback, pattern=r"^project:logs:"))

    # Files
    application.add_handler(CallbackQueryHandler(files.project_files_callback, pattern=r"^project:files:"))
    application.add_handler(CallbackQueryHandler(files.file_browse_callback, pattern=r"^file:browse:"))
    application.add_handler(CallbackQueryHandler(files.file_content_callback, pattern=r"^file:content:"))

    # System
    # Note: the "🔄 Refresh" button intentionally reuses home:system's own
    # callback_data (see system_status_keyboard() in inline.py) - there's no
    # separate handler registered for it here, since home_system_callback
    # already correctly re-renders and restarts the auto-refresh job on
    # every call, whether it's the first open or a manual refresh.

    # Plain text = a prompt (new or follow-up, decided by conversation state)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, prompt.handle_text_message))

    # Voice notes: transcribe -> confirm/edit/discard, see bot/handlers/prompt.py
    application.add_handler(MessageHandler(filters.VOICE, prompt.handle_voice_message))
    application.add_handler(CallbackQueryHandler(prompt.voice_send_callback, pattern=r"^voice:send$"))
    application.add_handler(CallbackQueryHandler(prompt.voice_edit_callback, pattern=r"^voice:edit$"))
    application.add_handler(CallbackQueryHandler(prompt.voice_discard_callback, pattern=r"^voice:discard$"))

    application.add_error_handler(_on_error)
    return application


def main() -> None:
    try:
        settings = Settings.load()
    except RuntimeError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    configure_logging(settings.log_dir, settings.log_level)
    logger.info("Starting Telegram Agent Bot - allowed users: %s", settings.allowed_user_ids)

    application = build_application(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
