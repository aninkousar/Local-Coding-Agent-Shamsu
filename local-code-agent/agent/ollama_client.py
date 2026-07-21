from __future__ import annotations
import base64
import json
from pathlib import Path
from typing import Any, Iterable

import requests


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    """Talks only to http://localhost:11434 (or wherever your local Ollama lives).
    No other network call exists anywhere in this codebase.
    """

    def __init__(self, host: str, chat_model: str, embed_model: str,
                 context_window: int = 8192, temperature: float = 0.3,
                 enable_thinking: bool = False, keep_alive: str | int = "30m",
                 embed_batch_size: int = 32):
        self.host = host.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.context_window = context_window
        self.temperature = temperature
        self.enable_thinking = enable_thinking
        self.keep_alive = keep_alive
        self.embed_batch_size = embed_batch_size
        # Windows in particular can resolve "localhost" to IPv6 (::1) while Ollama
        # only listens on IPv4 (127.0.0.1), or vice versa - a well-documented cause
        # of connections failing 100% of the time even though Ollama is running fine.
        # We cache whichever address actually answers so we're not double-probing
        # every call once we know which one works.
        self._resolved_host: str | None = None

    def _alt_host(self, host: str) -> str | None:
        if "localhost" in host:
            return host.replace("localhost", "127.0.0.1")
        if "127.0.0.1" in host:
            return host.replace("127.0.0.1", "localhost")
        return None

    def _probe(self, host: str) -> bool:
        try:
            r = requests.get(f"{host}/api/tags", timeout=2)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _get_working_host(self) -> str | None:
        """Returns a host Ollama is actually answering on right now, or None if
        neither the configured address nor its localhost/127.0.0.1 counterpart work."""
        if self._resolved_host and self._probe(self._resolved_host):
            return self._resolved_host
        candidates = [self.host]
        alt = self._alt_host(self.host)
        if alt:
            candidates.append(alt)
        for candidate in candidates:
            if self._probe(candidate):
                self._resolved_host = candidate
                return candidate
        self._resolved_host = None
        return None

    def _unreachable_error(self, detail: str = "") -> OllamaError:
        alt = self._alt_host(self.host)
        tried = f"{self.host}" + (f" and {alt}" if alt else "")
        return OllamaError(
            f"Could not reach a local Ollama server (tried {tried}).\n"
            f"Checklist: (1) is Ollama actually running - not just a closed `ollama run` "
            f"session, but the background service/tray app, or `ollama serve` in its own "
            f"window; (2) on Windows, check Windows Defender Firewall isn't silently "
            f"blocking port 11434 (it doesn't do this by default, but security software "
            f"sometimes adds a rule); (3) check nothing set the OLLAMA_HOST environment "
            f"variable to something unexpected."
            + (f"\nDetails: {detail}" if detail else "")
        )

    # -- health -------------------------------------------------------------
    def ping(self) -> bool:
        return self._get_working_host() is not None

    def has_model(self, name: str) -> bool:
        host = self._get_working_host()
        if not host:
            return False
        try:
            r = requests.get(f"{host}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(n == name or n.startswith(name.split(":")[0]) for n in names)
        except requests.RequestException:
            return False

    # -- chat / tool calling --------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
              images_b64: list[str] | None = None) -> dict:
        """Single non-streaming chat turn. Returns the raw `message` dict from Ollama,
        which may include `content` and/or `tool_calls`.
        """
        host = self._get_working_host()
        if not host:
            raise self._unreachable_error()
        payload = self._build_chat_payload(messages, tools, images_b64, stream=False)
        try:
            r = requests.post(f"{host}/api/chat", json=payload, timeout=600)
            r.raise_for_status()
        except requests.RequestException as e:
            raise self._unreachable_error(str(e))
        data = r.json()
        if "message" not in data:
            raise OllamaError(f"Unexpected Ollama response: {data}")
        return data["message"]

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                     images_b64: list[str] | None = None):
        """Streaming chat turn. Yields dicts as they arrive:
          {"type": "content", "delta": "..."}   - one for each streamed text fragment
          {"type": "done", "content": "...", "tool_calls": [...]}  - once, at the end

        Callers should print each "content" delta live, then use the final "done"
        event's accumulated content/tool_calls exactly like the non-streaming chat().
        """
        host = self._get_working_host()
        if not host:
            raise self._unreachable_error()
        payload = self._build_chat_payload(messages, tools, images_b64, stream=True)
        full_content = []
        tool_calls: list[dict] = []
        try:
            with requests.post(f"{host}/api/chat", json=payload, timeout=600, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message", {}) or {}
                    delta = msg.get("content", "")
                    if delta:
                        full_content.append(delta)
                        yield {"type": "content", "delta": delta}
                    if msg.get("tool_calls"):
                        tool_calls = msg["tool_calls"]
                    if chunk.get("done"):
                        break
        except requests.RequestException as e:
            raise self._unreachable_error(str(e))
        yield {"type": "done", "content": "".join(full_content), "tool_calls": tool_calls}

    def _build_chat_payload(self, messages: list[dict], tools: list[dict] | None,
                             images_b64: list[str] | None, stream: bool) -> dict:
        if images_b64 and messages:
            # attach images to the most recent user message
            last = messages[-1]
            if last.get("role") == "user":
                last = dict(last)
                last["images"] = images_b64
                messages = messages[:-1] + [last]

        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.context_window,
            },
        }
        if tools:
            payload["tools"] = tools
        payload["think"] = self.enable_thinking
        return payload

    # -- embeddings ------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Uses the modern /api/embed endpoint, which batches multiple inputs into a
        single request - one HTTP round-trip per batch instead of one per chunk, which
        matters a lot when indexing a codebase with hundreds of chunks.
        """
        if not texts:
            return []
        host = self._get_working_host()
        if not host:
            raise self._unreachable_error()
        out: list[list[float]] = []
        for i in range(0, len(texts), self.embed_batch_size):
            batch = texts[i:i + self.embed_batch_size]
            try:
                r = requests.post(
                    f"{host}/api/embed",
                    json={"model": self.embed_model, "input": batch, "keep_alive": self.keep_alive},
                    timeout=180,
                )
                r.raise_for_status()
                embeddings = r.json().get("embeddings", [])
            except requests.RequestException as e:
                raise self._unreachable_error(str(e))
            if len(embeddings) != len(batch):
                raise OllamaError(
                    f"Embedding batch mismatch: sent {len(batch)} texts, got {len(embeddings)} vectors back."
                )
            out.extend(embeddings)
        return out

    @staticmethod
    def image_to_b64(path: Path) -> str:
        return base64.b64encode(Path(path).read_bytes()).decode("utf-8")
