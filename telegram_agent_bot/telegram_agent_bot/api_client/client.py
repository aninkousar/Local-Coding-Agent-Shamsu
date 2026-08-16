"""Typed async HTTP client for the local coding agent's REST API.

This is the ONLY module that knows the agent speaks HTTP - every other part
of the bot goes through AgentAPIClient and only ever sees typed Pydantic
models, never raw dicts or URLs. That's what keeps the bot independent from
the agent's actual implementation, per the design brief: swap this one
module's internals (e.g. point it at a different agent, or a different
transport) and nothing else in the bot needs to change.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import httpx
from pydantic import ValidationError

from bot.models.schemas import (
    FileContent,
    FileEntry,
    FollowUpPromptRequest,
    Project,
    PromptRequest,
    PromptResponse,
    SystemStatus,
)

logger = logging.getLogger(__name__)


class AgentAPIError(Exception):
    """Raised for any failure talking to the agent - connection refused,
    timeout, a non-2xx response, or a response that doesn't match the
    expected schema. Callers (handlers) catch this ONE exception type
    rather than needing to know about httpx or pydantic specifically."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, retriable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable


class AgentAPIClient:
    """Async client for the agent's REST API. Every method retries on
    connection failures and 5xx responses (the retriable, likely-transient
    failure modes), with exponential backoff - but never retries on a 4xx
    response, since retrying a request the agent has already rejected as
    invalid would just fail the same way again.
    """

    def __init__(self, base_url: str, timeout: float = 30.0,
                 max_retries: int = 3, retry_backoff: float = 1.5) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AgentAPIClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # --- internal request machinery --------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Core retry loop, shared by every endpoint method below. Retries
        on connection errors, timeouts, and 5xx responses; raises
        immediately (no retry) on a 4xx response, since that means the
        request itself was invalid, not that the agent was temporarily
        unavailable."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout) as e:
                last_error = e
                logger.warning("Agent API %s %s failed (attempt %d/%d): %s",
                                method, path, attempt, self._max_retries, e)
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_backoff * attempt)
                continue

            if resp.status_code >= 500:
                last_error = AgentAPIError(
                    f"Agent returned {resp.status_code} for {method} {path}",
                    status_code=resp.status_code, retriable=True,
                )
                logger.warning("Agent API %s %s returned %d (attempt %d/%d)",
                                method, path, resp.status_code, attempt, self._max_retries)
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_backoff * attempt)
                continue

            if resp.status_code >= 400:
                # Client error - the request itself was bad, retrying won't help.
                raise AgentAPIError(
                    f"Agent rejected {method} {path}: {resp.status_code} {resp.text[:200]}",
                    status_code=resp.status_code, retriable=False,
                )

            try:
                return resp.json()
            except ValueError as e:
                raise AgentAPIError(f"Agent returned invalid JSON for {method} {path}: {e}")

        # Every retry attempt was exhausted on a retriable failure.
        raise AgentAPIError(
            f"Agent unreachable after {self._max_retries} attempts for {method} {path}: {last_error}",
            retriable=True,
        )

    # --- endpoints, per the API Specification -----------------------------------

    async def send_prompt(self, prompt: str) -> PromptResponse:
        """POST /prompt"""
        data = await self._request("POST", "/prompt", json=PromptRequest(prompt=prompt).model_dump())
        return _parse(PromptResponse, data, "POST /prompt")

    async def send_followup_prompt(self, project_id: str, prompt: str) -> PromptResponse:
        """POST /project/{id}/prompt"""
        body = FollowUpPromptRequest(project_id=project_id, prompt=prompt)
        data = await self._request("POST", f"/project/{project_id}/prompt", json=body.model_dump())
        return _parse(PromptResponse, data, f"POST /project/{project_id}/prompt")

    async def list_projects(self) -> List[Project]:
        """GET /projects"""
        data = await self._request("GET", "/projects")
        items = data.get("projects", data) if isinstance(data, dict) else data
        return [_parse(Project, item, "GET /projects") for item in items]

    async def get_project(self, project_id: str) -> Project:
        """GET /project/{id}"""
        data = await self._request("GET", f"/project/{project_id}")
        return _parse(Project, data, f"GET /project/{project_id}")

    async def get_logs(self, project_id: str, limit: int = 20, offset: int = 0) -> List[dict]:
        """GET /project/{id}/logs"""
        data = await self._request(
            "GET", f"/project/{project_id}/logs", params={"limit": limit, "offset": offset}
        )
        return data.get("logs", data) if isinstance(data, dict) else data

    async def get_files(self, project_id: str, path: str = "") -> List[FileEntry]:
        """GET /project/{id}/files"""
        data = await self._request("GET", f"/project/{project_id}/files", params={"path": path})
        items = data.get("files", data) if isinstance(data, dict) else data
        return [_parse(FileEntry, item, "GET .../files") for item in items]

    async def get_file_content(self, project_id: str, path: str, offset: int = 0,
                                limit: int = 100) -> FileContent:
        """GET /project/{id}/files - with a specific file path, returns
        content instead of a directory listing. First N lines per the spec
        ("First 100 lines"), paginated via offset/limit for larger files."""
        data = await self._request(
            "GET", f"/project/{project_id}/files",
            params={"path": path, "offset": offset, "limit": limit},
        )
        return _parse(FileContent, data, "GET .../files (content)")

    async def get_system_status(self) -> SystemStatus:
        """GET /system"""
        data = await self._request("GET", "/system")
        return _parse(SystemStatus, data, "GET /system")

    async def control_project(self, project_id: str, action: str) -> None:
        """Pause/Resume/Stop, per the Project Detail buttons. Not a
        separately-numbered endpoint in the spec's list, implemented as a
        natural extension of the same /project/{id} resource."""
        if action not in ("pause", "resume", "stop"):
            raise ValueError(f"Unknown control action: {action}")
        await self._request("POST", f"/project/{project_id}/{action}")


def _parse(model_cls, data: dict, context: str):
    """Parses agent response data into the expected Pydantic model, wrapping
    a validation failure in the same AgentAPIError type everything else
    raises - callers never need to catch pydantic.ValidationError directly."""
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        raise AgentAPIError(f"Agent response for {context} didn't match the expected shape: {e}")
