from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .ollama_client import OllamaClient
from .tools import format_plan_text, parse_plan_steps
from .indexer import extract_python_symbols


def _rough_token_count(messages: list[dict]) -> int:
    # ~4 chars/token is a fine enough estimate for budgeting on a local box.
    chars = 0
    for m in messages:
        content = m.get("content") or ""
        chars += len(content)
    return chars // 4


def summarize_task_history_for_episode(task_history: list[dict]) -> str:
    """A one-line summary of the current plan state for the episode log - built
    from already-structured data (no LLM call needed, unlike a real summary
    would require), so it's cheap enough to always run at session end."""
    if not task_history:
        return "Session ended with no active plan."
    completed = sum(1 for s in task_history if s.get("status") == "completed")
    total = len(task_history)
    in_progress = next((s["description"] for s in task_history if s.get("status") == "in_progress"), None)
    last_bit = f" - was working on: {in_progress}" if in_progress else ""
    return f"Plan: {completed}/{total} steps done{last_bit}"


def build_tool_message(name: str, args: dict, shown: str) -> str:
    """Formats a tool result for storage, tagging it with an identifiable key (the
    file path, query, etc.) extracted from its arguments - e.g. "[read_file|app.py]
    ...". This is what lets the Context Selector later recognize "this is the third
    time app.py was read" and deduplicate, which it can't do from the result text
    alone (read_file's own output never repeats the path it read).
    """
    key = ""
    for key_field in ("path", "paths", "query", "sql", "db_path", "sql_file"):
        val = args.get(key_field)
        if val:
            key = ",".join(val) if isinstance(val, list) else str(val)
            break
    tag = f"{name}|{key}" if key else name
    return f"[{tag}] {shown}"


# Only these are deduplicated - tools where re-running with the same target is a
# genuinely common, wasteful pattern (repeated reads of a file that hasn't
# changed). Write/execute-type tools are deliberately excluded: seeing that a file
# was edited or a command was run twice is meaningful history, not redundant, and
# collapsing it could hide something the user actually needs to see.
_DEDUP_TOOL_NAMES = {
    "read_file", "read_document", "read_files", "list_symbols", "list_directory",
    "db_schema", "search_codebase", "grep_codebase",
}
_TOOL_TAG_RE = re.compile(r"^\[([\w]+)(?:\|([^\]]*))?\]")


def _dedupe_superseded_reads(messages: list[dict]) -> list[dict]:
    """Context Selector optimization: if the same read-type tool was called with
    the same target more than once, only the LAST occurrence's full content is
    still likely to matter - earlier ones are almost always pure waste (either
    identical, since the file didn't change, or superseded, since it did). Replaces
    earlier duplicates with a short placeholder. Operates only on what's SENT to
    the model this turn - `self.messages` itself is left untouched, so compaction
    and the true conversation record are unaffected by this.
    """
    last_index: dict[tuple[str, str], int] = {}
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        match = _TOOL_TAG_RE.match(m.get("content") or "")
        if not match:
            continue
        name, key = match.group(1), match.group(2) or ""
        if name in _DEDUP_TOOL_NAMES:
            last_index[(name, key)] = i

    result = []
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            result.append(m)
            continue
        match = _TOOL_TAG_RE.match(m.get("content") or "")
        if not match:
            result.append(m)
            continue
        name, key = match.group(1), match.group(2) or ""
        if name in _DEDUP_TOOL_NAMES and last_index.get((name, key)) != i:
            label = f"{name}({key})" if key else name
            result.append({
                "role": "tool",
                "content": f"[{label}] (superseded by a more recent, identical call - see further down for the current content)",
            })
        else:
            result.append(m)
    return result


@dataclass
class SelectedContext:
    """What the Context Selector decided to include for this turn - a plain,
    inspectable structure so build_prompt() (and tests) can see exactly what was
    chosen without re-deriving it."""
    recent_chat: list[dict]
    long_term_memory: str | None
    task_history: str | None
    scratchpad: str | None
    file_index: str | None
    project_knowledge: str | None
    episodic_memory: str | None


