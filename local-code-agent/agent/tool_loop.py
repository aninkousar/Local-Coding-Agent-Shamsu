from __future__ import annotations
import json
from rich.console import Console
from rich.panel import Panel

from .ollama_client import OllamaClient, OllamaError
from .tools import ToolRegistry, TOOL_SCHEMAS
from .memory import ConversationMemory

console = Console()


def _render_plan(steps: list[dict]) -> None:
    lines = []
    for i, s in enumerate(steps, 1):
        desc = s.get("description", "")
        status = s.get("status", "pending")
        if status == "completed":
            lines.append(f"[green]✔[/green] [dim strike]{i}. {desc}[/dim strike]")
        elif status == "in_progress":
            lines.append(f"[yellow]▶[/yellow] [bold]{i}. {desc}[/bold]")
        else:
            lines.append(f"[dim]○ {i}. {desc}[/dim]")
    console.print(Panel("\n".join(lines), title="Plan", border_style="cyan", expand=False))


class AgentLoop:
    def __init__(self, client: OllamaClient, tools: ToolRegistry,
                 memory: ConversationMemory, max_iterations: int = 25):
        self.client = client
        self.tools = tools
        self.memory = memory
        self.max_iterations = max_iterations

    def run_turn(self, user_input: str) -> str:
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
                        # markup/highlight off: model output often contains [brackets] and
                        # code that Rich would otherwise try to interpret as its own markup
                        console.print(event["delta"], end="", markup=False, highlight=False)
                    elif event["type"] == "done":
                        content = event["content"].strip()
                        tool_calls = event.get("tool_calls") or []
            except OllamaError as e:
                console.print(f"[red]{e}[/red]")
                return "(local model error - see above)"
            pending_images = []

            if content:
                console.print()  # newline after the streamed text
            self.memory.add("assistant", content or "")

            if not tool_calls:
                if self.memory.needs_compaction():
                    console.print("[dim](«compacting older conversation history to save context»)[/dim]")
                    self.memory.compact(self.client)
                return content

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
                    _render_plan(raw_args.get("steps", []))
                else:
                    console.print(f"[dim]→ tool call: {name}({json.dumps(raw_args)[:200]})[/dim]")
                result = self.tools.execute(name, raw_args)

                # Feed the tool result back into the conversation
                shown = result.text if len(result.text) < 4000 else result.text[:4000] + "\n...(truncated)"
                self.memory.add("tool", f"[{name}] {shown}")

                if result.image_b64:
                    pending_images.append(result.image_b64)

        console.print("[yellow]Hit the tool-call safety limit for this turn - stopping here. "
                       "Ask me to continue if more work is needed.[/yellow]")
        return "(stopped: reached max tool iterations for this turn)"
