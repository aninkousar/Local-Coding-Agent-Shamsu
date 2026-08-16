"""Unit tests for bot/services/auth.py - the whitelist gate every handler
depends on. The single most important property tested here: an
unauthorized user must NEVER reach the wrapped handler, not just receive
a rejection message alongside it."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.auth import require_authorized
from config.settings import Settings


@pytest.fixture
def settings():
    return Settings(telegram_bot_token="x", allowed_user_ids=[111])


async def test_authorized_user_reaches_the_real_handler(settings):
    called = []

    @require_authorized
    async def handler(update, context):
        called.append(True)
        return "ok"

    update = MagicMock()
    update.effective_user.id = 111
    context = MagicMock()
    context.bot_data = {"settings": settings}

    result = await handler(update, context)
    assert called == [True]
    assert result == "ok"


async def test_unauthorized_user_never_reaches_the_real_handler(settings):
    called = []

    @require_authorized
    async def handler(update, context):
        called.append(True)

    update = MagicMock()
    update.effective_user.id = 999
    update.callback_query = None
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"settings": settings}

    await handler(update, context)
    assert called == [], "the wrapped handler must never execute for an unauthorized user"
    update.effective_message.reply_text.assert_called_once()


async def test_missing_effective_user_is_treated_as_unauthorized(settings):
    called = []

    @require_authorized
    async def handler(update, context):
        called.append(True)

    update = MagicMock()
    update.effective_user = None
    update.callback_query = None
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"settings": settings}

    await handler(update, context)
    assert called == []
