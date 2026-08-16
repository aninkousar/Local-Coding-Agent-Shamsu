from __future__ import annotations
import json
from rich.console import Console
from rich.panel import Panel

from .ollama_client import OllamaClient, OllamaError
from .tools import ToolRegistry, TOOL_SCHEMAS, parse_plan_steps, find_announced_but_uncalled_tool
from .context_manager import ContextManager, build_tool_message
from .session_log import SessionLogger

console = Console()


def _render_plan(steps: list[dict]) -> None:
    lines = []
    seen_feature = object()  # sentinel - never equals a real feature name or None
    has_any_feature = any(s.get("feature") for s in steps)
    step_num = 0
    for s in steps:
        if has_any_feature:
            feature = s.get("feature")
            if feature != seen_feature:
                seen_feature = feature
                lines.append(f"[bold cyan]{feature or '(ungrouped)'}[/bold cyan]")
        step_num += 1
        desc = s.get("description", "")
        status = s.get("status", "pending")
        prefix = "  " if has_any_feature else ""
        if status == "completed":
            lines.append(f"{prefix}[green]✔[/green] [dim strike]{step_num}. {desc}[/dim strike]")
        elif status == "in_progress":
            lines.append(f"{prefix}[yellow]▶[/yellow] [bold]{step_num}. {desc}[/bold]")
        else:
            lines.append(f"{prefix}[dim]○ {step_num}. {desc}[/dim]")
    console.print(Panel("\n".join(lines), title="Plan", border_style="cyan", expand=False))


class AgentLoop:
    def __init__(self, client: OllamaClient, tools: ToolRegistry,
                 memory: ContextManager, max_iterations: int = 25,
                 logger: SessionLogger | None = None):
        self.client = client
        self.tools = tools
        self.memory = memory
        self.max_iterations = max_iterations
        self.logger = logger

    def run_turn(self, user_input: str) -> str:
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
                        # markup/highlight off: model output often contains [brackets] and
                        # code that Rich would otherwise try to interpret as its own markup
                        console.print(event["delta"], end="", markup=False, highlight=False)
                    elif event["type"] == "context_fallback":
                        console.print(
                            f"\n[yellow](Ollama failed at {event['from']}-token context, likely from "
                            f"memory pressure - automatically retrying at {event['to']} tokens for this "
                            f"request only. Your configured context_window is unchanged. If this keeps "
                            f"happening, see the README section on OLLAMA_KV_CACHE_TYPE.)[/yellow]"
                        )
                    elif event["type"] == "done":
                        content = event["content"].strip()
                        tool_calls = event.get("tool_calls") or []
                        if self.logger:
                            self.logger.token_usage(event.get("prompt_tokens"), event.get("completion_tokens"))
            except OllamaError as e:
                console.print(f"[red]{e}[/red]")
                if self.logger:
                    self.logger.note(f"Model error: {e}")
                return "(local model error - see above)"
            pending_images = []

            if content:
                console.print()  # newline after the streamed text
            self.memory.add("assistant", content or "")

            if not tool_calls:
                mentioned = find_announced_but_uncalled_tool(content) if content else None
                if mentioned and not already_nudged:
                    already_nudged = True
                    console.print(
                        f"[yellow](you mentioned using {mentioned} but didn't actually call it - "
                        f"nudging you to follow through)[/yellow]"
                    )
                    self.memory.add(
                        "user",
                        f"[system_reminder] You mentioned {mentioned} but no matching tool call "
                        f"happened in that turn - nothing was actually read or done yet, even though "
                        f"it may have sounded like it. If you still need to do this, make the actual "
                        f"tool call now, not just a sentence describing it.",
                    )
                    continue
                if self.memory.needs_compaction():
                    console.print("[dim](«compacting older conversation history to save context»)[/dim]")
                    self.memory.compact(self.client)
                if self.logger:
                    self.logger.assistant_message(content)
                return content

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
                names = ", ".join(n for n, _ in parsed_calls)
                console.print(f"[dim]→ running {len(parsed_calls)} read-only tool calls in parallel: {names}[/dim]")
                for name, raw_args in parsed_calls:
                    if self.logger:
                        self.logger.tool_call(name, raw_args)
                results = self.tools.execute_batch(parsed_calls)
                for (name, raw_args), result in zip(parsed_calls, results):
                    self._record_tool_result(name, raw_args, result, pending_images)
            else:
                for name, raw_args in parsed_calls:
                    if name == "update_plan":
                        parsed_steps = parse_plan_steps(raw_args.get("steps", []))
                        _render_plan(parsed_steps)
                    elif name == "update_scratchpad":
                        console.print("[dim]→ scratchpad updated[/dim]")
                    else:
                        console.print(f"[dim]→ tool call: {name}({json.dumps(raw_args)[:200]})[/dim]")
                    if self.logger:
                        self.logger.tool_call(name, raw_args)
                    result = self.tools.execute(name, raw_args)
                    self._record_tool_result(name, raw_args, result, pending_images)

        console.print("[yellow]Hit the tool-call safety limit for this turn - stopping here. "
                       "Ask me to continue if more work is needed.[/yellow]")
        if self.logger:
            self.logger.note("Hit the tool-call safety limit for this turn.")
        return "(stopped: reached max tool iterations for this turn)"

    # Tools that reveal information about one specific project source file -
    # touching one of these sets it as the anchor for dependency-graph-based
    # Project Knowledge retrieval. Deliberately excludes db_schema/db_query
    # (database files, not source files with import relationships) and
    # anything without a clear single-file target (run_command, db_execute).
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
        self.memory.add("tool", build_tool_message(name, raw_args, shown))
        if self.logger:
            self.logger.tool_result(name, result.text)

        if result.image_b64:
            pending_images.append(result.image_b64)
