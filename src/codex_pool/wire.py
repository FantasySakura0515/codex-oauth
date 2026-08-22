"""Codex Responses wire codec.

Pure translation between :mod:`codex_pool.types` and the Codex Responses wire
format. No IO, no vendor SDK. Structured output rides the ``text.format``
field — the wire shape behind the Codex CLI's ``--output-schema``.

There is no ``from_wire``: the endpoint rejects non-streaming requests
(``400 {"detail":"Stream must be set to true"}``), so every response arrives as
SSE and the transport aggregates :func:`stream_event_to_chunk` output instead.
"""

from __future__ import annotations

from typing import Any

from codex_pool.types import ChatRequest, Role, StreamChunk, TokenUsage

# Annotation-only keywords OpenAI strict mode rejects or ignores.
_SCHEMA_ANNOTATIONS = frozenset({"title", "default", "examples", "$comment"})
# Keywords whose value maps *names* to schemas. Their keys are field names, so
# the walk must recurse into the values without filtering the keys.
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def strict_schema(schema: object) -> object:
    """A pydantic JSON schema in OpenAI structured-output *strict* form.

    Strict mode rejects any object that does not carry ``additionalProperties:
    false`` and list every property in ``required``. Pydantic emits neither, so
    the schema is walked and tightened rather than maintained by hand beside
    the models — a second copy would drift the first time a field is added.

    Making every property required changes what comes back: a field the model
    has nothing to say about arrives as ``null`` rather than being omitted, and
    ``null`` does **not** satisfy a non-nullable annotation like ``str = ""`` —
    pydantic raises instead of applying the default. Run the decoded payload
    through :func:`drop_nulls` before validating; that is what turns the nulls
    back into "absent, use the default".
    """

    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for key, value in schema.items():
        if key in _SCHEMA_ANNOTATIONS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: strict_schema(sub) for name, sub in value.items()}
        else:
            out[key] = strict_schema(value)
    if "properties" in out:
        out["type"] = "object"
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def drop_nulls(payload: object) -> object:
    """Recursively remove ``null``-valued keys from a decoded strict-mode payload.

    The companion to :func:`strict_schema`: it made every property required, so
    the model emits ``"notes": null`` where it would otherwise have omitted the
    key. Removing those keys restores "absent", which is what a pydantic model
    needs to apply the field's default.

    A field declared ``X | None`` with default ``None`` is unaffected — dropping
    the key yields ``None`` either way. A field that is nullable *and* required
    (``X | None`` with no default) is the one shape this would break, turning a
    legitimate ``null`` into a missing-field error; give such a field a default
    of ``None``, which is what it means anyway.

    List elements are walked but never dropped — a ``null`` inside a list is a
    positional value, not an omitted key, and removing it would silently
    shorten the list.
    """

    if isinstance(payload, dict):
        return {key: drop_nulls(value) for key, value in payload.items() if value is not None}
    if isinstance(payload, list):
        return [drop_nulls(item) for item in payload]
    return payload


def _message_role(role: Role | str) -> str:
    """Codex input items only understand ``user`` / ``assistant``."""

    return "assistant" if role == Role.ASSISTANT else "user"


def to_wire(req: ChatRequest, *, model_id: str) -> dict[str, Any]:
    """Translate a :class:`ChatRequest` into a Codex Responses request body.

    System messages are hoisted into the single top-level ``instructions``
    string (Codex accepts only one), joined by a blank line.
    """

    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in req.messages:
        if message.role == Role.SYSTEM:
            instructions_parts.append(message.content)
            continue
        input_items.append(
            {
                "type": "message",
                "role": _message_role(message.role),
                "content": [{"type": "input_text", "text": message.content}],
            }
        )

    body: dict[str, Any] = {
        "model": model_id,
        "instructions": "\n\n".join(instructions_parts),
        "input": input_items,
        "store": False,
        # Non-negotiable: the endpoint 400s without it.
        "stream": True,
        "reasoning": {"effort": req.reasoning_effort, "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
    }
    if req.max_tokens is not None:
        body["max_output_tokens"] = req.max_tokens
    if req.output_schema is not None:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "structured_output",
                "strict": True,
                "schema": req.output_schema,
            }
        }
    return body


def stream_event_to_chunk(event: dict[str, Any]) -> StreamChunk | None:
    """Translate one Codex SSE event into a :class:`StreamChunk`.

    Returns ``None`` for events the caller should skip — lifecycle frames and
    reasoning summaries are not part of the answer text.
    """

    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None

    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if not isinstance(delta, str):
            return None
        return StreamChunk(delta=delta, done=False)

    if event_type == "response.completed":
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        raw_usage = response.get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            usage = TokenUsage(
                prompt_tokens=int(raw_usage.get("input_tokens") or 0),
                completion_tokens=int(raw_usage.get("output_tokens") or 0),
                total_tokens=int(raw_usage.get("total_tokens") or 0),
            )
        model_id = response.get("model")
        return StreamChunk(
            delta="",
            done=True,
            model_id=model_id if isinstance(model_id, str) else None,
            usage=usage,
        )

    return None


__all__ = ["drop_nulls", "stream_event_to_chunk", "strict_schema", "to_wire"]
