"""Request/response types for the chat surface.

``ChatRequest.model`` carries either a tier name resolved through the live
catalogue (``"fast"`` / ``"balanced"`` / ``"strong"``) or a concrete model slug,
which passes through unchanged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    """A single chat turn."""

    role: Role
    content: str


class ChatRequest(BaseModel):
    """Input to ``chat`` / ``stream``.

    ``output_schema`` (optional) is a JSON Schema the response must satisfy —
    it rides the Responses ``text.format`` field in OpenAI *strict* structured
    output mode, the same wire shape the Codex CLI's ``--output-schema`` uses.
    Run it through :func:`codex_pool.wire.strict_schema` first: strict mode
    rejects schemas that allow additional properties or optional fields.
    """

    model: str
    messages: list[Message]
    max_tokens: int | None = None
    reasoning_effort: str = "medium"
    output_schema: dict[str, Any] | None = None


class TokenUsage(BaseModel):
    """Token accounting for a single completion call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """Result of a non-streaming chat call."""

    content: str
    model_id: str
    provider: str
    usage: TokenUsage = Field(default_factory=TokenUsage)


class StreamChunk(BaseModel):
    """One frame of a streaming response.

    ``model_id`` / ``usage`` are populated best-effort on the terminal
    ``done=True`` chunk only; both stay ``None`` when the backend does not
    supply them rather than fabricating zero values.
    """

    delta: str = ""
    done: bool = False
    model_id: str | None = None
    usage: TokenUsage | None = None


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "Message",
    "Role",
    "StreamChunk",
    "TokenUsage",
]
