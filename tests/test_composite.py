"""Rotation behaviour — the part that must never regress.

Every test drives fakes: no network, no store, no sleeping.
"""

import datetime

import pytest

from codex_pool.composite import CodexPoolDryError, CompositeProvider
from codex_pool.pool import CodexCredential, CodexRefreshFailed
from codex_pool.transport import (
    CodexAuthExpiredError,
    CodexTransportError,
    CodexUsageLimitError,
)
from codex_pool.types import ChatRequest, ChatResponse, Message, Role, StreamChunk

TIERS = {"balanced": "gpt-test"}


def req() -> ChatRequest:
    return ChatRequest(model="balanced", messages=[Message(role=Role.USER, content="hi")])


def cred(account_id: str, token: str = "access") -> CodexCredential:
    return CodexCredential(
        account_id=account_id, label=account_id, access_token=token, refresh_token="r"
    )


class FakePool:
    def __init__(self, creds):
        self._queue = list(creds)
        self.exhausted: list[tuple[str, object]] = []
        self.unauthorized: list[tuple[str, str]] = []
        self.refreshes: list[str] = []
        self.refresh_result = None
        self.refresh_error: Exception | None = None

    async def acquire(self):
        return self._queue.pop(0) if self._queue else None

    async def mark_exhausted(self, account_id, *, resets_at):
        self.exhausted.append((account_id, resets_at))

    async def mark_unauthorized(self, account_id, *, reason):
        self.unauthorized.append((account_id, reason))

    async def refresh(self, account_id, *, stale_access_token=None):
        self.refreshes.append(account_id)
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refresh_result or cred(account_id, "refreshed")


