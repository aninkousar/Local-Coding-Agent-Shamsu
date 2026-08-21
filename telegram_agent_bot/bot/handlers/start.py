"""/start command and the Home Menu inline keyboard, per the spec's Section 2.

Routing note: each home:* button (new_prompt, projects, active_tasks,
project_status, logs, system) is handled by its own owning module
(prompt.py, projects.py, logs.py, system.py respectively), each registered
with python-telegram-bot's native pattern-matched CallbackQueryHandler in
bot/main.py - e.g. CallbackQueryHandler(projects.projects_callback,
pattern=r"^home:projects$"). This module only owns /start itself and the
"home:root" case (the "🏠 Home" button present on nearly every screen).
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards.inline import home_menu
from bot.services.auth import require_authorized

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "🤖 *Local Coding Agent — Remote Control*\n\n"
    "Choose an action below, or use /newprompt to describe a task directly\\."
)


@require_authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        WELCOME_TEXT, parse_mode="MarkdownV2", reply_markup=home_menu(),
    )


@require_authorized
async def home_root_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles exactly `home:root` - the "🏠 Home" button's target everywhere
    it appears."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(WELCOME_TEXT, parse_mode="MarkdownV2", reply_markup=home_menu())

