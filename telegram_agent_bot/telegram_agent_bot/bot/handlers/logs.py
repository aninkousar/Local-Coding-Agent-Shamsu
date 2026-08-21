"""Logs view with pagination - spec Section 8."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from api_client.client import AgentAPIError
from bot.keyboards.inline import logs_pagination
from bot.models.schemas import LogEntry, LogLevel
from bot.services import formatter
from bot.services.auth import require_authorized

logger = logging.getLogger(__name__)

_PAGE_SIZE = 10


@require_authorized
async def project_logs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """project:logs:{project_id}:{page} - paginated log view for one project."""
    query = update.callback_query
    await query.answer()
    _, _, project_id, page_str = query.data.split(":", 3)
    page = int(page_str)

    client = context.bot_data["api_client"]
    try:
        raw_logs = await client.get_logs(project_id, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
    except AgentAPIError as e:
        await query.edit_message_text(f"⚠️ Couldn't load logs: {e}")
        return

    entries = [_parse_log_entry(project_id, item) for item in raw_logs]
    # Fetch one extra to know whether a "Next" page actually exists, without
    # needing a separate count endpoint.
    try:
        next_page_raw = await client.get_logs(project_id, limit=1, offset=(page + 1) * _PAGE_SIZE)
        has_more = len(next_page_raw) > 0
    except AgentAPIError:
        has_more = False

    total_estimate = (page * _PAGE_SIZE) + len(entries) + (1 if has_more else 0)
    text = formatter.logs_text(entries, page=page, page_size=_PAGE_SIZE, total=total_estimate)
    await query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=logs_pagination(project_id, page, has_more),
    )


@require_authorized
async def home_logs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """home:logs - the spec lists a top-level Logs button; without a
    specific project selected yet, this shows the project picker so the
    user chooses which project's logs to view (logs are inherently
    per-project, per the /project/{id}/logs endpoint)."""
    from bot.handlers.projects import home_projects_callback
    query = update.callback_query
    await query.answer("Select a project to view its logs")
    await home_projects_callback(update, context)


def _parse_log_entry(project_id: str, raw: dict) -> LogEntry:
    """Tolerant parsing - the agent's log entries may not always include an
    `id`, so one is synthesized rather than failing the whole page over a
    missing optional field."""
    from datetime import datetime, timezone
    return LogEntry(
        id=raw.get("id", 0),
        project_id=project_id,
        timestamp=datetime.fromisoformat(raw["timestamp"]) if isinstance(raw.get("timestamp"), str)
                   else datetime.now(timezone.utc),
        level=LogLevel(raw.get("level", "info")),
        message=raw.get("message", ""),
    )
