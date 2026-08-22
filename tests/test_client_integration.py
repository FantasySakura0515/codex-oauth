"""One call through the fully assembled stack — client, pool, transport, store.

Only HTTP is faked. This is what proves the wiring ``TidepoolClient`` does on
the caller's behalf actually holds together; every other client test injects a
provider and skips it.
"""

import json

import httpx
import respx

from tidepool import TidepoolClient


def sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def store_file(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_id": "acct-1",
                        "label": "a",
                        "access_token": "token",
                        "refresh_token": "refresh",
                        "status": "available",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


@respx.mock
def test_structured_call_end_to_end(tmp_path):
    respx.get(url__regex=r".*/models").mock(
        return_value=httpx.Response(
            200, json={"models": [{"slug": "gpt-live", "priority": 1}]}
        )
    )
    responses = respx.post(url__regex=r".*/responses").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {
                    "type": "response.output_text.delta",
                    "delta": '{"title": "t", "notes": null}',
                },
                {"type": "response.completed", "response": {"model": "gpt-live"}},
            ),
        )
    )

    from pydantic import BaseModel

    class Report(BaseModel):
        title: str
        notes: str = ""

    with TidepoolClient(store_file(tmp_path), model="strong") as llm:
        report = llm.complete_structured(Report, "go", system="be brief")

        # The catalogue answered, so "strong" became a live slug rather than
        # the pinned fallback.
        body = json.loads(responses.calls[0].request.content)
        assert body["model"] == "gpt-live"
        assert body["instructions"] == "be brief"
        # notes came back as null (strict mode requires every field); the
        # default survived it.
        assert report == Report(title="t", notes="")
        assert [a.status for a in llm.status()] == ["active"]


@respx.mock
def test_pool_dry_surfaces_as_llm_unavailable(tmp_path):
    from tidepool import LLMUnavailableError

    path = tmp_path / "accounts.json"
    path.write_text('{"accounts": []}', encoding="utf-8")

    with TidepoolClient(path) as llm:
        try:
            llm.complete("hi")
        except LLMUnavailableError as err:
            assert "cooling or disabled" in str(err)
        else:  # pragma: no cover
            raise AssertionError("expected LLMUnavailableError")
