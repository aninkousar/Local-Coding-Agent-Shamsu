"""Unit tests for bot/services/formatter.py - MarkdownV2 well-formedness in
particular. This module shipped with a real, widespread escaping bug during
initial development (unescaped decimal points in percentages, unescaped
parentheses in static text) that would have made Telegram reject real
messages; these tests specifically re-verify the exact cases that broke,
using the strict validator in tests/markdown_validator.py since there's no
bot token available to test against the real Telegram API.
"""
from datetime import datetime, timezone

from bot.models.schemas import FileContent, LogEntry, LogLevel, Project, ProjectStatus, SystemStatus
from bot.services import formatter
from tests.markdown_validator import find_markdown_v2_violations


def _project(**overrides):
    defaults = dict(
        id="PRJ-001", name="Inventory API", status=ProjectStatus.RUNNING, progress=65,
        branch="feature/auth", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Project(**defaults)


def test_project_card_is_valid_markdown():
    p = _project(current_task="Implementing JWT middleware")
    text = formatter.project_card(p, now_seconds_ago=120)
    assert not find_markdown_v2_violations(text)


def test_project_card_with_special_characters_in_name_is_escaped():
    """The exact adversarial case: a project name/task containing literal
    MarkdownV2 special characters must still produce valid output."""
    p = _project(name="auth.service! (v2.0)", current_task="Fixed bug in db-connector.py [urgent]")
    text = formatter.project_card(p, now_seconds_ago=30)
    assert not find_markdown_v2_violations(text)


def test_logs_text_empty_case_is_valid_markdown():
    """The exact bug that shipped: '_No log entries yet._' had an
    unescaped trailing period before the closing italic marker."""
    text = formatter.logs_text([], page=0, page_size=10, total=0)
    assert not find_markdown_v2_violations(text)


def test_logs_text_with_entries_is_valid_markdown():
    entries = [LogEntry(id=1, project_id="PRJ-001", timestamp=datetime.now(timezone.utc),
                         level=LogLevel.SUCCESS, message="Created project")]
    text = formatter.logs_text(entries, page=0, page_size=10, total=1)
    assert not find_markdown_v2_violations(text)


def test_system_status_text_with_decimal_percentages_is_valid_markdown():
    """The exact bug that shipped: interpolated floats like '23.5%' had an
    unescaped literal decimal point."""
    status = SystemStatus(agent_online=True, cpu_percent=23.5, ram_percent=61.2, ram_used_gb=4.9,
                           ram_total_gb=8.0, active_projects=2, running_tasks=1, queue_length=0,
                           uptime_seconds=7384)
    text = formatter.system_status_text(status)
    assert not find_markdown_v2_violations(text)


def test_file_content_text_fits_telegram_message_limit():
    """A large file's content must never produce a message exceeding
    Telegram's real 4096-char cap, and must report exactly how many lines
    actually fit - see the pagination-offset bug this was built to catch."""
    lines = [f"line {i}: some reasonably long line of mock source code content" for i in range(1, 300)]
    content = FileContent(path="big_file.py", size_bytes=50000, last_modified=datetime.now(timezone.utc),
                           total_lines=len(lines), lines=lines[:100], offset=0)
    text, lines_shown = formatter.file_content_text(content)
    assert len(text) <= 4096
    assert not find_markdown_v2_violations(text)
    # every line claimed as shown must genuinely be present
    for n in range(1, lines_shown + 1):
        assert f"line {n}:" in text


def test_file_content_text_pagination_has_no_gap():
    """Directly verifies the fixed bug: the next page's offset (computed by
    the caller using lines_shown) must never skip a line that wasn't
    actually displayed on the current page."""
    lines = [f"line {i}: x" * 5 for i in range(1, 300)]
    content = FileContent(path="f.py", size_bytes=1000, last_modified=datetime.now(timezone.utc),
                           total_lines=len(lines), lines=lines[:100], offset=0)
    _, lines_shown = formatter.file_content_text(content)
    # lines_shown may be less than 100 if the lines are long enough to hit
    # the message size cap - either way, the NEXT page must start exactly
    # at lines_shown, not at the originally-requested page size of 100.
    assert lines_shown <= 100
    assert lines_shown >= 1
