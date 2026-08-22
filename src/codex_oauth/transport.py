"""Codex transport — one SSE request for one credential.

Knows nothing about the account pool: it receives a credential, performs one
request, and either returns or raises a typed error. Rotation decisions belong
to :mod:`codex_oauth.composite`, pool state to :mod:`codex_oauth.pool` — that
split is what lets every rotation scenario be tested without a network.

Imports ``httpx`` only, no vendor SDK.
"""

from __future__ import annotations

import datetime
import json
import logging
from collections.abc import AsyncIterator

import httpx

from codex_oauth.catalogue import backend_headers
from codex_oauth.config import CodexOAuthConfig
from codex_oauth.pool import CodexCredential
from codex_oauth.types import ChatRequest, ChatResponse, StreamChunk, TokenUsage
from codex_oauth.wire import stream_event_to_chunk, to_wire

logger = logging.getLogger(__name__)

PROVIDER_NAME = "codex-oauth"


class CodexTransportError(RuntimeError):
    """The Codex backend returned an error this layer cannot interpret."""


class CodexUsageLimitError(CodexTransportError):
    """The account hit its subscription quota (HTTP 429)."""

    def __init__(self, account_id: str, resets_at: datetime.datetime | None) -> None:
        super().__init__(f"codex account {account_id} reached its usage limit")
        self.account_id = account_id
        self.resets_at = resets_at


class CodexAuthExpiredError(CodexTransportError):
    """The access token is expired or rejected (HTTP 401)."""

    def __init__(self, account_id: str) -> None:
        super().__init__(f"codex account {account_id} token rejected")
        self.account_id = account_id


def _parse_resets_at(payload: object) -> datetime.datetime | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    resets_at = error.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(float(resets_at), tz=datetime.UTC)


def _failure_message(event: dict[str, object]) -> str:
    """Best-effort reason out of a ``response.failed`` event."""

    response = event.get("response")
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:300]
    return "response.failed"


class CodexTransport:
    """Performs Codex Responses calls for a single supplied credential."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        config: CodexOAuthConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or CodexOAuthConfig()
        self._client = client

    def _headers(self, cred: CodexCredential) -> dict[str, str]:
        headers = backend_headers(cred, self._config)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"
        return headers

    def _raise_for_status(self, cred: CodexCredential, status: int, body: str) -> None:
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        if status == 429:
            raise CodexUsageLimitError(cred.account_id, _parse_resets_at(payload))
        if status == 401:
            raise CodexAuthExpiredError(cred.account_id)
        raise CodexTransportError(f"codex responses HTTP {status}: {body[:300]}")

    async def stream(
        self, req: ChatRequest, cred: CodexCredential, *, model_id: str
    ) -> AsyncIterator[StreamChunk]:
        """Yield chunks for ``req``. The final chunk has ``done=True``.

        A 200 stream that ends without a terminal event — closed early, or
        ``response.failed`` — raises instead of ending quietly: silent
        truncation is indistinguishable from a short answer, and a structured
        payload cut in half parses as garbage downstream.
        """

        completed = False
        owns_client = self._client is None
        http = self._client or httpx.AsyncClient(
            timeout=self._config.request_timeout_seconds
        )
        try:
            async with http.stream(
                "POST",
                f"{self._config.api_base}/responses",
                json=to_wire(req, model_id=model_id),
                headers=self._headers(cred),
                params={"client_version": self._config.client_version},
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    self._raise_for_status(cred, response.status_code, body)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        logger.debug("codex: skipping unparseable SSE line")
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "response.failed":
                        raise CodexTransportError(
                            f"codex stream failed: {_failure_message(event)}"
                        )
                    chunk = stream_event_to_chunk(event)
                    if chunk is not None:
                        completed = completed or chunk.done
                        yield chunk
                if not completed:
                    # Necessarily raised after partial deltas were yielded. That
                    # is intended: the before-first-chunk guarantee applies to
                    # HTTP-status errors only, and the composite's emitted-flag
                    # logic propagates a mid-stream error to the caller instead
                    # of rotating to another account.
                    raise CodexTransportError("codex stream ended without terminal event")
        finally:
            if owns_client:
                await http.aclose()

    async def chat(
        self, req: ChatRequest, cred: CodexCredential, *, model_id: str
    ) -> ChatResponse:
        """Aggregate the stream into a single response.

        The endpoint is streaming-only, so this is the only way to serve a
        non-streaming call.
        """

        parts: list[str] = []
        usage: TokenUsage | None = None
        resolved_model = model_id
        async for chunk in self.stream(req, cred, model_id=model_id):
            if chunk.delta:
                parts.append(chunk.delta)
            if chunk.done:
                usage = chunk.usage
                resolved_model = chunk.model_id or model_id
        return ChatResponse(
            content="".join(parts),
            model_id=resolved_model,
            provider=PROVIDER_NAME,
            usage=usage or TokenUsage(),
        )


__all__ = [
    "CodexAuthExpiredError",
    "CodexTransport",
    "CodexTransportError",
    "CodexUsageLimitError",
    "PROVIDER_NAME",
]
