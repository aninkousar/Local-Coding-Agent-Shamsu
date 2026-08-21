"""Authentication - the whitelist gate every handler must pass through.

Implemented as a decorator (`require_authorized`) rather than a check
handlers remember to call themselves, specifically so a handler that
forgets the check is a missing decorator (visible in a code review) rather
than a missing function call (easy to overlook). Every handler in
bot/handlers/ is wrapped with this.
"""
from __future__ import annotations

import functools
import logging
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def require_authorized(handler: Callable) -> Callable:
    """Wraps a python-telegram-bot handler function so it only runs for
    whitelisted user IDs. Rejects silently-but-visibly for everyone else:
    logs the attempt (so unexpected access attempts are noticeable in the
    logs) and sends a short rejection message, rather than either staying
    completely silent (confusing for the rejected user) or revealing
    anything about what the bot does (no hint of functionality leaked to
    an unauthorized caller).
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        settings = context.bot_data["settings"]

        if user is None or not settings.is_user_allowed(user.id):
            user_id = user.id if user else "unknown"
            username = user.username if user else "unknown"
            logger.warning("Rejected unauthorized access attempt: user_id=%s username=%s", user_id, username)
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Not authorized.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Not authorized.", show_alert=True)
            return

        return await handler(update, context, *args, **kwargs)

    return wrapper