@dataclass
class ContextManager:
    """
    User -> Conversation Manager -> {Recent Chat, Long-term Memory, Project
    Knowledge, File Index, Task History, Scratchpad, Episodic Memory} ->
    Context Selector -> Prompt Builder -> LLM.

    Each store below is separate, independently-updated state. select_context()
    is the Context Selector: it decides what's relevant for THIS turn from each
    store. build_prompt() is the Prompt Builder: it's a pure formatting step that
    assembles the final message list from whatever was selected - no relevance
    decisions happen there anymore, only presentation.

    This exists because "keep appending to one big message list until it's too
    big, then summarize" (the old approach) treats everything as equally
    important. It isn't: file structure and the current plan are cheap and
    always worth including; a summary of turns from 20 minutes ago usually
    isn't; and which parts of the actual codebase are relevant changes with
    every single message, so that's the one store that needs a fresh relevance
    decision each turn rather than being dumped in wholesale.
    """
    system_prompt_base: str
    soft_limit_tokens: int = 6000

    # -- Recent Chat -----------------------------------------------------------
    messages: list[dict] = field(default_factory=list)

    # -- Long-term Memory (rolling summary of chat that's been compacted away) --
    # Adaptive Compression, part 1: the summary itself is now bounded. Found
    # while building #37 (later rolled back): compact() concatenated each new
    # summary onto the old one with no cap, so it could grow unboundedly across
    # a long session with many compactions. #37 fixed this by wiping the WHOLE
    # conversation; this is the more surgical version - just re-compress the
    # summary itself once it crosses summary_max_chars, via one more (infrequent)
    # model call, rather than resetting anything else.
    summary: str | None = None
    summary_max_chars: int = 1500

    # Adaptive Compression, part 2: a middle tier between "full detail" (the
    # most recent messages) and "fully summarized" (compact(), once triggered).
    # Tool results specifically - usually the largest individual messages, e.g.
    # a big file read - get progressively more aggressively truncated the
    # older they get within the active window, before eventually being
    # compacted away entirely. Pure deterministic truncation, no extra model
    # calls - smooths what used to be a single sharp cliff.
    full_detail_recent_count: int = 4
    light_compression_recent_count: int = 10
    light_compression_max_chars: int = 1500
    heavy_compression_max_chars: int = 500

    # -- Task History (mirrors the current update_plan state) -------------------
    task_history: list[dict] = field(default_factory=list)
    # Persisted continuously to disk on every update, loaded once at startup -
    # unlike #37 (rolled back), this has NO reset/wipe mechanism attached at
    # all. It exists purely so a process restart (crash, update, closing the
    # terminal) doesn't lose an in-progress plan, the same way scratchpad and
    # episode-log persistence already survive a restart.
    plan_persist_path: Path | None = None

    # -- Scratchpad (freeform notes the agent writes for itself, e.g. "the DB
    # connection env var is DATABASE_URL" - survives compaction verbatim, unlike
    # chat history, which gets lossily summarized) -----------------------------
    scratchpad: list[str] = field(default_factory=list)

    # -- File Index (cheap, cached project structure listing - not content) -----
    file_index_text: str | None = None

    # -- Project Knowledge, now dependency-graph-based instead of semantic RAG.
    # Replaces the old CodebaseIndex-driven auto-retrieval with pure static
    # analysis: given the file(s) actually being worked on this session (the
    # "focus files"), walk the dependency graph outward and surface directly
    # related files' content - no embedding model call needed at all.
    #
    # Honest limitation, stated plainly rather than glossed over: this needs an
    # anchor. Semantic search could surface relevant code for a brand-new,
    # open-ended question before any file had been touched ("how do I
    # calculate the total price" matching a function by meaning alone); this
    # cannot - it has nothing to traverse from until at least one file has
    # actually been read/edited/written this session. That's a deliberate
    # tradeoff of this replacement, not an oversight.
    project_root: Path | None = None
    dependency_forward: dict[str, set[str]] | None = None
    dependency_reverse: dict[str, set[str]] | None = None
    focus_files: list[str] = field(default_factory=list)
    focus_files_max: int = 2
    dependency_context_max_files: int = 4
    dependency_context_max_chars_per_file: int = 500

    # -- Context Budget Manager: task history/scratchpad/episodic memory are
    # small and high-priority, included in full. File Index and Project
    # Knowledge are more flexible - rather than each having its own fixed cap
    # regardless of what else is using space, they split whatever's left of
    # this total budget, proportionally shrinking if the high-priority stores
    # are unexpectedly large this turn instead of just always maxing out. -----
    context_overhead_budget_tokens: int = 2000

    # -- Episodic Memory (spans SEPARATE sessions - distinct from everything
    # above, which is scoped to one running process). Two parts: the scratchpad
    # persisted to disk and reloaded at the start of a new session (so a fact
    # learned last week is still known today), and a rolling log of brief,
    # one-line summaries of what happened in past sessions on this project. -----
    scratchpad_persist_path: Path | None = None
    episode_log_path: Path | None = None
    episode_log: list[str] = field(default_factory=list)
    episode_log_max_shown: int = 5

    # === public API used by the CLI/GUI agent loops ============================

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def needs_compaction(self) -> bool:
        return _rough_token_count(self.messages) > self.soft_limit_tokens

    def compact(self, client: OllamaClient, keep_recent: int = 6) -> None:
        """Summarize everything except the most recent `keep_recent` messages.
        This is what makes long sessions practical without the raw context
        growing without bound - task history, scratchpad, and file index are
        untouched by this, since they're small and don't need summarizing."""
        if len(self.messages) <= keep_recent:
            return
        old = self.messages[:-keep_recent]
        recent = self.messages[-keep_recent:]

        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in old if m.get("content"))
        prompt = (
            "Summarize this coding-session conversation history in under 200 words. "
            "Preserve: what the project is, what's been built/changed so far, open decisions, "
            "and anything the user explicitly asked to remember. Be concrete, not vague.\n\n"
            f"{transcript}"
        )
        reply = client.chat([{"role": "user", "content": prompt}])
        new_summary = reply.get("content", "").strip()

        if self.summary:
            self.summary = f"{self.summary}\n{new_summary}"
        else:
            self.summary = new_summary
        self.messages = recent

        # Adaptive Compression: keep the summary itself bounded rather than
        # letting it grow indefinitely across many compactions in a long
        # session - see the field comment above for why this exists.
        if len(self.summary) > self.summary_max_chars:
            self.summary = self._recompress_summary(client)

    def _recompress_summary(self, client: OllamaClient) -> str:
        """Condenses an over-grown summary into itself - one more model call,
        but an infrequent one (only when the summary crosses summary_max_chars,
        not on every compaction), in exchange for keeping Long-term Memory from
        growing without bound across a very long session."""
        prompt = (
            "The following is a summary of a coding session's earlier history that has "
            "grown too large. Condense it into under 150 words, keeping only what's "
            "still likely to matter: what the project is, key decisions made, and "
            f"anything explicitly asked to be remembered. Be concrete, not vague.\n\n{self.summary}"
        )
        try:
            reply = client.chat([{"role": "user", "content": prompt}])
            condensed = reply.get("content", "").strip()
            return condensed or self.summary
        except Exception:
            return self.summary  # never let a re-compression hiccup lose the existing summary

    def update_task_history(self, steps: list[dict]) -> None:
        self.task_history = steps
        self._persist_plan()

    def _persist_plan(self) -> None:
        if not self.plan_persist_path:
            return
        try:
            self.plan_persist_path.parent.mkdir(parents=True, exist_ok=True)
            # Deliberately NOT format_plan_text() here - that bakes in step
            # numbers and indentation for human-readable display, which would
            # NOT round-trip cleanly back through parse_plan_steps(). This is
            # the same raw "marker + description" / "## Feature" line format
            # update_plan itself accepts, so loading it back is exact.
            lines = []
            current_feature = object()  # sentinel - never equals a real feature name or None
            for s in self.task_history:
                feature = s.get("feature")
                if feature != current_feature:
                    current_feature = feature
                    if feature:
                        lines.append(f"## {feature}")
                marker = {"completed": "[x]", "in_progress": "[~]"}.get(s.get("status"), "[ ]")
                lines.append(f"{marker} {s.get('description', '')}")
            body = "\n".join(lines) or "(no active plan)"
            self.plan_persist_path.write_text(
                "# Task plan (auto-saved continuously - survives a process restart, no reset behavior)\n\n"
                + body + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # persistence failing should never break the actual session

    def load_persisted_plan(self) -> None:
        """Call once at the start of a session: restores an in-progress plan
        from a previous session that ended (or crashed) mid-task, so a process
        restart doesn't lose track of where things stood. Purely additive -
        no reset or wipe behavior attached, unlike the rolled-back #37."""
        if not self.plan_persist_path or not self.plan_persist_path.exists():
            return
        try:
            text = self.plan_persist_path.read_text(encoding="utf-8")
            raw_lines = []
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("##"):
                    raw_lines.append(line)          # a feature-group header - keep
                elif not stripped.startswith("#"):
                    raw_lines.append(line)          # a real step line - keep
                # else: a plain '#' comment line (the auto-saved file header) - skip
            if raw_lines and raw_lines != ["(no active plan)"]:
                self.task_history = parse_plan_steps(raw_lines)
        except OSError:
            pass

    def update_scratchpad(self, notes: list[str]) -> None:
        self.scratchpad = [n.strip() for n in notes if n and n.strip()]
        self._persist_scratchpad()

    def _persist_scratchpad(self) -> None:
        if not self.scratchpad_persist_path:
            return
        try:
            self.scratchpad_persist_path.parent.mkdir(parents=True, exist_ok=True)
            body = "\n".join(f"- {n}" for n in self.scratchpad) or "(empty)"
            self.scratchpad_persist_path.write_text(
                "# Scratchpad (auto-saved - persists across sessions)\n\n" + body + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # persistence failing should never break the actual session

    def load_episodic_memory(self) -> None:
        """Call once at the start of a session: pre-populates the scratchpad from
        a prior session's saved copy (so a fact learned last week is still known
        today), and loads the most recent entries from the rolling episode log
        (brief one-line summaries of what happened in past sessions)."""
        if self.scratchpad_persist_path and self.scratchpad_persist_path.exists():
            try:
                text = self.scratchpad_persist_path.read_text(encoding="utf-8")
                notes = [line[2:].strip() for line in text.splitlines()
                         if line.startswith("- ") and line[2:].strip() != "(empty)"]
                self.scratchpad = notes
            except OSError:
                pass
        if self.episode_log_path and self.episode_log_path.exists():
            try:
                lines = [l for l in self.episode_log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.episode_log = lines[-self.episode_log_max_shown:]
            except OSError:
                pass

    def record_episode(self, summary: str) -> None:
        """Call once at the end of a session: appends a brief, one-line summary
        to the rolling episode log, so the NEXT session (a separate process,
        possibly days later) has a short history of recent work on this project
        without needing the full session log."""
        if not self.episode_log_path:
            return
        try:
            self.episode_log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(self.episode_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {summary}\n")
        except OSError:
            pass

    def set_file_index(self, text: str) -> None:
        self.file_index_text = text

    # === Context Selector ========================================================

    def select_context(self) -> SelectedContext:
        """Recent chat, long-term memory, task history, scratchpad, and file index
        are all cheap and small by construction, so they're always included as-is
        (recent chat also gets superseded-read deduplication and graduated
        tool-result compression - see _dedupe_superseded_reads and
        _apply_graduated_compression). Project knowledge is different - it's
        queried fresh against the current user message every turn, since which
        part of a codebase is relevant changes with every message and a whole
        codebase can't fit regardless."""
        recent_chat = self._apply_graduated_compression(_dedupe_superseded_reads(self.messages))
        return SelectedContext(
            recent_chat=recent_chat,
            long_term_memory=self.summary,
            task_history=self._format_task_history(),
            scratchpad=self._format_scratchpad(),
            file_index=self.file_index_text,
            project_knowledge=self._dependency_context(),
            episodic_memory=self._format_episode_log(),
        )

    def _format_episode_log(self) -> str | None:
        if not self.episode_log:
            return None
        return "\n".join(self.episode_log[-self.episode_log_max_shown:])

    def _apply_graduated_compression(self, messages: list[dict]) -> list[dict]:
        """Adaptive Compression, part 2: a middle tier between full detail and
        compact()'s full summarization. Tool-role messages specifically (the
        usual size culprit - a big file read, a large search result) get
        progressively smaller truncation caps the older they are within the
        active window: full detail for the most recent few, a moderate cap
        for the next tier, an aggressive cap beyond that - rather than the old
        binary choice of full detail or nothing. Doesn't touch self.messages
        itself, only what's assembled for this turn - same pattern as
        _dedupe_superseded_reads.
        """
        n = len(messages)
        result = []
        for i, m in enumerate(messages):
            if m.get("role") != "tool":
                result.append(m)
                continue
            age_from_end = n - i  # 1 = most recent
            content = m.get("content") or ""

            if age_from_end <= self.full_detail_recent_count:
                result.append(m)
                continue
            elif age_from_end <= self.light_compression_recent_count:
                max_chars = self.light_compression_max_chars
            else:
                max_chars = self.heavy_compression_max_chars

            if len(content) > max_chars:
                new_m = dict(m)
                new_m["content"] = content[:max_chars] + \
                    "\n...(older tool result trimmed further to save space - re-run the tool if full detail is needed again)"
                result.append(new_m)
            else:
                result.append(m)
        return result

    def _format_task_history(self) -> str | None:
        if not self.task_history:
            return None
        return format_plan_text(self.task_history)

    def _format_scratchpad(self) -> str | None:
        if not self.scratchpad:
            return None
        return "\n".join(f"- {n}" for n in self.scratchpad)

    def set_focus_file(self, path: str) -> None:
        """Called after any tool touches a specific file (read/edit/write/list_symbols)
        - tracks a small rolling window of recently-touched files as the anchor
        for dependency-graph-based context retrieval. This is what replaced
        semantic search's job of "what's relevant right now" - instead of asking
        what's semantically similar to the last message, it asks what's
        structurally connected to what's actually being worked on.
        """
        if not path:
            return
        if path in self.focus_files:
            self.focus_files.remove(path)
        self.focus_files.insert(0, path)
        self.focus_files = self.focus_files[:self.focus_files_max]

    def _dependency_context(self) -> str | None:
        """Project Knowledge, dependency-graph-based: given the focus file(s) -
        whatever's actually been read/edited/written this session - walks the
        dependency graph outward (what they import, what imports them) and
        surfaces each related file's content, up to a bounded preview length.
        Pure static analysis, no embedding model call at all.

        Returns None with no anchor established yet (nothing touched this
        session) or no dependency graph built yet (reindex hasn't run) - this
        is the honest limitation of a graph-based approach: it has nothing to
        offer for a brand-new, open-ended question before some file is already
        in view, unlike semantic search, which could match by meaning alone.
        """
        if not self.focus_files or not self.project_root:
            return None
        if not (self.dependency_forward or self.dependency_reverse):
            return None

        related: dict[str, str] = {}
        for focus in self.focus_files:
            for dep in (self.dependency_forward or {}).get(focus, set()):
                related.setdefault(dep, f"imported by {focus}")
            for dep in (self.dependency_reverse or {}).get(focus, set()):
                related.setdefault(dep, f"imports {focus}")
        # never show a focus file as "related to itself"
        for focus in self.focus_files:
            related.pop(focus, None)
        if not related:
            return None

        blocks = []
        for rel_path, relation in list(related.items())[:self.dependency_context_max_files]:
            file_path = self.project_root / rel_path
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            symbol_map = extract_python_symbols(content) if rel_path.endswith(".py") else None
            if symbol_map is not None:
                # A symbol map instead of a truncated raw-content dump: more
                # informative (the whole file's shape, not an arbitrary
                # character-count prefix that could cut off mid-function) and
                # typically more compact. get_symbol can fetch one specific
                # function/class's exact body if it turns out to matter.
                lines = [f"({relation})"]
                if symbol_map["summary"]:
                    lines.append(symbol_map["summary"])
                for ln, label in symbol_map["entries"]:
                    lines.append(f"  {ln}: {label}")
                for ln, name in symbol_map["variables"]:
                    lines.append(f"  {ln}: {name}")
                if not symbol_map["entries"] and not symbol_map["variables"]:
                    lines.append("(no functions, classes, or module-level variables)")
                blocks.append(f"--- {rel_path} ---\n" + "\n".join(lines))
            else:
                # Non-Python, or Python that failed to parse (a syntax error) -
                # no AST support here for other languages, fall back to the
                # previous truncated raw-content approach.
                if len(content) > self.dependency_context_max_chars_per_file:
                    content = content[:self.dependency_context_max_chars_per_file] + \
                        "\n... (truncated - use read_file for the rest)"
                blocks.append(f"--- {rel_path} ({relation}) ---\n{content}")

        return "\n\n".join(blocks) if blocks else None

    # === Prompt Builder ===========================================================

    def build_prompt(self, selected: SelectedContext) -> list[dict]:
        """Pure formatting: assembles the final message list from whatever the
        Context Selector already decided to include. No relevance decisions here -
        EXCEPT for the Context Budget Manager step below, which is about
        proportioning space, not deciding relevance, so it stays here rather
        than in select_context()."""
        sys = self.system_prompt_base

        # Context Budget Manager: task history, scratchpad, and episodic memory
        # are small and high-priority, so they're always included in full. File
        # Index and Project Knowledge are the flexible stores - rather than each
        # having its own fixed cap regardless of what else is using space, they
        # split whatever's left of one shared budget, shrinking proportionally
        # if the high-priority stores are unexpectedly large this turn.
        fixed_chars = sum(len(t or "") for t in
                          (selected.task_history, selected.scratchpad, selected.episodic_memory))
        total_budget_chars = self.context_overhead_budget_tokens * 4
        remaining_chars = max(200, total_budget_chars - fixed_chars)
        file_index_budget = int(remaining_chars * 0.4)
        project_knowledge_budget = remaining_chars - file_index_budget

        file_index_text = selected.file_index
        if file_index_text and len(file_index_text) > file_index_budget:
            file_index_text = file_index_text[:file_index_budget] + \
                "\n... (trimmed to fit this turn's context budget - use list_directory for the rest)"

        project_knowledge_text = selected.project_knowledge
        if project_knowledge_text and len(project_knowledge_text) > project_knowledge_budget:
            project_knowledge_text = project_knowledge_text[:project_knowledge_budget] + \
                "\n... (trimmed to fit this turn's context budget)"

        # Prompt Compiler: short, structured labels instead of full explanatory
        # sentences repeated every single turn. Safe to do because every one of
        # these stores is already explained once in the persistent system
        # prompt ("Your context already includes, automatically: a file
        # index..." etc.) - repeating that explanation here on every turn was
        # pure redundant cost, not added clarity. Nothing about WHAT
        # information is included changes, only how tersely it's labeled.
        if file_index_text:
            sys += f"\n\nFILES:\n{file_index_text}\n"
        if project_knowledge_text:
            sys += f"\n\nRELATED:\n{project_knowledge_text}\n"
        if selected.task_history:
            sys += f"\n\nPLAN:\n{selected.task_history}\n"
        if selected.scratchpad:
            sys += f"\n\nNOTES:\n{selected.scratchpad}\n"
        if selected.episodic_memory:
            sys += f"\n\nPAST SESSIONS:\n{selected.episodic_memory}\n"
        if selected.long_term_memory:
            sys += f"\n\nSUMMARY:\n{selected.long_term_memory}\n"

        return [{"role": "system", "content": sys}] + selected.recent_chat

    def as_chat_messages(self) -> list[dict]:
        """Context Selector + Prompt Builder in one call, for callers that don't
        need the intermediate SelectedContext for anything else - this is what
        the CLI/GUI agent loops actually call each turn."""
        return self.build_prompt(self.select_context())
