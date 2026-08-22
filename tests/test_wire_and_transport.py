import json

import httpx
import pytest
from pydantic import BaseModel

from codex_pool.config import CodexPoolConfig
from codex_pool.pool import CodexCredential
from codex_pool.transport import (
    CodexAuthExpiredError,
    CodexTransport,
    CodexTransportError,
    CodexUsageLimitError,
)
from codex_pool.types import ChatRequest, Message, Role
from codex_pool.wire import drop_nulls, stream_event_to_chunk, strict_schema, to_wire

CRED = CodexCredential(account_id="acct", label="a", access_token="tok", refresh_token="r")


def request(**kwargs) -> ChatRequest:
    return ChatRequest(
        model="gpt-test",
        messages=[
            Message(role=Role.SYSTEM, content="sys one"),
            Message(role=Role.SYSTEM, content="sys two"),
            Message(role=Role.USER, content="hello"),
            Message(role=Role.ASSISTANT, content="hi"),
        ],
        **kwargs,
    )


# ------------------------------------------------------------------- wire


def test_to_wire_hoists_system_messages():
    body = to_wire(request(), model_id="m")
    assert body["instructions"] == "sys one\n\nsys two"
    assert [item["role"] for item in body["input"]] == ["user", "assistant"]
    assert body["stream"] is True  # endpoint 400s without it
    assert body["store"] is False


def test_to_wire_includes_schema_when_given():
    body = to_wire(request(output_schema={"type": "object"}), model_id="m")
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["type"] == "json_schema"


def test_to_wire_omits_schema_by_default():
    assert "text" not in to_wire(request(), model_id="m")


def test_strict_schema_tightens_pydantic_output():
    class Inner(BaseModel):
        note: str = "d"

    class Outer(BaseModel):
        name: str
        count: int = 0
        inner: Inner

    tightened = strict_schema(Outer.model_json_schema())
    assert tightened["additionalProperties"] is False
    assert set(tightened["required"]) == {"name", "count", "inner"}
    assert "title" not in tightened
    nested = tightened["$defs"]["Inner"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["note"]  # optional made required — see drop_nulls


def test_drop_nulls_restores_pydantic_defaults():
    class Model(BaseModel):
        name: str
        note: str = "default"

    # strict mode forces every field into `required`, so the model emits null
    # for the one it had nothing to say about.
    raw = {"name": "x", "note": None}
    assert Model.model_validate(drop_nulls(raw)).note == "default"


def test_drop_nulls_recurses_into_nested_objects():
    raw = {"outer": {"keep": 1, "drop": None}, "top": None}
    assert drop_nulls(raw) == {"outer": {"keep": 1}}


def test_drop_nulls_keeps_list_positions():
    # A null inside a list is a positional value; removing it would shorten the list.
    raw = {"items": [1, None, 3]}
    assert drop_nulls(raw) == {"items": [1, None, 3]}


def test_drop_nulls_walks_objects_inside_lists():
    raw = {"items": [{"a": 1, "b": None}]}
    assert drop_nulls(raw) == {"items": [{"a": 1}]}


def test_stream_event_delta():
    chunk = stream_event_to_chunk({"type": "response.output_text.delta", "delta": "abc"})
    assert chunk.delta == "abc"
    assert chunk.done is False


def test_stream_event_completed_carries_usage():
    chunk = stream_event_to_chunk(
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-real",
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            },
        }
    )
    assert chunk.done is True
    assert chunk.model_id == "gpt-real"
    assert chunk.usage.total_tokens == 7


def test_stream_event_ignores_lifecycle_frames():
    assert stream_event_to_chunk({"type": "response.created"}) is None
    assert stream_event_to_chunk({"no": "type"}) is None


# -------------------------------------------------------------- transport


def sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def transport_for(handler) -> CodexTransport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CodexTransport(config=CodexPoolConfig(), client=client)


async def test_chat_aggregates_stream():
    def handler(req_):
        return httpx.Response(
            200,
            content=sse(
                {"type": "response.output_text.delta", "delta": "he"},
                {"type": "response.output_text.delta", "delta": "llo"},
                {"type": "response.completed", "response": {"model": "gpt-real"}},
            ),
        )

    result = await transport_for(handler).chat(request(), CRED, model_id="gpt-test")
    assert result.content == "hello"
    assert result.model_id == "gpt-real"


async def test_headers_carry_backend_identity():
    seen = {}

    def handler(req_):
        seen.update(req_.headers)
        return httpx.Response(200, content=sse({"type": "response.completed", "response": {}}))

    await transport_for(handler).chat(request(), CRED, model_id="m")
    assert seen["authorization"] == "Bearer tok"
    assert seen["chatgpt-account-id"] == "acct"
    assert seen["originator"] == "codex_cli_rs"


async def test_429_raises_usage_limit_with_resets_at():
    body = {"error": {"resets_at": 1700000000}}

    def handler(req_):
        return httpx.Response(429, json=body)

    with pytest.raises(CodexUsageLimitError) as exc:
        await transport_for(handler).chat(request(), CRED, model_id="m")
    assert exc.value.resets_at is not None


async def test_429_without_resets_at_is_still_usage_limit():
    def handler(req_):
        return httpx.Response(429, text="nope")

    with pytest.raises(CodexUsageLimitError) as exc:
        await transport_for(handler).chat(request(), CRED, model_id="m")
    assert exc.value.resets_at is None


async def test_401_raises_auth_expired():
    def handler(req_):
        return httpx.Response(401)

    with pytest.raises(CodexAuthExpiredError):
        await transport_for(handler).chat(request(), CRED, model_id="m")


async def test_truncated_stream_raises():
    # 200, deltas, but no terminal event: silent truncation must not pass as success.
    def handler(req_):
        return httpx.Response(200, content=sse({"type": "response.output_text.delta", "delta": "half"}))

    with pytest.raises(CodexTransportError, match="terminal event"):
        await transport_for(handler).chat(request(), CRED, model_id="m")


async def test_response_failed_raises_with_reason():
    def handler(req_):
        return httpx.Response(
            200,
            content=sse({"type": "response.failed", "response": {"error": {"message": "boom"}}}),
        )

    with pytest.raises(CodexTransportError, match="boom"):
        await transport_for(handler).chat(request(), CRED, model_id="m")


async def test_unparseable_sse_line_is_skipped():
    def handler(req_):
        content = b"data: {oops\n\n" + sse({"type": "response.completed", "response": {}})
        return httpx.Response(200, content=content)

    result = await transport_for(handler).chat(request(), CRED, model_id="m")
    assert result.content == ""  # survived the bad frame
