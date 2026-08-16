"""WebSocket listener - connects to the agent's WS /events stream and, for
a project the user is actively watching, edits a single Telegram message
continuously rather than sending a new message per event, per the spec's
explicit requirement.

Two real constraints shaped this design, not just "call edit_message_text
on every event":
1. Telegram rate-limits message edits (roughly one edit/second per chat in
   practice) - editing on every single WS event without throttling would
   hit that limit on a fast-moving project. A minimum interval between
   edits is enforced.
2. Telegram rejects an edit whose text is byte-identical to the message's
   current text ("message is not modified") - skipped defensively rather
   than treated as a real error.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Optional

import websockets
from telegram import Bot
from telegram.error import BadRequest

from bot.models.schemas import WSEvent
from bot.services.formatter import code, esc

logger = logging.getLogger(__name__)

_MIN_EDIT_INTERVAL_SECONDS = 1.2  # stays under Telegram's per-chat edit rate limit


class ProjectStreamWatcher:
    """Tracks the (chat_id, message_id) currently displaying live progress
    for one project, and the throttling state for editing it."""

    def __init__(self, chat_id: int, message_id: int) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_edit_time = 0.0
        self.last_text: Optional[str] = None
        self.pending_text: Optional[str] = None


class WebSocketListener:
    """Owns the single persistent connection to the agent's WS /events
    endpoint, and dispatches each event to whichever chat is currently
    watching that project (if any) via throttled message edits.
    """

    def __init__(self, ws_url: str, bot: Bot, reconnect_delay: float = 3.0) -> None:
        self._ws_url = ws_url
        self._bot = bot
        self._reconnect_delay = reconnect_delay
        self._watchers: Dict[str, ProjectStreamWatcher] = {}  # project_id -> watcher
        self._flush_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    def watch(self, project_id: str, chat_id: int, message_id: int) -> None:
        """Start streaming live updates for `project_id` into the given
        message. Replaces any previous watcher for the same project (e.g.
        the user re-opened the project view, so a fresh message is now the
        one to edit)."""
        self._watchers[project_id] = ProjectStreamWatcher(chat_id, message_id)

    def unwatch(self, project_id: str) -> None:
        self._watchers.pop(project_id, None)
        task = self._flush_tasks.pop(project_id, None)
        if task and not task.done():
            task.cancel()

    async def run_forever(self) -> None:
        """Maintains the WS connection, reconnecting with a fixed delay on
        any failure - the agent may not be up yet when the bot starts, or
        may restart while the bot keeps running, and neither should crash
        the bot."""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    logger.info("Connected to agent WebSocket at %s", self._ws_url)
                    async for raw in ws:
                        await self._handle_raw_event(raw)
            except (websockets.exceptions.ConnectionClosed, OSError, ConnectionRefusedError) as e:
                logger.warning("WebSocket connection lost/unavailable (%s) - retrying in %.1fs",
                                e, self._reconnect_delay)
            except Exception:
                logger.exception("Unexpected error in WebSocket listener - retrying in %.1fs",
                                  self._reconnect_delay)
            if self._running:
                await asyncio.sleep(self._reconnect_delay)

    def stop(self) -> None:
        self._running = False

    async def _handle_raw_event(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
            event = WSEvent.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Ignoring malformed WS event: %s", e)
            return

        watcher = self._watchers.get(event.project_id)
        if watcher is None:
            return  # nobody is currently watching this project - nothing to edit

        text = _format_event_text(event)
        await self._schedule_edit(event.project_id, watcher, text)

    async def _schedule_edit(self, project_id: str, watcher: ProjectStreamWatcher, text: str) -> None:
        """Applies the throttle: if the last edit was recent, remember this
        text as `pending_text` and let an already-scheduled flush pick it up
        rather than editing immediately - this coalesces bursts of events
        into the minimum number of actual Telegram API calls."""
        watcher.pending_text = text
        if project_id in self._flush_tasks and not self._flush_tasks[project_id].done():
            return  # a flush is already scheduled, it'll pick up the latest pending_text
        self._flush_tasks[project_id] = asyncio.create_task(self._flush_soon(project_id, watcher))

    async def _flush_soon(self, project_id: str, watcher: ProjectStreamWatcher) -> None:
        elapsed = asyncio.get_event_loop().time() - watcher.last_edit_time
        wait = max(0.0, _MIN_EDIT_INTERVAL_SECONDS - elapsed)
        if wait > 0:
            await asyncio.sleep(wait)

        text = watcher.pending_text
        if text is None or text == watcher.last_text:
            return  # nothing new to show, or Telegram would reject as "not modified"

        try:
            await self._bot.edit_message_text(
                chat_id=watcher.chat_id, message_id=watcher.message_id,
                text=text, parse_mode="MarkdownV2",
            )
            watcher.last_text = text
            watcher.last_edit_time = asyncio.get_event_loop().time()
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                pass  # harmless race - text became identical between the check above and the API call
            else:
                logger.warning("Failed to edit progress message for %s: %s", project_id, e)


def _format_event_text(event: WSEvent) -> str:
    """Builds the live-updating message body from one event - matches the
    spec's example stream style: a short status line per update."""
    lines = [f"⚙️ *Live progress* — {code(event.project_id)}", ""]
    if event.progress is not None:
        filled = round(10 * event.progress / 100)
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(f"{bar} {esc(event.progress)}\\%")
    lines.append(esc(event.message))
    return "\n".join(lines)
