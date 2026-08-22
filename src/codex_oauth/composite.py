"""Composite provider — pool rotation wrapped around the single-credential transport.

``fallback`` is optional: with one configured, pool exhaustion degrades to it;
without one, :class:`CodexPoolDryError` is raised and the caller decides what
"the pool is dry" means.

Rotation is an internal retry invisible to callers — one logical call, however
many accounts it walks through. Callers with their own retry budgets must not
see rotation consume them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from codex_oauth.pool import CodexRefreshFailed
from codex_oauth.transport import CodexAuthExpiredError, CodexUsageLimitError
from codex_oauth.types import ChatRequest, ChatResponse, StreamChunk

logger = logging.getLogger(__name__)

#: One day. The backend ships a new generation every few weeks, so a service
#: that never restarts would otherwise stay a generation behind forever.
DEFAULT_CATALOGUE_TTL_SECONDS = 86400.0


def _monotonic() -> float:
    """Indirection so tests can move the clock without sleeping."""

    return time.monotonic()


class CodexPoolDryError(RuntimeError):
    """Every account is cooling or disabled and no fallback is configured."""


class _Pool(Protocol):
    async def acquire(self) -> Any: ...
    async def mark_exhausted(self, account_id: str, *, resets_at: Any) -> None: ...
    async def mark_unauthorized(self, account_id: str, *, reason: str) -> None: ...
    async def refresh(
        self, account_id: str, *, stale_access_token: str | None = None
    ) -> Any: ...


class _Codex(Protocol):
    async def chat(self, req: ChatRequest, cred: Any, *, model_id: str) -> ChatResponse: ...

    # Not ``async def``: the transport's ``stream`` is an async *generator*, so
    # calling it yields the iterator directly rather than a coroutine.
    def stream(
        self, req: ChatRequest, cred: Any, *, model_id: str
    ) -> AsyncIterator[StreamChunk]: ...


class _Fallback(Protocol):
    async def chat(self, req: ChatRequest) -> ChatResponse: ...
    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]: ...


class _Resolver(Protocol):
    """Reads the live tier map for a credential (``codex_oauth.catalogue``)."""

    async def __call__(self, cred: Any, /) -> dict[str, str]: ...


class CompositeProvider:
    """Chat over a Codex pool, with optional degradation to a fallback provider."""

    provider_name = "codex-oauth"

    def __init__(
        self,
        *,
        pool: _Pool,
        codex: _Codex,
        fallback: _Fallback | None = None,
        tier_models: dict[str, str] | None = None,
        resolver: _Resolver | None = None,
        catalogue_ttl_seconds: float = DEFAULT_CATALOGUE_TTL_SECONDS,
    ) -> None:
        self._pool = pool
        self._codex = codex
        self._fallback = fallback
        # The pinned ids. Correct only until the backend ships its next
        # generation, which it does every few weeks — see ``_tier_models``.
        self._pinned = dict(tier_models or {})
        self._resolver = resolver
        # No resolver → the pinned map *is* the answer, resolved from the
        # start. Tests take this path so none of them need a network.
        self._resolved: dict[str, str] | None = None if resolver else self._pinned
        self._resolved_at: float | None = None
        self._ttl = catalogue_ttl_seconds
        self._resolve_lock = asyncio.Lock()

    def _needs_resolving(self) -> bool:
        if self._resolved is None:
            return True
        if self._resolver is None or self._ttl <= 0:
            # Nothing to ask, or re-resolution deliberately switched off.
            return False
        return (
            self._resolved_at is None
            or _monotonic() - self._resolved_at >= self._ttl
        )

    async def _tier_models(self, cred: Any) -> dict[str, str]:
        """Tier→model map, re-read from the live catalogue every ``ttl``.

        Pinned ids rot — the backend retires slugs every few weeks and a dead
        id 400s every request — so the live catalogue is the real source and
        the pins are only a floor. A service that runs for months has to pick
        up new generations on its own, which is what the TTL is for; it
        defaults to a day.

        Resolution happens after a credential exists (the endpoint is
        authenticated) and the answer is cached whether it succeeded or not: a
        request must never fail, or pay an extra doomed round trip, because
        catalogue resolution did. A refresh that fails keeps the map already in
        use rather than falling back to the pins — the previous answer came
        from the live catalogue and is the better of the two.
        """

        if not self._needs_resolving():
            return self._resolved  # type: ignore[return-value]
        async with self._resolve_lock:
            # Re-check: everything queued behind the lock while one caller
            # resolved is already served by that result.
            if not self._needs_resolving():
                return self._resolved  # type: ignore[return-value]
            resolver = self._resolver
            if resolver is None:  # pragma: no cover — implied by _resolved is None
                self._resolved = self._pinned
                return self._resolved
            previous = self._resolved
            try:
                resolved = await resolver(cred)
            except Exception as exc:  # noqa: BLE001 — never fail a request over this
                logger.warning("codex catalogue unresolved (%s)", exc)
                resolved = {}
            if resolved:
                # Fill any tier the catalogue did not cover, so a partial
                # answer cannot leave a tier mapping to its own name.
                current = {**self._pinned, **resolved}
            else:
                current = previous if previous is not None else dict(self._pinned)
            # Stamped even on failure, so a broken catalogue is retried on the
            # next TTL rather than on every request.
            self._resolved_at = _monotonic()
            if previous is None:
                logger.info("codex tier models resolved: %s", current)
            elif current != previous:
                # Worth a warning: every log line above this one names the old
                # generation.
                logger.warning("codex tier models changed: %s -> %s", previous, current)
            self._resolved = current
        return self._resolved

    @staticmethod
    def _model_for(req: ChatRequest, tier_models: dict[str, str]) -> str:
        # A tier name resolves through the map; a concrete slug passes through.
        return tier_models.get(req.model, req.model)

    async def _next_credential(self, tried: set[str]) -> Any:
        cred = await self._pool.acquire()
        if cred is None or cred.account_id in tried:
            return None
        return cred

    # ------------------------------------------------------------------ chat

    async def chat(self, req: ChatRequest) -> ChatResponse:
        tried: set[str] = set()

        while True:
            cred = await self._next_credential(tried)
            if cred is None:
                if self._fallback is not None:
                    logger.warning("codex pool dry; falling back for chat")
                    return await self._fallback.chat(req)
                raise CodexPoolDryError("every codex account is cooling or disabled")
            tried.add(cred.account_id)
            # After acquire, not before: the catalogue endpoint is authenticated.
            model_id = self._model_for(req, await self._tier_models(cred))

            try:
                return await self._codex.chat(req, cred, model_id=model_id)
            except CodexUsageLimitError as exc:
                await self._pool.mark_exhausted(cred.account_id, resets_at=exc.resets_at)
            except CodexAuthExpiredError:
                refreshed = await self._refresh_or_disable(cred)
                if refreshed is None:
                    continue
                try:
                    return await self._codex.chat(req, refreshed, model_id=model_id)
                except CodexAuthExpiredError:
                    await self._pool.mark_unauthorized(
                        cred.account_id, reason="token rejected after refresh"
                    )
                except CodexUsageLimitError as exc:
                    await self._pool.mark_exhausted(
                        cred.account_id, resets_at=exc.resets_at
                    )

    async def _refresh_or_disable(self, cred: Any) -> Any:
        """Try one refresh; disable the account only when it is truly dead.

        The failed token is passed as ``stale_access_token`` so the pool can
        take the reuse path when a racing caller already rotated this account.

        ``mark_unauthorized`` is terminal — only a re-import revives a
        ``disabled`` row — so it is reserved for a :class:`CodexRefreshFailed`
        marked ``permanent``. A transient failure, or any exception nobody
        classified, leaves the account untouched for the next request to retry:
        retiring on an unclassified error is how one network blip permanently
        burns a credential.
        """

        try:
            return await self._pool.refresh(
                cred.account_id, stale_access_token=cred.access_token
            )
        except CodexRefreshFailed as exc:
            if not exc.permanent:
                logger.warning(
                    "codex refresh for %s failed transiently (%s); leaving it active",
                    cred.account_id,
                    exc,
                )
                return None
            await self._pool.mark_unauthorized(
                cred.account_id, reason=f"refresh failed: {exc}"
            )
            return None
        except Exception as exc:  # noqa: BLE001 — unclassified: never retire on it
            logger.warning(
                "codex refresh for %s raised an unclassified error (%r); leaving it active",
                cred.account_id,
                exc,
            )
            return None

    # ---------------------------------------------------------------- stream

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Return the chunk iterator.

        Coroutine-returning-an-iterator, not an async generator: callers do
        ``async for chunk in await provider.stream(req)``. Rotation happens on
        the first ``__anext__``, inside the caller's ``try``.
        """

        return self._rotating_stream(req)

    async def _rotating_stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        tried: set[str] = set()

        while True:
            cred = await self._next_credential(tried)
            if cred is None:
                if self._fallback is not None:
                    logger.warning("codex pool dry; falling back for stream")
                    async for chunk in await self._fallback.stream(req):
                        yield chunk
                    return
                raise CodexPoolDryError("every codex account is cooling or disabled")
            tried.add(cred.account_id)
            # After acquire, not before: the catalogue endpoint is authenticated.
            model_id = self._model_for(req, await self._tier_models(cred))

            emitted = False
            refreshed_once = False
            candidate = cred
            while True:
                try:
                    # Only the two account-scoped errors are caught. A bare
                    # ``CodexTransportError`` — which the transport also raises
                    # for a truncated or ``response.failed`` stream, possibly
                    # *after* chunks — says nothing bad about this credential, so
                    # rotating on it would retire a healthy account and pay twice
                    # for a backend hiccup. It propagates in both positions.
                    async for chunk in self._codex.stream(
                        req, candidate, model_id=model_id
                    ):
                        emitted = True
                        yield chunk
                    return
                except CodexUsageLimitError as exc:
                    await self._pool.mark_exhausted(
                        cred.account_id, resets_at=exc.resets_at
                    )
                    if emitted:
                        # Chunks already reached the client; re-running would
                        # make the visible answer restart. Surface the error
                        # instead. The account is cooled, so the next request
                        # picks another.
                        raise
                    break
                except CodexAuthExpiredError:
                    if not emitted and not refreshed_once:
                        # An expired access token is the routine case, not a
                        # dead account. Retrying the same credential before any
                        # chunk is exactly as safe as rotating to another one,
                        # and skipping the refresh would retire the whole pool
                        # on ordinary token expiry.
                        renewed = await self._refresh_or_disable(cred)
                        if renewed is None:
                            break
                        refreshed_once = True
                        candidate = renewed
                        continue
                    await self._pool.mark_unauthorized(
                        cred.account_id,
                        reason=(
                            "token rejected after refresh"
                            if refreshed_once
                            else "token rejected mid-stream"
                        ),
                    )
                    if emitted:
                        raise
                    break


__all__ = [
    "DEFAULT_CATALOGUE_TTL_SECONDS",
    "CodexPoolDryError",
    "CompositeProvider",
]
