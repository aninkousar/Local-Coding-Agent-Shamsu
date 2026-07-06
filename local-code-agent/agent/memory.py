from __future__ import annotations
from dataclasses import dataclass, field

from .ollama_client import OllamaClient


def _rough_token_count(messages: list[dict]) -> int:
    # ~4 chars/token is a fine enough estimate for budgeting on a local box.
    chars = 0
    for m in messages:
        content = m.get("content") or ""
        chars += len(content)
    return chars // 4


@dataclass
class ConversationMemory:
    system_prompt: str
    soft_limit_tokens: int = 6000
    messages: list[dict] = field(default_factory=list)
    summary: str | None = None

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def as_chat_messages(self) -> list[dict]:
        sys = self.system_prompt
        if self.summary:
            sys += f"\n\nSummary of earlier conversation (for your context, not verbatim history):\n{self.summary}\n"
        return [{"role": "system", "content": sys}] + self.messages

    def needs_compaction(self) -> bool:
        return _rough_token_count(self.messages) > self.soft_limit_tokens

    def compact(self, client: OllamaClient, keep_recent: int = 6) -> None:
        """Summarize everything except the most recent `keep_recent` messages.
        This is what makes 'unlimited' chatting practical on an 8GB CPU box -
        we never let the raw context grow without bound.
        """
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
