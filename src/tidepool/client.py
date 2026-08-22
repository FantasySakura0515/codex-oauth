"""The plug-in surface — one object, one line of wiring.

Everything below this module (pool rotation, 429 cooldowns, token refresh,
model resolution, the Codex wire format, strict-mode structured output) is an
implementation detail of these methods::

    llm = TidepoolClient.from_env()
    report = llm.complete_structured(response_model=Report, messages="...")

The methods are synchronous because most application code is. Inside an event
loop use the ``a``-prefixed twins instead; the sync ones refuse to run there
rather than quietly blocking the caller's loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

import httpx
from pydantic import BaseModel, ValidationError

from tidepool.catalogue import resolve_tier_models
from tidepool.composite import CompositeProvider
from tidepool.config import TidepoolConfig
from tidepool.pool import AccountStatus, CodexPool
from tidepool.store import AccountStore, JsonFileStore
from tidepool.transport import CodexTransport
from tidepool.types import ChatRequest, Message, Role
from tidepool.wire import drop_nulls, strict_schema

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: A prompt is a bare string, or a sequence of ``{"role", "content"}`` mappings
#: / :class:`Message` objects. Roles other than ``system``/``assistant`` are
#: sent as ``user`` — the Codex input format knows no others.
Prompt = str | Sequence[Mapping[str, Any] | Message]

DEFAULT_MODEL = "balanced"
DEFAULT_TIMEOUT = 300.0


class LLMUnavailableError(RuntimeError):
    """No answer could be obtained: pool dry, credentials dead, timeout, backend error.

    The one error application code has to handle. A *bad* answer is not this —
    a reply that does not fit ``response_model`` raises pydantic's
    ``ValidationError``, which callers can catch separately to retry with a
    repair prompt.
    """


@runtime_checkable
class LLMClient(Protocol):
    """What an application should depend on. Any backend can implement it."""

    def complete(self, messages: Prompt, *, system: str = ...) -> str: ...

    def complete_structured(
        self, *, system: str, messages: Prompt, response_model: type[T]
    ) -> T: ...


# --------------------------------------------------------------------- bridge


class _LoopThread:
    """One daemon event loop for the whole process, started on first use.

    A fresh ``asyncio.run()`` per call would bind the store's lock to a loop
    that is then closed, so the second call fails and concurrent callers never
    actually exclude each other. One long-lived loop gives every caller —
    including several threads of a web server's threadpool — the same lock.
    """

    _instance: _LoopThread | None = None
    _guard = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        # daemon: nothing here needs flushing at shutdown — credential state is
        # committed to the store at the end of every transaction.
        self._thread = threading.Thread(
            target=self._serve, name="tidepool-loop", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @classmethod
    def shared(cls) -> _LoopThread:
        with cls._guard:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run(self, coro: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Cancel the loop-side task too, or it runs on in the background
            # still holding the store transaction.
            future.cancel()
            raise


_DONE = object()


async def _anext_or_done(iterator: AsyncIterator[Any]) -> Any:
    """``__anext__`` with exhaustion as a value rather than an exception.

    ``StopAsyncIteration`` travelling across a thread boundary inside a future
    is not worth relying on; a sentinel is.
    """

    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _DONE


# ---------------------------------------------------------------- normalising


def _strip_fences(raw: str) -> str:
    """Unwrap a fenced code block. Strict mode rarely emits one; be tolerant anyway."""

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


def _env(suffix: str) -> str:
    """Read ``TIDEPOOL_<suffix>``, falling back to the pre-rename spelling.

    ``CODEX_POOL_*`` was the prefix before the tidepool rename; deployments
    that still set it keep working through the transition, with a nudge in the
    log rather than a hard break.
    """

    value = os.environ.get(f"TIDEPOOL_{suffix}", "").strip()
    if value:
        return value
    legacy = os.environ.get(f"CODEX_POOL_{suffix}", "").strip()
    if legacy:
        logger.warning(
            "CODEX_POOL_%s is the pre-rename spelling; rename it to TIDEPOOL_%s",
            suffix,
            suffix,
        )
    return legacy


def _float_env(suffix: str) -> float | None:
    """A numeric environment variable, or ``None`` when unset or unusable.

    An unparseable value is a warning rather than a crash: a typo in one
    optional tuning knob must not take the whole backend down.
    """

    raw = _env(suffix)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("TIDEPOOL_%s=%r is not a number; ignoring it", suffix, raw)
        return None


_ROLES = {role.value for role in Role}


def _to_messages(prompt: Prompt, system: str) -> list[Message]:
    items: list[Message] = []
    if system:
        items.append(Message(role=Role.SYSTEM, content=system))
    if isinstance(prompt, str):
        items.append(Message(role=Role.USER, content=prompt))
        return items
    for entry in prompt:
        if isinstance(entry, Message):
            items.append(entry)
            continue
        role = str(entry.get("role", "user"))
        items.append(
            Message(
                role=Role(role) if role in _ROLES else Role.USER,
                content=str(entry.get("content", "")),
            )
        )
    return items


# --------------------------------------------------------------------- client


class TidepoolClient:
    """Synchronous LLM client over a ChatGPT-subscription account pool.

    Construct it with a store path — or an :class:`~tidepool.store.AccountStore`
    for a custom backing store — or use :meth:`from_env`.
    """

    provider_name = "tidepool"

    def __init__(
        self,
        store: str | Path | AccountStore | None = None,
        *,
        model: str = DEFAULT_MODEL,
        config: TidepoolConfig | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        fallback: Any | None = None,
        provider: Any | None = None,
    ) -> None:
        if store is None and provider is None:
            raise ValueError("TidepoolClient needs a store path (or a provider)")
        if isinstance(store, (str, Path)):
            path = Path(store).expanduser()
            if not path.is_file():
                # An empty pool is harder to diagnose than a missing file, so
                # name the file and the command that creates it.
                raise LLMUnavailableError(
                    f"tidepool store not found: {path} — run tidepool-import first"
                )
            store = JsonFileStore(path)
        self._store = store
        self._model = model
        self._config = config or TidepoolConfig()
        self._timeout = timeout
        self._fallback = fallback
        self._provider = provider
        self._http: httpx.AsyncClient | None = None
        self._loop = _LoopThread.shared()

    # ------------------------------------------------------------------ build

    @classmethod
    def from_env(cls, **overrides: Any) -> TidepoolClient:
        """Build from ``TIDEPOOL_*`` environment variables.

        ``TIDEPOOL_STORE`` (required — the credential store path),
        ``TIDEPOOL_MODEL`` (tier name or concrete slug),
        ``TIDEPOOL_CLIENT_VERSION`` (match the codex CLI that produced the
        credentials), ``TIDEPOOL_TIMEOUT`` (seconds per call). The pre-rename
        ``CODEX_POOL_*`` spellings are still read as a fallback.

        Raises :class:`LLMUnavailableError` when the store is unset or absent,
        so an application treating this backend as optional catches one error
        and moves on.
        """

        raw_store = _env("STORE")
        if not raw_store and "store" not in overrides:
            raise LLMUnavailableError("TIDEPOOL_STORE is not set")

        config_kwargs: dict[str, Any] = {}
        client_version = _env("CLIENT_VERSION")
        if client_version:
            config_kwargs["client_version"] = client_version
        ttl = _float_env("CATALOGUE_TTL")
        if ttl is not None:
            config_kwargs["catalogue_ttl_seconds"] = ttl

        kwargs: dict[str, Any] = {
            "store": raw_store,
            "model": _env("MODEL") or DEFAULT_MODEL,
            "config": TidepoolConfig(**config_kwargs),
            "timeout": _float_env("TIMEOUT") or DEFAULT_TIMEOUT,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def _build_provider(self) -> Any:
        """Assemble the stack. Only ever runs on the loop thread, so no lock.

        One ``httpx.AsyncClient`` is shared by the transport and the catalogue
        reader instead of one being created per request.
        """

        if self._provider is None:
            self._http = httpx.AsyncClient(
                timeout=self._config.request_timeout_seconds
            )
            self._provider = CompositeProvider(
                pool=CodexPool(self._store, config=self._config),
                codex=CodexTransport(config=self._config, client=self._http),
                fallback=self._fallback,
                resolver=lambda cred: resolve_tier_models(
                    cred, config=self._config, client=self._http
                ),
                catalogue_ttl_seconds=self._config.catalogue_ttl_seconds,
            )
        return self._provider

    def _request(
        self, prompt: Prompt, system: str, model: str | None, **kwargs: Any
    ) -> ChatRequest:
        return ChatRequest(
            model=model or self._model,
            messages=_to_messages(prompt, system),
            **kwargs,
        )

    def _blocking(self, coro: Any, timeout: float | None = None) -> Any:
        """Run a coroutine on the shared loop and block for its result."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise RuntimeError(
                "TidepoolClient's synchronous methods block; inside an event loop "
                "call acomplete()/acomplete_structured()/astream() instead"
            )
        try:
            return self._loop.run(
                coro, self._timeout if timeout is None else timeout
            )
        except (LLMUnavailableError, ValidationError):
            # A reply that does not fit the model is a bad *answer*, not an
            # unavailable backend: callers retry that one with a repair prompt.
            raise
        except TimeoutError as err:
            raise LLMUnavailableError(
                f"tidepool call timed out after {self._timeout}s"
            ) from err
        except Exception as err:  # noqa: BLE001 — callers get one failure mode
            raise LLMUnavailableError(str(err) or type(err).__name__) from err

    # ------------------------------------------------------------------- text

    async def acomplete(
        self,
        messages: Prompt,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Plain-text completion."""

        provider = self._build_provider()
        response = await provider.chat(self._request(messages, system, model, **kwargs))
        return response.content

    def complete(
        self,
        messages: Prompt,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        return self._blocking(
            self.acomplete(messages, system=system, model=model, **kwargs)
        )

    # ------------------------------------------------------------- structured

    async def acomplete_structured(
        self,
        response_model: type[T] | None = None,
        messages: Prompt | None = None,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> T:
        """A reply validated into ``response_model``.

        The schema is enforced by the backend — Responses *strict* mode, the
        wire shape behind the Codex CLI's ``--output-schema`` — not asked for in
        the prompt. Both halves of that mode are handled here: the schema is
        tightened on the way out, and on the way back the ``null``s strict mode
        produces for fields the model had nothing to say about are dropped, so
        pydantic applies their defaults instead of rejecting them.
        """

        if response_model is None or messages is None:
            raise TypeError("complete_structured needs both response_model and messages")

        provider = self._build_provider()
        request = self._request(
            messages,
            system,
            model,
            output_schema=strict_schema(response_model.model_json_schema()),
            **kwargs,
        )
        text = _strip_fences((await provider.chat(request)).content)
        try:
            payload = json.loads(text)
        except ValueError:
            # Hand it to pydantic so an undecodable answer surfaces as a
            # ValidationError like any other bad one; a bespoke exception type
            # would sail past the retry logic that already catches that.
            return response_model.model_validate_json(text)
        return response_model.model_validate(drop_nulls(payload))

    def complete_structured(
        self,
        response_model: type[T] | None = None,
        messages: Prompt | None = None,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> T:
        # Checked before the coroutine exists: a miscall is a bug in the caller,
        # not the backend being unavailable, and _blocking maps everything the
        # coroutine raises onto LLMUnavailableError.
        if response_model is None or messages is None:
            raise TypeError("complete_structured needs both response_model and messages")
        return self._blocking(
            self.acomplete_structured(
                response_model, messages, system=system, model=model, **kwargs
            )
        )

    # ----------------------------------------------------------------- stream

    async def astream(
        self,
        messages: Prompt,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield text deltas as they arrive."""

        iterator = await self._open_stream(
            messages, system=system, model=model, **kwargs
        )
        async for chunk in iterator:
            if chunk.delta:
                yield chunk.delta

    def stream(
        self,
        messages: Prompt,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Blocking generator of text deltas.

        The timeout applies per chunk rather than to the whole answer — a long
        reply that keeps arriving is not a stall.
        """

        iterator = self._blocking(
            self._open_stream(messages, system=system, model=model, **kwargs)
        )
        try:
            while True:
                item = self._blocking(_anext_or_done(iterator))
                if item is _DONE:
                    return
                if item.delta:
                    yield item.delta
        finally:
            self._blocking(iterator.aclose(), timeout=30.0)

    async def _open_stream(
        self,
        messages: Prompt,
        *,
        system: str = "",
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        provider = self._build_provider()
        return await provider.stream(self._request(messages, system, model, **kwargs))

    # ------------------------------------------------------------ operational

    async def astatus(self) -> list[AccountStatus]:
        if self._store is None:
            raise LLMUnavailableError("this client was built from a provider, not a store")
        return await CodexPool(self._store, config=self._config).status()

    def status(self) -> list[AccountStatus]:
        """Per-account health. Silent attrition is the failure mode to watch."""

        return self._blocking(self.astatus(), timeout=30.0)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            self._provider = None

    def close(self) -> None:
        self._blocking(self.aclose(), timeout=30.0)

    def __enter__(self) -> TidepoolClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "TidepoolClient",
    "LLMClient",
    "LLMUnavailableError",
    "Prompt",
]
