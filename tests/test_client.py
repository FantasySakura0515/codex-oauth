"""The plug-in surface: message normalising, structured output, error mapping.

No network and no store — the provider underneath is a fake, so these tests
cover exactly what the client itself adds.
"""

import json
import threading

import pytest
from pydantic import BaseModel, ValidationError

from codex_oauth import CodexOAuthClient, LLMClient, LLMUnavailableError
from codex_oauth.client import _strip_fences, _to_messages
from codex_oauth.composite import CodexPoolDryError
from codex_oauth.types import ChatResponse, Message, Role, StreamChunk


class Report(BaseModel):
    title: str
    notes: str = ""


class FakeProvider:
    """Replays one scripted outcome and records the requests it was given."""

    def __init__(self, content: str = "", error: Exception | None = None, deltas=()):
        self.content = content
        self.error = error
        self.deltas = list(deltas)
        self.requests = []
        self.closed = False

    async def chat(self, req):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return ChatResponse(content=self.content, model_id="gpt-test", provider="codex-oauth")

    async def stream(self, req):
        self.requests.append(req)
        if self.error is not None:
            raise self.error

        async def gen():
            try:
                for delta in self.deltas:
                    yield StreamChunk(delta=delta, done=False)
                yield StreamChunk(delta="", done=True)
            finally:
                self.closed = True

        return gen()


def client_for(provider, **kwargs) -> CodexOAuthClient:
    return CodexOAuthClient(provider=provider, **kwargs)


# ------------------------------------------------------------------ normalising


def test_string_prompt_becomes_one_user_message():
    assert _to_messages("hi", "") == [Message(role=Role.USER, content="hi")]


def test_system_is_prepended():
    assert _to_messages("hi", "be brief") == [
        Message(role=Role.SYSTEM, content="be brief"),
        Message(role=Role.USER, content="hi"),
    ]


def test_dict_messages_keep_known_roles():
    items = _to_messages(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], ""
    )
    assert [m.role for m in items] == [Role.USER, Role.ASSISTANT]


def test_unknown_role_is_sent_as_user():
    # Codex input items understand user/assistant only; "tool" must not 400 the
    # request, and dropping the turn would silently change the conversation.
    items = _to_messages([{"role": "tool", "content": "x"}], "")
    assert items == [Message(role=Role.USER, content="x")]


def test_message_objects_pass_through():
    original = Message(role=Role.ASSISTANT, content="a")
    assert _to_messages([original], "") == [original]


def test_strip_fences_unwraps_json_block():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_leaves_bare_json():
    assert _strip_fences(' {"a": 1} ') == '{"a": 1}'


# ------------------------------------------------------------------- structured


def test_structured_validates_into_the_model():
    provider = FakeProvider(content=json.dumps({"title": "t", "notes": "n"}))
    report = client_for(provider).complete_structured(Report, "go")
    assert report == Report(title="t", notes="n")


def test_structured_accepts_the_all_keyword_call():
    provider = FakeProvider(content=json.dumps({"title": "t", "notes": "n"}))
    report = client_for(provider).complete_structured(
        system="be brief", messages=[{"role": "user", "content": "go"}], response_model=Report
    )
    assert report.title == "t"
    assert provider.requests[0].messages[0].role == Role.SYSTEM


def test_structured_sends_a_strict_schema():
    provider = FakeProvider(content=json.dumps({"title": "t", "notes": "n"}))
    client_for(provider).complete_structured(Report, "go")
    schema = provider.requests[0].output_schema
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["notes", "title"]


def test_structured_drops_nulls_so_defaults_apply():
    # Strict mode lists every field as required, so the model answers null for
    # the ones it has nothing to say about. Validating that directly would fail
    # on a non-nullable ``notes: str = ""``.
    provider = FakeProvider(content=json.dumps({"title": "t", "notes": None}))
    assert client_for(provider).complete_structured(Report, "go").notes == ""


def test_structured_tolerates_a_fenced_answer():
    provider = FakeProvider(content='```json\n{"title": "t", "notes": "n"}\n```')
    assert client_for(provider).complete_structured(Report, "go").title == "t"


def test_undecodable_answer_raises_validation_error():
    # Not LLMUnavailableError: the backend answered, the answer was bad — that
    # is the retry-with-a-repair-prompt case, and callers catch it separately.
    provider = FakeProvider(content="sorry, I can't do that")
    with pytest.raises(ValidationError):
        client_for(provider).complete_structured(Report, "go")


def test_schema_mismatch_raises_validation_error():
    provider = FakeProvider(content=json.dumps({"notes": "n"}))
    with pytest.raises(ValidationError):
        client_for(provider).complete_structured(Report, "go")


