"""Project Dashboard, Project Detail, and pause/resume/stop controls -
spec Sections 4 and 5.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from api_client.client import AgentAPIError
from bot.keyboards.inline import project_detail, project_list
from bot.services import formatter
from bot.services.auth import require_authorized

logger = logging.getLogger(__name__)


def _seconds_since(dt: datetime) -> float:
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _render_project_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    client = context.bot_data["api_client"]
    query = update.callback_query
    try:
        projects = await client.list_projects()
    except AgentAPIError as e:
        await query.edit_message_text(f"⚠️ Couldn't reach the agent: {e}")
        return

    if not projects:
        await query.edit_message_text(
            "No projects yet\\. Use *New Prompt* to start one\\.", parse_mode="MarkdownV2",
        )
        return

    await query.edit_message_text(
        f"*Projects* \\({len(projects)} total\\)", parse_mode="MarkdownV2",
        reply_markup=project_list(projects, page=page),
    )


@require_authorized
async def home_projects_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """home:projects - the Projects dashboard entry point from the home menu."""
    query = update.callback_query
    await query.answer()
    await _render_project_list(update, context, page=0)


@require_authorized
async def project_list_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """project:list_page:{page} - pagination within the project dashboard."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    await _render_project_list(update, context, page=page)


@require_authorized
async def project_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """project:view:{project_id} - opens the Project Detail screen."""
    query = update.callback_query
    await query.answer()
    project_id = query.data.split(":", 2)[2]
    client = context.bot_data["api_client"]
    db = context.bot_data["db"]

    try:
        project = await client.get_project(project_id)
    except AgentAPIError as e:
        await query.edit_message_text(f"⚠️ Couldn't load {project_id}: {e}")
        return

    await db.upsert_project(project)
    tasks = await db.list_tasks(project_id)
    completed = sum(1 for t in tasks if t.status.value == "completed")
    remaining = sum(1 for t in tasks if t.status.value in ("pending", "running"))

    text = formatter.project_detail_text(
        project, completed_count=completed, remaining_count=remaining, files_modified=0,
    )
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=project_detail(project))


async def _control_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    """Shared implementation for pause/resume/stop - identical flow, only
    the action string and confirmation wording differ."""
    query = update.callback_query
    await query.answer(f"{action.capitalize()}ing…")
    project_id = query.data.split(":", 2)[2]
    client = context.bot_data["api_client"]

    try:
        await client.control_project(project_id, action)
        project = await client.get_project(project_id)
    except AgentAPIError as e:
        await query.answer(f"⚠️ Failed to {action}: {e}", show_alert=True)
        return

    await context.bot_data["db"].upsert_project(project)
    text = formatter.project_detail_text(project, completed_count=0, remaining_count=0, files_modified=0)
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=project_detail(project))


@require_authorized
async def project_pause_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _control_action(update, context, "pause")


@require_authorized
async def project_resume_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _control_action(update, context, "resume")


@require_authorized
async def project_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _control_action(update, context, "stop")
