"""System Monitor - spec Section 9. Auto-refresh (every 5s "while open") is
implemented via a JobQueue-scheduled repeating edit, started when the
screen opens and stopped when the user navigates away - wired in
bot/main.py's job scheduling, not here; this module owns rendering.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from api_client.client import AgentAPIError
from bot.keyboards.inline import system_status_keyboard
from bot.services import formatter
from bot.services.auth import require_authorized

logger = logging.getLogger(__name__)


async def _render_system_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.bot_data["api_client"]
    query = update.callback_query
    try:
        status = await client.get_system_status()
    except AgentAPIError as e:
        await query.edit_message_text(f"⚠️ Couldn't reach the agent: {e}", reply_markup=system_status_keyboard())
        return

    text = formatter.system_status_text(status)
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=system_status_keyboard())


@require_authorized
async def home_system_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """home:system - opens the System Monitor and starts its auto-refresh job."""
    query = update.callback_query
    await query.answer()
    await _render_system_status(update, context)
    _start_auto_refresh(update, context)


def _start_auto_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Schedules a repeating job that re-renders the system status message
    every SYSTEM_REFRESH_SECONDS, per the spec's "Refresh every 5 seconds
    while open". Any previous auto-refresh job for this chat is cancelled
    first, so navigating to System twice doesn't stack up duplicate jobs
    all editing the same message."""
    job_queue = context.job_queue
    chat_id = update.effective_chat.id
    job_name = f"system_refresh:{chat_id}"

    for job in job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    interval = context.bot_data["settings"].system_refresh_seconds
    job_queue.run_repeating(
        _refresh_job, interval=interval, first=interval, name=job_name,
        chat_id=chat_id, data={"message_id": update.callback_query.message.message_id},
    )


async def _refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The JobQueue callback - re-fetches and re-renders in place. Stops
    itself (removes the job) if the edit fails because the message was
    deleted or the user navigated away and Telegram now rejects the edit,
    rather than continuing to fail silently forever in the background."""
    client = context.bot_data["api_client"]
    chat_id = context.job.chat_id
    message_id = context.job.data["message_id"]

    try:
        status = await client.get_system_status()
    except AgentAPIError:
        return  # transient - try again on the next tick rather than giving up

    text = formatter.system_status_text(status)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            parse_mode="MarkdownV2", reply_markup=system_status_keyboard(),
        )
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        logger.info("Stopping system auto-refresh for chat %s (message no longer editable: %s)", chat_id, e)
        context.job.schedule_removal()
