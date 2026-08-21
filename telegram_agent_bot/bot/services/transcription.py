"""Voice transcription via faster-whisper - runs fully locally, matching the
rest of this stack's "nothing leaves the machine unless you opt in"
posture (Ollama runs locally too; see local-code-agent's own README for the
same principle applied there).

The model is loaded ONCE, via load(), called during bot startup - model
loading (reading/decoding the weights) is the genuinely slow part; actual
transcription of a short voice note is fast once the model is warm. Loading
per-message would make every voice note pay that cost again.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)


class TranscriptionError(Exception):
    """Raised for any failure transcribing a voice note - model not loaded,
    unreadable audio, or anything faster-whisper itself raises. Callers
    catch this one type rather than needing to know faster-whisper's own
    exception surface."""


class TranscriptionService:
    """Wraps a single, shared faster-whisper model instance. Construct once
    at bot startup, call load() once (blocking - run it off the event loop,
    see bot/main.py's _post_init), then call transcribe() per voice note."""

    def __init__(self, model_size: str = "base", language: Optional[str] = None) -> None:
        self._model_size = model_size
        self._language = language
        self._model: Optional[WhisperModel] = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Loads the whisper model - blocking, and the slow step (reading or
        downloading model weights). Call once during startup, not per voice
        note. Safe to call again (e.g. after a config change) - simply
        reloads."""
        logger.info("Loading faster-whisper model (%s)...", self._model_size)
        self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        logger.info("faster-whisper model (%s) loaded", self._model_size)

    def _transcribe_sync(self, file_path: Path) -> str:
        if self._model is None:
            raise TranscriptionError(
                "TranscriptionService.load() must be called before transcribe() - "
                "the model isn't loaded yet."
            )
        try:
            segments, _info = self._model.transcribe(str(file_path), language=self._language)
            return " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as e:
            raise TranscriptionError(f"Transcription failed: {e}") from e

    async def transcribe(self, file_path: Path) -> str:
        """Runs the blocking faster-whisper call in a thread pool executor so
        it never blocks the bot's asyncio event loop - CPU transcription of
        even a short clip takes real seconds, long enough to stall every
        other update the bot would otherwise be handling concurrently."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, file_path)