def test_structured_needs_both_arguments():
    with pytest.raises(TypeError):
        client_for(FakeProvider()).complete_structured(Report)


# ------------------------------------------------------------------------ text


def test_complete_returns_content():
    assert client_for(FakeProvider(content="hello")).complete("hi") == "hello"


def test_model_override_beats_the_default():
    provider = FakeProvider(content="x")
    client_for(provider, model="balanced").complete("hi", model="gpt-5.6")
    assert provider.requests[0].model == "gpt-5.6"


# ---------------------------------------------------------------- error mapping


def test_pool_dry_becomes_llm_unavailable():
    provider = FakeProvider(error=CodexPoolDryError("dry"))
    with pytest.raises(LLMUnavailableError, match="dry"):
        client_for(provider).complete("hi")


def test_missing_store_file_is_reported_with_the_path(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(LLMUnavailableError, match="nope.json"):
        CodexOAuthClient(missing)


def test_client_without_store_or_provider_is_a_programming_error():
    with pytest.raises(ValueError):
        CodexOAuthClient()


async def test_sync_call_inside_an_event_loop_refuses():
    # Blocking here would stall the caller's loop for the whole completion.
    with pytest.raises(RuntimeError, match="acomplete"):
        client_for(FakeProvider(content="x")).complete("hi")


async def test_async_twin_works_inside_an_event_loop():
    assert await client_for(FakeProvider(content="x")).acomplete("hi") == "x"


# ---------------------------------------------------------------------- stream


def test_stream_yields_deltas_and_closes_the_iterator():
    provider = FakeProvider(deltas=["a", "b"])
    assert list(client_for(provider).stream("hi")) == ["a", "b"]
    assert provider.closed


def test_stream_stops_early_without_leaking_the_iterator():
    provider = FakeProvider(deltas=["a", "b", "c"])
    stream = client_for(provider).stream("hi")
    assert next(stream) == "a"
    stream.close()
    assert provider.closed


async def test_astream_yields_deltas():
    provider = FakeProvider(deltas=["a", "b"])
    assert [d async for d in client_for(provider).astream("hi")] == ["a", "b"]


# ------------------------------------------------------------------------- env


def store_file(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text('{"accounts": []}', encoding="utf-8")
    return path


def test_from_env_without_a_store_says_so(monkeypatch):
    monkeypatch.delenv("CODEX_OAUTH_STORE", raising=False)
    monkeypatch.delenv("CODEX_POOL_STORE", raising=False)
    with pytest.raises(LLMUnavailableError, match="CODEX_OAUTH_STORE"):
        CodexOAuthClient.from_env()


def test_from_env_reads_the_pre_rename_spelling(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_OAUTH_STORE", raising=False)
    monkeypatch.setenv("CODEX_POOL_STORE", str(store_file(tmp_path)))
    monkeypatch.setenv("CODEX_POOL_MODEL", "strong")
    assert CodexOAuthClient.from_env()._model == "strong"


def test_from_env_prefers_the_new_spelling(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_OAUTH_STORE", str(store_file(tmp_path)))
    monkeypatch.setenv("CODEX_OAUTH_MODEL", "fast")
    monkeypatch.setenv("CODEX_POOL_MODEL", "strong")
    assert CodexOAuthClient.from_env()._model == "fast"


def test_from_env_reads_model_and_client_version(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_OAUTH_STORE", str(store_file(tmp_path)))
    monkeypatch.setenv("CODEX_OAUTH_MODEL", "strong")
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_VERSION", "9.9.9")
    client = CodexOAuthClient.from_env()
    assert client._model == "strong"
    assert client._config.client_version == "9.9.9"


def test_from_env_falls_back_on_an_unparseable_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_OAUTH_STORE", str(store_file(tmp_path)))
    monkeypatch.setenv("CODEX_OAUTH_TIMEOUT", "soon")
    assert CodexOAuthClient.from_env()._timeout == 300.0


def test_from_env_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_OAUTH_STORE", str(store_file(tmp_path)))
    monkeypatch.setenv("CODEX_OAUTH_MODEL", "strong")
    assert CodexOAuthClient.from_env(model="fast")._model == "fast"


# ---------------------------------------------------------------------- protocol


def test_the_client_satisfies_the_protocol():
    assert isinstance(client_for(FakeProvider()), LLMClient)


def test_the_loop_thread_is_shared_across_clients():
    # One loop per process is what makes the store's lock actually exclusive
    # between callers on different threads.
    assert client_for(FakeProvider())._loop is client_for(FakeProvider())._loop


def test_sync_calls_work_from_several_threads():
    provider = FakeProvider(content="x")
    client = client_for(provider)
    results = []

    def call():
        results.append(client.complete("hi"))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert results == ["x"] * 4
