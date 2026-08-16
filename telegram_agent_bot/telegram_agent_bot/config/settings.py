"""Application configuration, loaded from environment variables / .env.

Uses pydantic-settings semantics manually (no extra dependency beyond
pydantic itself) so this stays lightweight: python-dotenv loads the .env
file into os.environ, and this module reads from there with typed
validation and clear defaults.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Load .env from the project root before anything else reads os.environ.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


def _parse_id_list(raw: str) -> List[int]:
    """Parses a comma-separated list of Telegram user IDs from a string,
    tolerant of surrounding whitespace and empty entries."""
    ids: List[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            raise ValueError(
                f"Invalid Telegram user ID in ALLOWED_USER_IDS: {piece!r} - "
                f"this must be a comma-separated list of numeric IDs."
            )
    return ids


class Settings(BaseModel):
    """Typed, validated application settings. Construct via Settings.load()
    rather than the constructor directly, so environment parsing happens in
    one place with clear error messages for missing/invalid values."""

    telegram_bot_token: str
    allowed_user_ids: List[int] = Field(default_factory=list)

    agent_api_base_url: str = "http://localhost:8000"
    agent_ws_url: str = "ws://localhost:8000/events"

    database_path: Path = Path("database/bot.db")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    # HTTP client behavior for talking to the agent API
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 3
    http_retry_backoff_seconds: float = 1.5

    # System monitor auto-refresh interval, in seconds (spec: "every 5 seconds")
    system_refresh_seconds: float = 5.0

    @field_validator("allowed_user_ids")
    @classmethod
    def _must_have_at_least_one_user(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError(
                "ALLOWED_USER_IDS is empty - refusing to start with an empty "
                "whitelist, since that would either reject everyone or (if "
                "misconfigured) accidentally allow everyone. Set at least "
                "your own Telegram user ID."
            )
        return v

    @classmethod
    def load(cls) -> "Settings":
        """Reads and validates all settings from the environment. Raises a
        clear, actionable error immediately at startup if anything required
        is missing or malformed, rather than failing confusingly later."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and "
                "fill in your bot token from @BotFather."
            )

        allowed_raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
        allowed_ids = _parse_id_list(allowed_raw)

        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed_ids,
            agent_api_base_url=os.environ.get("AGENT_API_BASE_URL", "http://localhost:8000").rstrip("/"),
            agent_ws_url=os.environ.get("AGENT_WS_URL", "ws://localhost:8000/events"),
            database_path=Path(os.environ.get("DATABASE_PATH", "database/bot.db")),
            log_dir=Path(os.environ.get("LOG_DIR", "logs")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30.0")),
            http_max_retries=int(os.environ.get("HTTP_MAX_RETRIES", "3")),
            http_retry_backoff_seconds=float(os.environ.get("HTTP_RETRY_BACKOFF_SECONDS", "1.5")),
            system_refresh_seconds=float(os.environ.get("SYSTEM_REFRESH_SECONDS", "5.0")),
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Central authorization check - every handler must call this before
        doing anything else. Whitelist-based: only IDs explicitly listed in
        ALLOWED_USER_IDS are accepted, everyone else is rejected."""
        return user_id in self.allowed_user_ids
