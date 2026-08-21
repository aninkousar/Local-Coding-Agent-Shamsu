r"""Message formatting - turns typed models into the Telegram message text
shown to the user. Centralized here so every handler produces consistent
formatting, and so emoji/style choices ("use emojis sparingly", "avoid
spam") are made in one place rather than repeated inconsistently.

Escaping discipline, stated explicitly because getting this wrong silently
breaks message delivery: MarkdownV2 requires escaping a specific character
set (_*[]()~`>#+-=|{}.!) anywhere it appears as LITERAL text - including
inside decimal numbers ("23.5" has a literal, unescaped period) and inside
otherwise-plain sentences ("(2 min ago)" has literal parentheses). The only
exception is inside a code span (backticks), where nothing but backtick and
backslash need escaping.

Rather than hand-interpolating a mix of already-escaped and not-yet-escaped
fragments into f-strings (easy to miss one, which is exactly what happened
during initial development here - "_No log entries yet._" and interpolated
percentages like "23.5%" were shipped unescaped and would have made
Telegram reject the message), every call site goes through the bold() /
italic() / code() helpers below, which escape their input internally before
adding the markup around it. There is no direct string formatting of
markdown syntax anywhere else in this module.
"""
from __future__ import annotations

from typing import List, Union

from bot.models.schemas import FileContent, FileEntry, LogEntry, Project, SystemStatus

_SPECIAL_CHARS = set(r"_*[]()~`>#+-=|{}.!")


def esc(value: Union[str, int, float]) -> str:
    """Escapes any value for safe use as LITERAL MarkdownV2 text - the one
    place this project's escaping logic lives, used by every helper below
    and safe to call on numbers (via str()) as well as strings, since a
    literal decimal point is exactly as unsafe as a literal parenthesis."""
    text = str(value)
    return "".join(f"\\{c}" if c in _SPECIAL_CHARS else c for c in text)


def bold(value: Union[str, int, float]) -> str:
    return f"*{esc(value)}*"


def italic(value: Union[str, int, float]) -> str:
    return f"_{esc(value)}_"


