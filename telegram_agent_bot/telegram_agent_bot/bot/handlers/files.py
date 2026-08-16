"""File Browser - spec Section 10. Navigating into a project shows folders
(src/, api/, models/, tests/, README.md per the example), selecting a file
shows path/size/last-modified/first-100-lines, paginated for larger files.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from api_client.client import AgentAPIError
from bot.keyboards.inline import file_browser, file_content_pagination
from bot.services import formatter
from bot.services.auth import require_authorized

logger = logging.getLogger(__name__)

_LINES_PER_PAGE = 100  # matches the spec's "First 100 lines"


@require_authorized
async def project_files_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """project:files:{project_id}:{path} - shows a directory listing at
    `path` (empty string = project root)."""
    query = update.callback_query
    await query.answer()
    _, _, project_id, path = query.data.split(":", 3)

    client = context.bot_data["api_client"]
    try:
        entries = await client.get_files(project_id, path=path)
    except AgentAPIError as e:
        await query.edit_message_text(f"⚠️ Couldn't list files: {e}")
        return

    text = formatter.file_list_text(project_id, path, entries)
    await query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=file_browser(project_id, path, entries),
    )


@require_authorized
async def file_browse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """file:browse:{project_id}:{path} - a file_browser button was pressed;
    dispatches to either a deeper directory listing (path ends in "/") or
    file content (otherwise), since the same keyboard is used for both
    folders and files per the spec's example tree."""
    query = update.callback_query
    await query.answer()
    _, _, project_id, path = query.data.split(":", 3)

    if path.endswith("/"):
        client = context.bot_data["api_client"]
        try:
            entries = await client.get_files(project_id, path=path)
        except AgentAPIError as e:
            await query.edit_message_text(f"⚠️ Couldn't list files: {e}")
            return
        text = formatter.file_list_text(project_id, path, entries)
        await query.edit_message_text(
            text, parse_mode="MarkdownV2", reply_markup=file_browser(project_id, path, entries),
        )
    else:
        await _render_file_content(update, context, project_id, path, offset=0)


@require_authorized
async def file_content_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """file:content:{project_id}:{path}:{offset} - pagination within a file's content."""
    query = update.callback_query
    await query.answer()
    _, _, project_id, path, offset_str = query.data.split(":", 4)
    await _render_file_content(update, context, project_id, path, offset=int(offset_str))


async def _render_file_content(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                project_id: str, path: str, offset: int) -> None:
    query = update.callback_query
    client = context.bot_data["api_client"]
    try:
        content = await client.get_file_content(project_id, path, offset=offset, limit=_LINES_PER_PAGE)
    except AgentAPIError as e:
        await query.edit_message_text(f"⚠️ Couldn't read {path}: {e}")
        return

    text, lines_shown = formatter.file_content_text(content)
    await query.edit_message_text(
        text, parse_mode="MarkdownV2",
        reply_markup=file_content_pagination(project_id, path, offset, lines_shown, content.total_lines),
    )
