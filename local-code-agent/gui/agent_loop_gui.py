from __future__ import annotations
import json

from agent.ollama_client import OllamaClient, OllamaError
from agent.tools import ToolRegistry, TOOL_SCHEMAS
from agent.memory import ConversationMemory

from . import events


class GuiAgentLoop:
    def __init__(self, client: OllamaClient, tools: ToolRegistry,
                 memory: ConversationMemory, max_iterations: int = 25):
        self.client = client
        self.tools = tools
        self.memory = memory
        self.max_iterations = max_iterations

    def run_turn(self, user_input: str) -> None:
        self.memory.add("user", user_input)
        pending_images: list[str] = []

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
                    elif event["type"] == "done":
                        content = event["content"].strip()
                        tool_calls = event.get("tool_calls") or []
            except OllamaError as e:
                events.push_event({"type": "error", "message": str(e)})
                events.push_event({"type": "turn_complete"})
                return
            pending_images = []

            self.memory.add("assistant", content or "")
            events.push_event({"type": "content_done"})

            if not tool_calls:
                if self.memory.needs_compaction():
                    events.push_event({"type": "status", "message": "Compacting older conversation history to save context..."})
                    self.memory.compact(self.client)
                events.push_event({"type": "turn_complete"})
                return

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}

                if name == "update_plan":
                    events.push_event({"type": "plan_update", "steps": raw_args.get("steps", [])})
                else:
                    events.push_event({"type": "tool_call", "name": name, "args": raw_args})

                result = self.tools.execute(name, raw_args)

                shown = result.text if len(result.text) < 4000 else result.text[:4000] + "\n...(truncated)"
                if name != "update_plan":
                    events.push_event({"type": "tool_result", "name": name, "text": shown})
                self.memory.add("tool", f"[{name}] {shown}")

                if result.image_b64:
                    pending_images.append(result.image_b64)

        events.push_event({"type": "error", "message": "Hit the tool-call safety limit for this turn - ask me to continue if more work is needed."})
        events.push_event({"type": "turn_complete"})
