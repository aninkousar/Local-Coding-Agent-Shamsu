"""Tests for bot/services/transcription.py. faster-whisper itself is mocked
at the WhisperModel boundary - no real model download/inference needed in
CI, matching how this whole test suite avoids needing a live network
service (see mock_agent/ for the same principle applied to the agent API).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bot.services.transcription as transcription_module
from bot.services.transcription import TranscriptionError, TranscriptionService


async def test_transcribe_before_load_raises_transcription_error():
    svc = TranscriptionService(model_size="base")
    with pytest.raises(TranscriptionError, match="load"):
        await svc.transcribe(Path("/tmp/fake.oga"))


def test_load_constructs_whisper_model_with_correct_args():
    fake_instance = MagicMock()
    FakeWhisperModel = MagicMock(return_value=fake_instance)
    with patch.object(transcription_module, "WhisperModel", FakeWhisperModel):
        svc = TranscriptionService(model_size="small", language="en")
        assert svc.is_loaded is False
        svc.load()
        assert svc.is_loaded is True
        FakeWhisperModel.assert_called_once_with("small", device="cpu", compute_type="int8")


async def test_transcribe_joins_segments_and_passes_language():
    fake_instance = MagicMock()
    seg1 = MagicMock(text=" build a fastapi ")
    seg2 = MagicMock(text="hello world endpoint ")
    fake_instance.transcribe.return_value = ([seg1, seg2], MagicMock())
    FakeWhisperModel = MagicMock(return_value=fake_instance)

    with patch.object(transcription_module, "WhisperModel", FakeWhisperModel):
        svc = TranscriptionService(model_size="base", language="en")
        svc.load()
        result = await svc.transcribe(Path("/tmp/voice.oga"))

    assert result == "build a fastapi hello world endpoint"
    fake_instance.transcribe.assert_called_once_with("/tmp/voice.oga", language="en")


async def test_transcribe_wraps_model_exceptions():
    fake_instance = MagicMock()
    fake_instance.transcribe.side_effect = RuntimeError("corrupt audio data")
    FakeWhisperModel = MagicMock(return_value=fake_instance)

    with patch.object(transcription_module, "WhisperModel", FakeWhisperModel):
        svc = TranscriptionService()
        svc.load()
        with pytest.raises(TranscriptionError, match="corrupt audio data"):
            await svc.transcribe(Path("/tmp/bad.oga"))


async def test_transcribe_does_not_block_the_event_loop():
    """The real, decisive check for run_in_executor being used correctly -
    not just present in the source, but genuinely allowing other async
    work to proceed concurrently with a slow (CPU-bound) transcription."""
    import asyncio
    import time

    fake_instance = MagicMock()

    def slow_transcribe(*args, **kwargs):
        time.sleep(0.5)
        return ([MagicMock(text="done")], MagicMock())

    fake_instance.transcribe.side_effect = slow_transcribe
    FakeWhisperModel = MagicMock(return_value=fake_instance)

    with patch.object(transcription_module, "WhisperModel", FakeWhisperModel):
        svc = TranscriptionService()
        svc.load()

        other_ran_concurrently = []

        async def other_work():
            await asyncio.sleep(0.1)
            other_ran_concurrently.append(True)

        start = time.monotonic()
        await asyncio.gather(svc.transcribe(Path("/tmp/x.oga")), other_work())
        elapsed = time.monotonic() - start

    assert other_ran_concurrently, "the event loop was blocked - other async work never got to run"
    assert elapsed < 0.6, "should overlap (~0.5s), not serialize (~0.6s+)"