def code(value: Union[str, int, float]) -> str:
    """Code span - only backtick/backslash need escaping here, but esc()'s
    broader escaping is harmless (over-escaping inside a code span is a
    no-op for display, unlike outside one), so the same helper is reused
    rather than maintaining a second, narrower escaping function."""
    text = str(value)
    safe = text.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{safe}`"


def _progress_bar(percent: int, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def _human_time_ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def project_card(project: Project, now_seconds_ago: float) -> str:
    """Matches the spec's example card layout: Inventory API / Running /
    ID: PRJ-001 / Branch: feature/auth / Progress: 65% / Updated 2 min ago /
    Current task: ..."""
    lines = [
        bold(project.name),
        esc(project.status_line()),
        f"ID: {code(project.id)}",
        f"Branch: {code(project.branch)}",
        f"Progress: {_progress_bar(project.progress)} {esc(project.progress)}\\%",
        f"Updated {esc(_human_time_ago(now_seconds_ago))}",
    ]
    if project.current_task:
        lines.append(f"\nCurrent task:\n{esc(project.current_task)}")
    return "\n".join(lines)


def project_detail_text(project: Project, completed_count: int, remaining_count: int,
                         files_modified: int) -> str:
    lines = [
        bold(project.name),
        esc(project.status_line()),
        "",
        esc(project.description) if project.description else italic("No description"),
        "",
        f"Progress: {_progress_bar(project.progress)} {esc(project.progress)}\\%",
        f"Files modified: {esc(files_modified)}",
        f"Tasks: {esc(completed_count)} completed, {esc(remaining_count)} remaining",
    ]
    if project.current_task:
        lines.append(f"\nCurrent objective:\n{esc(project.current_task)}")
    return "\n".join(lines)


def prompt_response_text(project_id: str, project_name: str, status: str,
                          estimated_steps) -> str:
    """Matches the spec's "Bot returns: Project ID / Project name / Status /
    Estimated steps" for the New Prompt flow."""
    lines = [
        f"✅ {bold('Project created')}",
        f"Project ID: {code(project_id)}",
        f"Name: {esc(project_name)}",
        f"Status: {esc(status)}",
    ]
    if estimated_steps is not None:
        lines.append(f"Estimated steps: {esc(estimated_steps)}")
    return "\n".join(lines)


def logs_text(entries: List[LogEntry], page: int, page_size: int, total: int) -> str:
    """Matches the spec's log format: "10:21 Created Project" style lines."""
    if not entries:
        return italic("No log entries yet")
    level_emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🔴", "success": "✅"}
    lines = [f"{bold('Logs')} \\(page {esc(page + 1)}, {esc(total)} total\\)\n"]
    for e in entries:
        time_str = e.timestamp.strftime("%H:%M")
        emoji = level_emoji.get(e.level.value, "•")
        lines.append(f"{esc(time_str)} {emoji} {esc(e.message)}")
    return "\n".join(lines)


def system_status_text(status: SystemStatus) -> str:
    agent_line = "🟢 Online" if status.agent_online else "🔴 Offline"
    uptime_h = status.uptime_seconds / 3600
    lines = [
        bold("System Monitor"),
        f"Agent: {agent_line}",
        f"CPU: {esc(f'{status.cpu_percent:.1f}')}\\%",
        f"RAM: {esc(f'{status.ram_percent:.1f}')}\\% "
        f"\\({esc(f'{status.ram_used_gb:.1f}')} / {esc(f'{status.ram_total_gb:.1f}')} GB\\)",
        f"Active projects: {esc(status.active_projects)}",
        f"Running tasks: {esc(status.running_tasks)}",
        f"Queue length: {esc(status.queue_length)}",
        f"Uptime: {esc(f'{uptime_h:.1f}')}h",
        "",
        italic("Auto-refreshes every 5s while open"),
    ]
    return "\n".join(lines)


def file_list_text(project_id: str, current_path: str, entries: List[FileEntry]) -> str:
    header = f"{bold('Files')} — {code(current_path or '/')}"
    if not entries:
        return header + "\n\n" + italic("Empty directory")
    return header


def file_content_text(content: FileContent):
    """Returns (text, lines_shown). lines_shown may be LESS than
    len(content.lines) if fitting within Telegram's 4096-char message limit
    required stopping before the end of the requested page - callers MUST
    use lines_shown (not len(content.lines)) when computing the next page's
    offset, or pagination would silently skip lines that were requested but
    never actually displayed. Found as a real bug during testing: the
    original version truncated the raw text blob independently of the
    pagination bookkeeping, so "line 100" could be silently dropped while
    the Next button still assumed it had been shown and jumped straight to
    line 101 - permanently losing it, not just delaying it to a later page.
    """
    header = (
        f"{bold(content.path)}\n"
        f"{esc(f'{content.size_bytes:,}')} bytes · {esc(content.total_lines)} lines · "
    )
    footer = "\n```"
    fixed_overhead = len(header) + len("showing 1\\-1\n") + len("```\n") + len(footer)
    max_total = 4000  # headroom under Telegram's real 4096-char cap
    available_for_body = max(0, max_total - fixed_overhead)

    shown_lines = []
    body_len = 0
    for line in content.lines:
        needed = len(line) + 1  # +1 for the newline joining it to the next
        if body_len + needed > available_for_body and shown_lines:
            break  # stop before exceeding budget - but always show at least 1 line
        shown_lines.append(line)
        body_len += needed

    header += f"showing {esc(content.offset + 1)}\\-{esc(content.offset + len(shown_lines))}\n"
    body = "\n".join(shown_lines)
    # Inside a pre/code block (triple backtick), only backtick/backslash need
    # escaping - same reasoning as the code() helper, applied manually here
    # since this is a multi-line block, not a single code() call.
    safe_body = body.replace("\\", "\\\\").replace("`", "\\`")
    text = header + "```\n" + safe_body + footer
    return text, len(shown_lines)
