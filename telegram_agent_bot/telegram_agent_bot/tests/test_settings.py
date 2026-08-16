"""Unit tests for config/settings.py - the whitelist authorization logic
in particular, since a bug here is a real security issue, not just a
functional one."""
import pytest

from config.settings import Settings


def test_allowed_user_is_recognized():
    s = Settings(telegram_bot_token="x", allowed_user_ids=[111, 222])
    assert s.is_user_allowed(111) is True
    assert s.is_user_allowed(222) is True


def test_unlisted_user_is_rejected():
    s = Settings(telegram_bot_token="x", allowed_user_ids=[111])
    assert s.is_user_allowed(999) is False


def test_empty_whitelist_is_rejected_at_construction():
    """An empty whitelist is refused outright, rather than silently
    behaving as either 'reject everyone' or (if some other bug crept in)
    'allow everyone' - see the field_validator in Settings."""
    with pytest.raises(Exception):
        Settings(telegram_bot_token="x", allowed_user_ids=[])


def test_load_requires_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        Settings.load()


def test_load_parses_comma_separated_ids_with_whitespace(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", " 111, 222 ,333")
    s = Settings.load()
    assert s.allowed_user_ids == [111, 222, 333]


def test_load_rejects_non_numeric_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111,not-a-number")
    with pytest.raises(ValueError):
        Settings.load()
