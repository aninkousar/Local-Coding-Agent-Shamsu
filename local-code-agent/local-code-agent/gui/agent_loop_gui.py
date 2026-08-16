from __future__ import annotations
import json

from agent.ollama_client import OllamaClient, OllamaError
from agent.tools import ToolRegistry, TOOL_SCHEMAS, parse_plan_steps, find_announced_but_uncalled_tool
from agent.context_manager import ContextManager, build_tool_message
from agent.session_log import SessionLogger

from . import events


class GuiAgentLoop:
    def __init__(self, client: OllamaClient, tools: ToolRegistry,
                 memory: ContextManager, max_iterations: int = 25,
                 logger: SessionLogger | None = None):
        self.client = client
        self.tools = tools
        self.memory = memory
        self.max_iterations = max_iterations
        self.logger = logger

    def run_turn(self, user_input: str) -> None:
        if self.logger:
            self.logger.user_message(user_input)
        self.memory.add("user", user_input)
        pending_images: list[str] = []
        already_nudged = False

        for iteration in range(self.max_iterations):
            messages = self.memory.as_chat_messages()
            content = ""
            tool_calls: list[dict] = []
            try:
                for event in self.client.chat_stream(
                    messages, tools=TOOL_SCHEMAS, images_b64=pending_images or None,
                ):
                    if event["type"] == "content":
                        events.push_event({"type": "content_delta", "delta": event["delta"]})
                    elif event["type"] == "context_fallback":
                        events.push_event({
                            "type": "status",
                            "message": f"Ollama failed at {event['from']}-token context (likely memory "
                                       f"pressure) - retrying at {event['to']} tokens for this request "
                                       f"only. Your configured context_window is unchanged.",
                        })
                    elif event["type"] == "done":
                        content = event["content"].strip()
                        tool_calls = event.get("tool_calls") or []
                        if self.logger:
                            self.logger.token_usage(event.get("prompt_tokens"), event.get("completion_tokens"))
            except OllamaError as e:
                events.push_event({"type": "error", "message": str(e)})
                events.push_event({"type": "turn_complete"})
                if self.logger:
                    self.logger.note(f"Model error: {e}")
                return
            pending_images = []

            self.memory.add("assistant", content or "")
            events.push_event({"type": "content_done"})

            if not tool_calls:
                mentioned = find_announced_but_uncalled_tool(content) if content else None
                if mentioned and not already_nudged:
                    already_nudged = True
                    events.push_event({
                        "type": "status",
                        "message": f"(mentioned using {mentioned} but didn't actually call it - nudging to follow through)",
                    })
                    self.memory.add(
                        "user",
                        f"[system_reminder] You mentioned {mentioned} but no matching tool call "
                        f"happened in that turn - nothing was actually read or done yet, even though "
                        f"it may have sounded like it. If you still need to do this, make the actual "
                        f"tool call now, not just a sentence describing it.",
                    )
                    continue
                if self.memory.needs_compaction():
                    events.push_event({"type": "status", "message": "Compacting older conversation history to save context..."})
                    self.memory.compact(self.client)
                events.push_event({"type": "turn_complete"})
                if self.logger:
                    self.logger.assistant_message(content)
                return

            parsed_calls: list[tuple[str, dict]] = []
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}
                parsed_calls.append((name, raw_args))

            if self.tools._batch_is_parallel_safe(parsed_calls):
                events.push_event({
                    "type": "status",
                    "message": f"Running {len(parsed_calls)} read-only tool calls in parallel: "
                               + ", ".join(n for n, _ in parsed_calls),
                })
                for name, raw_args in parsed_calls:
                    if self.logger:
                        self.logger.tool_call(name, raw_args)
                results = self.tools.execute_batch(parsed_calls)
                for (name, raw_args), result in zip(parsed_calls, results):
                    self._record_tool_result(name, raw_args, result, pending_images)
            else:
                for name, raw_args in parsed_calls:
                    if name == "update_plan":
                        events.push_event({"type": "plan_update", "steps": parse_plan_steps(raw_args.get("steps", []))})
                    elif name == "update_scratchpad":
                        events.push_event({"type": "status", "message": "Scratchpad updated."})
                    else:
                        events.push_event({"type": "tool_call", "name": name, "args": raw_args})
                    if self.logger:
                        self.logger.tool_call(name, raw_args)
                    result = self.tools.execute(name, raw_args)
                    self._record_tool_result(name, raw_args, result, pending_images)

        events.push_event({"type": "error", "message": "Hit the tool-call safety limit for this turn - ask me to continue if more work is needed."})
        events.push_event({"type": "turn_complete"})
        if self.logger:
            self.logger.note("Hit the tool-call safety limit for this turn.")

    _FOCUS_TRACKING_TOOLS = {"read_file", "edit_file", "write_file", "read_document", "list_symbols"}

    def _record_tool_result(self, name: str, raw_args: dict, result, pending_images: list[str]) -> None:
        """Shared post-processing for one tool call's result, used by both the
        sequential and parallel execution paths so behavior stays identical
        either way - only the timing of execute() itself differs between them."""
        if name == "update_plan":
            self.memory.update_task_history(self.tools._current_plan)
        elif name == "update_scratchpad":
            self.memory.update_scratchpad(self.tools._scratchpad)
        elif name in self._FOCUS_TRACKING_TOOLS:
            path = raw_args.get("path")
            if path:
                self.memory.set_focus_file(path)
        elif name == "read_files":
            for path in raw_args.get("paths", []) or []:
                self.memory.set_focus_file(path)

        shown = result.text if len(result.text) < 4000 else result.text[:4000] + "\n...(truncated)"
        if name not in ("update_plan", "update_scratchpad"):
            events.push_event({"type": "tool_result", "name": name, "text": shown})
        self.memory.add("tool", build_tool_message(name, raw_args, shown))
        if self.logger:
            self.logger.tool_result(name, result.text)

        if result.image_b64:
            pending_images.append(result.image_b64)
