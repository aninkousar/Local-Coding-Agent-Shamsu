from __future__ import annotations
import base64
import json
from pathlib import Path
from typing import Any

import requests


class OllamaError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OllamaClient:
    """Talks only to http://localhost:11434 (or wherever your local Ollama lives).
    The only other network-capable code in this project is check_local_server
    (agent/tools.py, refuses anything not on localhost/127.0.0.1) and db_tools.py's
    optional Postgres/MySQL support - both require explicit per-call user approval.
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
        # Token Cost Optimizer: real counts from Ollama's own response
        # (prompt_eval_count/eval_count), which it already computes on every
        # call - this replaces guessing at cost with what actually happened,
        # for whichever caller wants to log or inspect it after a call.
        self.last_prompt_tokens: int | None = None
        self.last_completion_tokens: int | None = None
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

    def _request_error(self, e: requests.RequestException) -> OllamaError:
        """Distinguishes genuine unreachability (connection refused, timeout, DNS
        failure - Ollama isn't there to answer at all) from an HTTP error response
        (Ollama responded, meaning it IS running and reachable, but the request
        itself failed on its end) - conflating these under one "could not reach"
        message is actively misleading, since the fix for each is completely
        different."""
        response = getattr(e, "response", None)
        if response is None:
            return self._unreachable_error(str(e))

        status = response.status_code
        try:
            body_snippet = (response.text or "")[:300]
        except Exception:
            body_snippet = ""
        url = getattr(response, "url", "") or (getattr(e.request, "url", "") if e.request else "")

        if status >= 500:
            return OllamaError(
                f"Ollama responded but failed with a server error (HTTP {status}) - it IS "
                f"running and reachable, the request itself is what failed on Ollama's side, "
                f"not a connectivity problem. Commonly caused by running out of memory while "
                f"processing a large context (check context_window in config.yaml against "
                f"your machine's actual available RAM - this is the first thing to suspect if "
                f"it happens on longer conversations specifically) or a crash inside Ollama "
                f"itself. Check the terminal/window where Ollama is running for the real "
                f"underlying error - this client can only see that the request failed, not why.\n"
                f"Details: HTTP {status} for {url}"
                + (f"\nOllama's response: {body_snippet}" if body_snippet else ""),
                status_code=status,
            )
        return OllamaError(
            f"Ollama responded with an unexpected error (HTTP {status}) for {url}. "
            f"Details: {body_snippet or str(e)}",
            status_code=status,
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
              images_b64: list[str] | None = None, _ctx_override: int | None = None) -> dict:
        """Single non-streaming chat turn. Returns the raw `message` dict from Ollama,
        which may include `content` and/or `tool_calls`. Has the same automatic
        one-level context fallback as chat_stream() - see its docstring."""
        host = self._get_working_host()
        if not host:
            raise self._unreachable_error()
        effective_ctx = _ctx_override if _ctx_override is not None else self.context_window
        payload = self._build_chat_payload(messages, tools, images_b64, stream=False, ctx_override=effective_ctx)
        try:
            r = requests.post(f"{host}/api/chat", json=payload, timeout=1800)
            r.raise_for_status()
        except requests.RequestException as e:
            err = self._request_error(e)
            if (_ctx_override is None and err.status_code is not None
                    and err.status_code >= 500 and effective_ctx > 2048):
                fallback_ctx = max(2048, effective_ctx // 2)
                return self.chat(messages, tools, images_b64, _ctx_override=fallback_ctx)
            raise err
        data = r.json()
        if "message" not in data:
            raise OllamaError(f"Unexpected Ollama response: {data}")
        self.last_prompt_tokens = data.get("prompt_eval_count")
        self.last_completion_tokens = data.get("eval_count")
        return data["message"]

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                     images_b64: list[str] | None = None, _ctx_override: int | None = None):
        """Streaming chat turn. Yields dicts as they arrive:
          {"type": "content", "delta": "..."}   - one for each streamed text fragment
          {"type": "context_fallback", "from": N, "to": M}  - emitted once, only if a
              5xx error struck before any content came back, and this transparently
              retries the SAME request with a smaller num_ctx - self.context_window
              (what you configured) is never touched by this, only this one call
          {"type": "done", "content": "...", "tool_calls": [...], "prompt_tokens": N, "completion_tokens": M}
              - once, at the end. prompt_tokens/completion_tokens are Ollama's own real
              counts (None if unavailable) - use these over any char-based estimate.

        Callers should print each "content" delta live, then use the final "done"
        event's accumulated content/tool_calls exactly like the non-streaming chat().
        """
        host = self._get_working_host()
        if not host:
            raise self._unreachable_error()
        effective_ctx = _ctx_override if _ctx_override is not None else self.context_window
        payload = self._build_chat_payload(messages, tools, images_b64, stream=True, ctx_override=effective_ctx)
        full_content = []
        tool_calls: list[dict] = []
        any_content_yielded = False
        prompt_tokens = None
        completion_tokens = None
        try:
            with requests.post(f"{host}/api/chat", json=payload, timeout=1800, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message", {}) or {}
                    delta = msg.get("content", "")
                    if delta:
                        full_content.append(delta)
                        any_content_yielded = True
                        yield {"type": "content", "delta": delta}
                    if msg.get("tool_calls"):
                        tool_calls = msg["tool_calls"]
                    if chunk.get("done"):
                        prompt_tokens = chunk.get("prompt_eval_count")
                        completion_tokens = chunk.get("eval_count")
                        break
        except requests.RequestException as e:
            err = self._request_error(e)
            # Only attempt the fallback if: this is a capacity-shaped error (5xx),
            # nothing was streamed yet (retrying after partial output would be
            # confusing - duplicate or garbled text), and this isn't already a
            # retry (one level only - don't spiral through ever-smaller windows).
            if (not any_content_yielded and _ctx_override is None
                    and err.status_code is not None and err.status_code >= 500
                    and effective_ctx > 2048):
                fallback_ctx = max(2048, effective_ctx // 2)
                yield {"type": "context_fallback", "from": effective_ctx, "to": fallback_ctx}
                yield from self.chat_stream(messages, tools, images_b64, _ctx_override=fallback_ctx)
                return
            raise err
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens
        yield {"type": "done", "content": "".join(full_content), "tool_calls": tool_calls,
               "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}

    def _build_chat_payload(self, messages: list[dict], tools: list[dict] | None,
                             images_b64: list[str] | None, stream: bool,
                             ctx_override: int | None = None) -> dict:
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
                "num_ctx": ctx_override if ctx_override is not None else self.context_window,
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
                raise self._request_error(e)
            if len(embeddings) != len(batch):
                raise OllamaError(
                    f"Embedding batch mismatch: sent {len(batch)} texts, got {len(embeddings)} vectors back."
                )
            out.extend(embeddings)
        return out

    @staticmethod
    def image_to_b64(path: Path) -> str:
        return base64.b64encode(Path(path).read_bytes()).decode("utf-8")
