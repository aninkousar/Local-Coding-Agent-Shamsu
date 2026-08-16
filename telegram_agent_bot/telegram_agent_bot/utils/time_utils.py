"""Shared time utilities - one place for timezone-aware "now", used
everywhere a default/current timestamp is needed across the codebase."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware UTC now. datetime.utcnow() is deprecated in modern
    Python and returns a naive datetime, a common source of subtle bugs
    when mixed with timezone-aware ones - this is the one place that
    decision is made, so it can't drift between modules."""
    return datetime.now(timezone.utc)
