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
                 enable_thinking: bool = False):
        self.host = host.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.context_window = context_window
        self.temperature = temperature
        self.enable_thinking = enable_thinking

    # -- health -------------------------------------------------------------
    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def has_model(self, name: str) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
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
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.context_window,
            },
        }
        if tools:
            payload["tools"] = tools
        payload["think"] = self.enable_thinking

        try:
            r = requests.post(f"{self.host}/api/chat", json=payload, timeout=600)
            r.raise_for_status()
        except requests.RequestException as e:
            raise OllamaError(
                f"Could not reach local Ollama server at {self.host}. "
                f"Is it running? (`ollama serve`). Details: {e}"
            )
        data = r.json()
        if "message" not in data:
            raise OllamaError(f"Unexpected Ollama response: {data}")
        return data["message"]

    # -- embeddings ------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            try:
                r = requests.post(
                    f"{self.host}/api/embeddings",
                    json={"model": self.embed_model, "prompt": t},
                    timeout=120,
                )
                r.raise_for_status()
                out.append(r.json().get("embedding", []))
            except requests.RequestException as e:
                raise OllamaError(f"Embedding call failed: {e}")
        return out

    @staticmethod
    def image_to_b64(path: Path) -> str:
        return base64.b64encode(Path(path).read_bytes()).decode("utf-8")
