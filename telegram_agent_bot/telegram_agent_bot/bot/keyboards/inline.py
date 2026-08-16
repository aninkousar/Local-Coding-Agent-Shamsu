"""Inline keyboard builders - every screen in this bot uses inline keyboards
only, per the UX requirement. Centralized here so callback_data formats are
defined in exactly one place rather than scattered across handler files.

callback_data convention: "namespace:action:arg" (colon-separated, short -
Telegram caps callback_data at 64 bytes). Handlers parse this same format
in bot/handlers/*.py's CallbackQueryHandler pattern matching.
"""
from __future__ import annotations

from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models.schemas import Project


def home_menu() -> InlineKeyboardMarkup:
    """The /start home menu - matches the spec's button table exactly."""
    rows = [
        [InlineKeyboardButton("📝 New Prompt", callback_data="home:new_prompt")],
        [InlineKeyboardButton("📁 Projects", callback_data="home:projects")],
        [InlineKeyboardButton("⚙️ Active Tasks", callback_data="home:active_tasks")],
        [InlineKeyboardButton("📊 Project Status", callback_data="home:project_status")],
        [InlineKeyboardButton("📜 Logs", callback_data="home:logs")],
        [InlineKeyboardButton("🖥 System", callback_data="home:system")],
    ]
    return InlineKeyboardMarkup(rows)


def back_to_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home:root")]])


def project_list(projects: List[Project], page: int = 0, page_size: int = 5) -> InlineKeyboardMarkup:
    """One button per project (as a card summary line), plus pagination if
    there are more projects than fit on one page."""
    start = page * page_size
    page_items = projects[start:start + page_size]

    rows = [
        [InlineKeyboardButton(f"{p.status.emoji} {p.name} ({p.progress}%)",
                               callback_data=f"project:view:{p.id}")]
        for p in page_items
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"project:list_page:{page - 1}"))
    if start + page_size < len(projects):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"project:list_page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🏠 Home", callback_data="home:root")])
    return InlineKeyboardMarkup(rows)


def project_detail(project: Project) -> InlineKeyboardMarkup:
    """The Project Detail action buttons, per the spec: Pause / Resume /
    Stop / Show Files / Open Logs / Send Follow-up Prompt. Pause is hidden
    once already paused (Resume shown instead), and controls are hidden
    entirely once a project has reached a terminal state."""
    rows = []
    if project.status.value == "running":
        rows.append([
            InlineKeyboardButton("⏸ Pause", callback_data=f"project:pause:{project.id}"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"project:stop:{project.id}"),
        ])
    elif project.status.value == "paused":
        rows.append([
            InlineKeyboardButton("▶️ Resume", callback_data=f"project:resume:{project.id}"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"project:stop:{project.id}"),
        ])

    rows.append([
        InlineKeyboardButton("📂 Show Files", callback_data=f"project:files:{project.id}:"),
        InlineKeyboardButton("📜 Open Logs", callback_data=f"project:logs:{project.id}:0"),
    ])
    rows.append([InlineKeyboardButton("💬 Send Follow-up Prompt", callback_data=f"project:followup:{project.id}")])
    rows.append([
        InlineKeyboardButton("⬅️ Back to Projects", callback_data="project:list_page:0"),
        InlineKeyboardButton("🏠 Home", callback_data="home:root"),
    ])
    return InlineKeyboardMarkup(rows)


def logs_pagination(project_id: str, page: int, has_more: bool) -> InlineKeyboardMarkup:
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"project:logs:{project_id}:{page - 1}"))
    if has_more:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"project:logs:{project_id}:{page + 1}"))
    rows = [nav_row] if nav_row else []
    rows.append([InlineKeyboardButton("⬅️ Back to Project", callback_data=f"project:view:{project_id}")])
    return InlineKeyboardMarkup(rows)


def file_browser(project_id: str, current_path: str, entries: list) -> InlineKeyboardMarkup:
    """One button per file/folder entry. Folders navigate deeper (path
    grows); files open file_content. A "Up" button appears once inside a
    subdirectory."""
    rows = []
    for entry in entries:
        label = f"📁 {entry.path}" if entry.is_dir else f"📄 {entry.path}"
        full_path = f"{current_path}{entry.path}" if current_path else entry.path
        rows.append([InlineKeyboardButton(label, callback_data=f"file:browse:{project_id}:{full_path}")])

    if current_path:
        parent = "/".join(current_path.rstrip("/").split("/")[:-1])
        parent = f"{parent}/" if parent else ""
        rows.append([InlineKeyboardButton("⬆️ Up", callback_data=f"project:files:{project_id}:{parent}")])

    rows.append([InlineKeyboardButton("⬅️ Back to Project", callback_data=f"project:view:{project_id}")])
    return InlineKeyboardMarkup(rows)


def file_content_pagination(project_id: str, path: str, offset: int, lines_shown_this_page: int,
                             total_lines: int) -> InlineKeyboardMarkup:
    """lines_shown_this_page is the ACTUAL number of lines that fit on the
    current page (from formatter.file_content_text's return), not a
    requested page size - large files with long lines can show fewer lines
    per page than a fixed request would assume, and the Next button must
    reflect what was really displayed or it would silently skip lines.

    Known, honest limitation: the Prev button still assumes the PREVIOUS
    page showed the same number of lines as the current one, since that
    information isn't available here (each page is rendered independently,
    with no memory of how many lines an earlier page actually fit). This is
    usually accurate in practice, since most files have fairly consistent
    line lengths throughout - but isn't guaranteed for a file with a few
    unusually long lines concentrated on one earlier page.
    """
    nav_row = []
    if offset > 0:
        prev_offset = max(0, offset - lines_shown_this_page)
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"file:content:{project_id}:{path}:{prev_offset}"))
    if offset + lines_shown_this_page < total_lines:
        next_offset = offset + lines_shown_this_page
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"file:content:{project_id}:{path}:{next_offset}"))
    rows = [nav_row] if nav_row else []
    parent_dir = "/".join(path.rstrip("/").split("/")[:-1])
    parent_dir = f"{parent_dir}/" if parent_dir else ""
    rows.append([InlineKeyboardButton("⬅️ Back to Files", callback_data=f"project:files:{project_id}:{parent_dir}")])
    return InlineKeyboardMarkup(rows)


def system_status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="home:system")],
        [InlineKeyboardButton("🏠 Home", callback_data="home:root")],
    ])


def confirm_cancel(confirm_callback: str, cancel_callback: str = "home:root") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=confirm_callback),
        InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback),
    ]])
