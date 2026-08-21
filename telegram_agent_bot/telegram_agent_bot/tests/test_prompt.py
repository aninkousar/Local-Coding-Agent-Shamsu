"""Tests for bot/handlers/prompt.py - the shared _send_prompt_text helper
(exercised by both the typed-text and voice-confirmation flows), voice
message handling including the duration limit and temp-file cleanup, and
the voice:send/edit/discard callbacks. Transcription is mocked - no real
audio needed, matching how the rest of this suite avoids needing a live
network service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from api_client.client import AgentAPIError
from bot.handlers import prompt
from bot.models.schemas import Project, ProjectStatus, PromptResponse


def _make_context(client=None, db=None, settings=None, transcription=None, ws_listener=None,
                   user_data=None):
    context = MagicMock()
    context.bot_data = {
        "api_client": client or AsyncMock(),
        "db": db or AsyncMock(),
        "settings": settings or MagicMock(voice_max_duration_seconds=120.0),
        "transcription": transcription,
        "ws_listener": ws_listener,
    }
    context.user_data = user_data if user_data is not None else {}
    return context


def _make_project(project_id="PRJ-001", name="Test Project"):
    now = datetime.now(timezone.utc)
    return Project(id=project_id, name=name, status=ProjectStatus.RUNNING, progress=10,
                   created_at=now, updated_at=now)


# --- _send_prompt_text: the shared body both flows depend on ------------------------

async def test_send_prompt_text_new_project():
    client = AsyncMock()
    client.send_prompt.return_value = PromptResponse(
        project_id="PRJ-001", project_name="New Thing", status=ProjectStatus.QUEUED, estimated_steps=3,
    )
    client.get_project.return_value = _make_project()
    db = AsyncMock()
    listener = MagicMock()
    listener.watch = MagicMock()
    context = _make_context(client=client, db=db, ws_listener=listener,
                             user_data={"awaiting_prompt_for": "stale-should-be-cleared"})

    thinking = AsyncMock()
    thinking.message_id = 555

    await prompt._send_prompt_text("build a thing", None, thinking, chat_id=999, context=context)

    client.send_prompt.assert_called_once_with("build a thing")
    db.upsert_project.assert_called_once()
    thinking.edit_text.assert_called_once()
    call_kwargs = thinking.edit_text.call_args
    assert "PRJ\\-001" in call_kwargs.args[0] or "PRJ-001" in call_kwargs.args[0]
    listener.watch.assert_called_once_with("PRJ-001", chat_id=999, message_id=555)
    assert context.user_data["awaiting_prompt_for"] is None


async def test_send_prompt_text_followup_refreshes_cache_and_logs():
    client = AsyncMock()
    client.get_project.return_value = _make_project(project_id="PRJ-002")
    client.send_followup_prompt.return_value = PromptResponse(
        project_id="PRJ-002", project_name="Existing", status=ProjectStatus.RUNNING, estimated_steps=None,
    )
    db = AsyncMock()
    context = _make_context(client=client, db=db)
    thinking = AsyncMock()
    thinking.message_id = 111

    await prompt._send_prompt_text("add caching", "PRJ-002", thinking, chat_id=1, context=context)

    client.get_project.assert_called_once_with("PRJ-002")
    db.upsert_project.assert_called_once()
    client.send_followup_prompt.assert_called_once_with("PRJ-002", "add caching")
    db.add_log.assert_called_once()
    assert "add caching" in db.add_log.call_args.args[2]


async def test_send_prompt_text_agent_error_shows_message_and_skips_watcher():
    client = AsyncMock()
    client.send_prompt.side_effect = AgentAPIError("agent unreachable")
    listener = MagicMock()
    context = _make_context(client=client, ws_listener=listener)
    thinking = AsyncMock()

    await prompt._send_prompt_text("build a thing", None, thinking, chat_id=1, context=context)

    thinking.edit_text.assert_called_once()
    assert "agent unreachable" in thinking.edit_text.call_args.args[0]
    listener.watch.assert_not_called()


async def test_send_prompt_text_clears_pending_voice_transcript_on_success():
    """A stale pending_voice_transcript must never resurface after a prompt
    has already been sent - regardless of whether it came from the voice
    flow or not, since a user could send text after leaving a voice
    transcript unanswered."""
    client = AsyncMock()
    client.send_prompt.return_value = PromptResponse(
        project_id="PRJ-001", project_name="X", status=ProjectStatus.QUEUED, estimated_steps=None,
    )
    client.get_project.return_value = _make_project()
    context = _make_context(client=client, user_data={
        "pending_voice_transcript": {"text": "stale", "project_id": None},
    })
    thinking = AsyncMock()

    await prompt._send_prompt_text("build a thing", None, thinking, chat_id=1, context=context)
    assert "pending_voice_transcript" not in context.user_data


async def test_send_prompt_text_clears_pending_voice_transcript_on_error():
    client = AsyncMock()
    client.send_prompt.side_effect = AgentAPIError("down")
    context = _make_context(client=client, user_data={
        "pending_voice_transcript": {"text": "stale", "project_id": None},
    })
    thinking = AsyncMock()

    await prompt._send_prompt_text("build a thing", None, thinking, chat_id=1, context=context)
    assert "pending_voice_transcript" not in context.user_data


# --- handle_voice_message: duration limit, rejected before any download -------------

async def test_voice_rejected_over_duration_limit_before_download():
    settings = MagicMock(voice_max_duration_seconds=120.0)
    context = _make_context(settings=settings)

    update = MagicMock()
    voice = MagicMock(duration=180)
    voice.get_file = AsyncMock()
    update.effective_message.voice = voice
    update.effective_message.reply_text = AsyncMock()

    await prompt.handle_voice_message(update, context)

    voice.get_file.assert_not_called()
    update.effective_message.reply_text.assert_called_once()
    msg = update.effective_message.reply_text.call_args.args[0]
    assert "180" in msg and "120" in msg


async def test_voice_accepted_within_duration_limit_attempts_download():
    settings = MagicMock(voice_max_duration_seconds=120.0)
    transcription = AsyncMock()
    transcription.transcribe.return_value = "build a fastapi endpoint"
    context = _make_context(settings=settings, transcription=transcription)

    update = MagicMock()
    voice = MagicMock(duration=30)
    tg_file = AsyncMock()
    tg_file.download_to_drive = AsyncMock()
    voice.get_file = AsyncMock(return_value=tg_file)
    update.effective_message.voice = voice

    placeholder = AsyncMock()
    update.effective_message.reply_text = AsyncMock(return_value=placeholder)

    await prompt.handle_voice_message(update, context)

    voice.get_file.assert_called_once()
    tg_file.download_to_drive.assert_called_once()
    transcription.transcribe.assert_called_once()


# --- handle_voice_message: temp file cleanup, success/failure/empty paths -----------

async def test_voice_temp_file_cleaned_up_on_success():
    settings = MagicMock(voice_max_duration_seconds=120.0)
    transcription = AsyncMock()
    transcription.transcribe.return_value = "build a fastapi endpoint"
    context = _make_context(settings=settings, transcription=transcription)

    update = MagicMock()
    voice = MagicMock(duration=10)
    tg_file = AsyncMock()

    captured_path = {}

    async def fake_download(custom_path):
        captured_path["path"] = Path(custom_path)
        assert Path(custom_path).exists(), "temp file should exist at download time"

    tg_file.download_to_drive = fake_download
    voice.get_file = AsyncMock(return_value=tg_file)
    update.effective_message.voice = voice
    placeholder = AsyncMock()
    update.effective_message.reply_text = AsyncMock(return_value=placeholder)

    await prompt.handle_voice_message(update, context)

    assert "path" in captured_path
    assert not captured_path["path"].exists(), "temp file must be deleted after successful transcription"
    placeholder.edit_text.assert_called_once()
    assert context.user_data["pending_voice_transcript"]["text"] == "build a fastapi endpoint"


async def test_voice_temp_file_cleaned_up_on_transcription_failure():
    settings = MagicMock(voice_max_duration_seconds=120.0)
    transcription = AsyncMock()
    transcription.transcribe.side_effect = RuntimeError("model exploded")
    context = _make_context(settings=settings, transcription=transcription)

    update = MagicMock()
    voice = MagicMock(duration=10)
    tg_file = AsyncMock()
    captured_path = {}

    async def fake_download(custom_path):
        captured_path["path"] = Path(custom_path)

    tg_file.download_to_drive = fake_download
    voice.get_file = AsyncMock(return_value=tg_file)
    update.effective_message.voice = voice
    placeholder = AsyncMock()
    update.effective_message.reply_text = AsyncMock(return_value=placeholder)

    await prompt.handle_voice_message(update, context)

    assert "path" in captured_path
    assert not captured_path["path"].exists(), "temp file must be deleted even when transcription fails"
    placeholder.edit_text.assert_called_once()
    assert "couldn't transcribe" in placeholder.edit_text.call_args.args[0].lower()


async def test_voice_empty_transcript_shows_no_speech_message_not_confirm_buttons():
    settings = MagicMock(voice_max_duration_seconds=120.0)
    transcription = AsyncMock()
    transcription.transcribe.return_value = "   "  # whitespace-only, treated as empty
    context = _make_context(settings=settings, transcription=transcription)

    update = MagicMock()
    voice = MagicMock(duration=10)
    tg_file = AsyncMock()
    tg_file.download_to_drive = AsyncMock()
    voice.get_file = AsyncMock(return_value=tg_file)
    update.effective_message.voice = voice
    placeholder = AsyncMock()
    update.effective_message.reply_text = AsyncMock(return_value=placeholder)

    await prompt.handle_voice_message(update, context)

    placeholder.edit_text.assert_called_once()
    call = placeholder.edit_text.call_args
    assert "couldn't make out" in call.args[0].lower()
    assert call.kwargs.get("reply_markup") is None
    assert "pending_voice_transcript" not in context.user_data


async def test_voice_transcription_unavailable_shows_clear_message():
    """context.bot_data["transcription"] is None if the model failed to load
    at startup - must degrade gracefully, not crash."""
    settings = MagicMock(voice_max_duration_seconds=120.0)
    context = _make_context(settings=settings, transcription=None)

    update = MagicMock()
    voice = MagicMock(duration=10)
    voice.get_file = AsyncMock()
    update.effective_message.voice = voice
    update.effective_message.reply_text = AsyncMock()

    await prompt.handle_voice_message(update, context)

    voice.get_file.assert_not_called()
    msg = update.effective_message.reply_text.call_args.args[0]
    assert "isn't available" in msg.lower()


# --- voice:send / voice:edit / voice:discard callbacks -------------------------------

async def test_voice_send_callback_sends_pending_transcript():
    client = AsyncMock()
    client.send_prompt.return_value = PromptResponse(
        project_id="PRJ-001", project_name="X", status=ProjectStatus.QUEUED, estimated_steps=None,
    )
    client.get_project.return_value = _make_project()
    context = _make_context(client=client, user_data={
        "pending_voice_transcript": {"text": "build a thing", "project_id": None},
    })

    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    thinking = AsyncMock()
    thinking.message_id = 42
    update.callback_query.edit_message_text = AsyncMock(return_value=thinking)

    await prompt.voice_send_callback(update, context)

    client.send_prompt.assert_called_once_with("build a thing")
    assert "pending_voice_transcript" not in context.user_data


async def test_voice_send_callback_with_no_pending_transcript_does_not_call_agent():
    client = AsyncMock()
    context = _make_context(client=client, user_data={})

    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    await prompt.voice_send_callback(update, context)

    client.send_prompt.assert_not_called()
    client.send_followup_prompt.assert_not_called()
    msg = update.callback_query.edit_message_text.call_args.args[0]
    assert "no longer available" in msg


async def test_voice_edit_callback_falls_back_to_typed_flow():
    context = _make_context(user_data={
        "pending_voice_transcript": {"text": "misheard", "project_id": "PRJ-007"},
    })
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    await prompt.voice_edit_callback(update, context)

    assert "pending_voice_transcript" not in context.user_data
    assert context.user_data["awaiting_prompt_for"] == "PRJ-007"
    msg = update.callback_query.edit_message_text.call_args.args[0]
    assert "PRJ\\-007" in msg or "PRJ-007" in msg


async def test_voice_discard_callback_clears_state():
    context = _make_context(user_data={
        "pending_voice_transcript": {"text": "nope", "project_id": None},
    })
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    await prompt.voice_discard_callback(update, context)

    assert "pending_voice_transcript" not in context.user_data
    update.callback_query.answer.assert_called_once()
    update.callback_query.edit_message_text.assert_called_once()