class FakeCodex:
    """Replays a scripted outcome per call: an exception, or a list of deltas."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[tuple[str, str]] = []

    def _next(self, cred_obj, model_id):
        self.calls.append((cred_obj.account_id, model_id))
        return self._script.pop(0) if self._script else []

    async def chat(self, request, cred_obj, *, model_id):
        outcome = self._next(cred_obj, model_id)
        if isinstance(outcome, Exception):
            raise outcome
        return ChatResponse(
            content="".join(outcome), model_id=model_id, provider="codex-pool"
        )

    async def stream(self, request, cred_obj, *, model_id):
        outcome = self._next(cred_obj, model_id)
        if isinstance(outcome, Exception):
            raise outcome
        for piece in outcome:
            yield StreamChunk(delta=piece)
        yield StreamChunk(delta="", done=True, model_id=model_id)


class MidStreamCodex:
    """Emits chunks, then raises — the 'already emitted' path."""

    def __init__(self, error):
        self._error = error

    async def chat(self, request, cred_obj, *, model_id):  # pragma: no cover
        raise self._error

    async def stream(self, request, cred_obj, *, model_id):
        yield StreamChunk(delta="partial")
        raise self._error


class FakeFallback:
    def __init__(self):
        self.chats = 0
        self.streams = 0

    async def chat(self, request):
        self.chats += 1
        return ChatResponse(content="fallback", model_id="fb", provider="fallback")

    async def stream(self, request):
        self.streams += 1

        async def gen():
            yield StreamChunk(delta="fallback", done=True)

        return gen()


def provider(pool, codex, **kwargs):
    return CompositeProvider(pool=pool, codex=codex, tier_models=TIERS, **kwargs)


# ----------------------------------------------------------------- chat


async def test_chat_happy_path_resolves_tier():
    pool = FakePool([cred("a")])
    codex = FakeCodex([["ok"]])
    result = await provider(pool, codex).chat(req())
    assert result.content == "ok"
    assert codex.calls == [("a", "gpt-test")]  # tier name mapped to slug


async def test_chat_rotates_on_usage_limit():
    resets = datetime.datetime.now(datetime.UTC)
    pool = FakePool([cred("a"), cred("b")])
    codex = FakeCodex([CodexUsageLimitError("a", resets), ["from b"]])
    result = await provider(pool, codex).chat(req())
    assert result.content == "from b"
    assert pool.exhausted == [("a", resets)]


async def test_chat_refreshes_then_succeeds_on_same_account():
    pool = FakePool([cred("a")])
    codex = FakeCodex([CodexAuthExpiredError("a"), ["after refresh"]])
    result = await provider(pool, codex).chat(req())
    assert result.content == "after refresh"
    assert pool.refreshes == ["a"]
    assert pool.unauthorized == []  # a routine expiry must not retire the account


async def test_chat_disables_when_rejected_after_refresh():
    pool = FakePool([cred("a"), cred("b")])
    codex = FakeCodex([CodexAuthExpiredError("a"), CodexAuthExpiredError("a"), ["from b"]])
    result = await provider(pool, codex).chat(req())
    assert result.content == "from b"
    assert [acct for acct, _ in pool.unauthorized] == ["a"]


async def test_transient_refresh_failure_leaves_account_active():
    pool = FakePool([cred("a"), cred("b")])
    pool.refresh_error = CodexRefreshFailed("5xx", permanent=False)
    codex = FakeCodex([CodexAuthExpiredError("a"), ["from b"]])
    result = await provider(pool, codex).chat(req())
    assert result.content == "from b"
    assert pool.unauthorized == []  # a blip must never burn a credential


async def test_unclassified_refresh_error_leaves_account_active():
    pool = FakePool([cred("a"), cred("b")])
    pool.refresh_error = RuntimeError("nobody classified this")
    codex = FakeCodex([CodexAuthExpiredError("a"), ["from b"]])
    result = await provider(pool, codex).chat(req())
    assert result.content == "from b"
    assert pool.unauthorized == []


async def test_permanent_refresh_failure_disables():
    pool = FakePool([cred("a"), cred("b")])
    pool.refresh_error = CodexRefreshFailed("rejected", permanent=True)
    codex = FakeCodex([CodexAuthExpiredError("a"), ["from b"]])
    await provider(pool, codex).chat(req())
    assert [acct for acct, _ in pool.unauthorized] == ["a"]


async def test_chat_transport_error_propagates_without_rotating():
    pool = FakePool([cred("a"), cred("b")])
    codex = FakeCodex([CodexTransportError("backend hiccup")])
    with pytest.raises(CodexTransportError):
        await provider(pool, codex).chat(req())
    assert pool.exhausted == []
    assert pool.unauthorized == []


async def test_chat_falls_back_when_pool_dry():
    fallback = FakeFallback()
    result = await provider(FakePool([]), FakeCodex([]), fallback=fallback).chat(req())
    assert result.content == "fallback"
    assert fallback.chats == 1


async def test_chat_raises_when_dry_without_fallback():
    with pytest.raises(CodexPoolDryError):
        await provider(FakePool([]), FakeCodex([])).chat(req())


async def test_same_account_is_not_retried_twice():
    # Pool keeps handing back 'a' (sticky) — the composite must stop, not spin.
    pool = FakePool([cred("a"), cred("a")])
    codex = FakeCodex([CodexUsageLimitError("a", None)])
    with pytest.raises(CodexPoolDryError):
        await provider(pool, codex).chat(req())


# --------------------------------------------------------------- stream


async def collect(iterator):
    return [chunk async for chunk in iterator]


async def test_stream_happy_path():
    pool = FakePool([cred("a")])
    codex = FakeCodex([["he", "llo"]])
    chunks = await collect(await provider(pool, codex).stream(req()))
    assert "".join(c.delta for c in chunks) == "hello"
    assert chunks[-1].done is True


async def test_stream_rotates_before_first_chunk():
    pool = FakePool([cred("a"), cred("b")])
    codex = FakeCodex([CodexUsageLimitError("a", None), ["from b"]])
    chunks = await collect(await provider(pool, codex).stream(req()))
    assert "".join(c.delta for c in chunks) == "from b"


async def test_stream_mid_stream_usage_limit_raises_not_restarts():
    pool = FakePool([cred("a"), cred("b")])
    codex = MidStreamCodex(CodexUsageLimitError("a", None))
    with pytest.raises(CodexUsageLimitError):
        await collect(await provider(pool, codex).stream(req()))
    assert pool.exhausted  # still cooled, so the next request picks another


async def test_stream_refreshes_before_first_chunk():
    pool = FakePool([cred("a")])
    codex = FakeCodex([CodexAuthExpiredError("a"), ["renewed"]])
    chunks = await collect(await provider(pool, codex).stream(req()))
    assert "".join(c.delta for c in chunks) == "renewed"
    assert pool.unauthorized == []


async def test_stream_mid_stream_auth_error_disables_and_raises():
    pool = FakePool([cred("a")])
    codex = MidStreamCodex(CodexAuthExpiredError("a"))
    with pytest.raises(CodexAuthExpiredError):
        await collect(await provider(pool, codex).stream(req()))
    assert [reason for _, reason in pool.unauthorized] == ["token rejected mid-stream"]


async def test_stream_falls_back_when_dry():
    fallback = FakeFallback()
    chunks = await collect(
        await provider(FakePool([]), FakeCodex([]), fallback=fallback).stream(req())
    )
    assert "".join(c.delta for c in chunks) == "fallback"


# ------------------------------------------------------------ catalogue


async def test_resolver_runs_once_and_is_memoized():
    calls = {"n": 0}

    async def resolver(_cred):
        calls["n"] += 1
        return {"balanced": "live-model"}

    pool = FakePool([cred("a"), cred("a")])
    codex = FakeCodex([["1"], ["2"]])
    prov = CompositeProvider(pool=pool, codex=codex, tier_models=TIERS, resolver=resolver)
    await prov.chat(req())
    await prov.chat(req())
    assert calls["n"] == 1
    assert codex.calls == [("a", "live-model"), ("a", "live-model")]


async def test_resolver_failure_falls_back_to_pinned():
    async def resolver(_cred):
        raise RuntimeError("catalogue down")

    pool = FakePool([cred("a")])
    codex = FakeCodex([["ok"]])
    prov = CompositeProvider(pool=pool, codex=codex, tier_models=TIERS, resolver=resolver)
    result = await prov.chat(req())
    assert result.content == "ok"
    assert codex.calls == [("a", "gpt-test")]  # pinned id, request still served


async def test_partial_catalogue_fills_missing_tiers_from_pinned():
    async def resolver(_cred):
        return {"fast": "live-fast"}  # 'balanced' absent

    pool = FakePool([cred("a")])
    codex = FakeCodex([["ok"]])
    prov = CompositeProvider(pool=pool, codex=codex, tier_models=TIERS, resolver=resolver)
    await prov.chat(req())
    assert codex.calls == [("a", "gpt-test")]  # not left mapping to its own name


async def test_concrete_slug_passes_through():
    pool = FakePool([cred("a")])
    codex = FakeCodex([["ok"]])
    request = ChatRequest(model="gpt-5.6-sol", messages=[Message(role=Role.USER, content="x")])
    await provider(pool, codex).chat(request)
    assert codex.calls == [("a", "gpt-5.6-sol")]
