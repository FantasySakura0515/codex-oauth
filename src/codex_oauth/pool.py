"""Codex account pool — credential selection, exhaustion, and cooldown.

Owns *which* credential to use and *when* an exhausted one comes back. Performs
no HTTP except token refresh, so every rotation scenario is unit-testable
without a network or a mock server.

Selection is sticky by design: one account is used until it hits its quota,
rather than spreading load. Exhaustion is detected passively — the backend does
not publish quota ceilings, but its 429 body carries an exact ``resets_at``, so
the cooldown is precise rather than guessed.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import httpx

from codex_oauth.config import CodexOAuthConfig
from codex_oauth.store import AccountRecord, AccountStore

logger = logging.getLogger(__name__)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True)
class CodexCredential:
    """One usable credential handed to the transport."""

    account_id: str
    label: str
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class AccountStatus:
    """Pool health, surfaced through readiness probes and admin views."""

    label: str
    account_id: str
    status: str
    cooldown_until: datetime.datetime | None


class CodexRefreshFailed(RuntimeError):
    """The OAuth refresh did not yield a usable access token.

    ``permanent`` separates the two failures callers must treat differently:

    * ``True`` — the auth server actively rejected this credential (400/401/403,
      or the account is not in the pool at all). Retiring it is correct.
    * ``False`` — the exchange did not complete for reasons that say nothing
      about the credential: a 5xx, a network error, an unparseable body. The
      account must be left alone; ``disabled`` is terminal and never returns to
      the rotation on its own, so classifying a blip as permanent would let one
      bad minute at the auth server drain the whole pool.
    """

    def __init__(self, message: str, *, permanent: bool = True) -> None:
        super().__init__(message)
        self.permanent = permanent


def _credential(record: AccountRecord) -> CodexCredential:
    return CodexCredential(
        account_id=record.account_id,
        label=record.label,
        access_token=record.access_token,
        refresh_token=record.refresh_token,
    )


class CodexPool:
    """Sticky, store-backed credential pool."""

    def __init__(self, store: AccountStore, *, config: CodexOAuthConfig | None = None) -> None:
        self._store = store
        self._config = config or CodexOAuthConfig()

    # ------------------------------------------------------------------ helpers

    def _clamp(self, target: datetime.datetime) -> datetime.datetime:
        """Bound a cooldown target to the configured window.

        The ceiling always applies. The floor applies only to future targets —
        it exists to stop a hot retry loop when the 429 body says "resets in
        2s". An *already elapsed* ``resets_at`` means the quota window turned
        over, so it passes through and the next ``acquire`` revives the account
        rather than serving it a freshly manufactured penalty.
        """

        now = utc_now()
        high = now + datetime.timedelta(seconds=self._config.max_cooldown_seconds)
        if target <= now:
            return target
        low = now + datetime.timedelta(seconds=self._config.min_cooldown_seconds)
        return min(max(target, low), high)

    # ------------------------------------------------------------------ protocol

    async def acquire(self) -> CodexCredential | None:
        """Return the account to use, or ``None`` when every one is unavailable.

        Sticky: an existing ``active`` record wins. Otherwise the least
        recently used ``available`` record is promoted. The whole decision runs
        in one store transaction, so two racing callers cannot promote
        different accounts.
        """

        now = utc_now()
        async with self._store.transaction() as accounts:
            # Return expired cooldowns to the pool first so they are candidates.
            for record in accounts.values():
                if (
                    record.status == "cooling"
                    and record.cooldown_until is not None
                    and record.cooldown_until <= now
                ):
                    record.status = "available"
                    record.cooldown_until = None

            active = next((r for r in accounts.values() if r.status == "active"), None)
            if active is not None:
                return _credential(active)

            available = [r for r in accounts.values() if r.status == "available"]
            if not available:
                return None
            # LRU with never-used first — mirrors ``ORDER BY last_used_at ASC
            # NULLS FIRST`` in the original.
            chosen = min(
                available,
                key=lambda r: (r.last_used_at is not None, r.last_used_at or now, r.label),
            )
            chosen.status = "active"
            chosen.last_used_at = now
            logger.warning("codex pool: now using account %s", chosen.label)
            return _credential(chosen)

    async def refresh(
        self,
        account_id: str,
        *,
        stale_access_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> CodexCredential:
        """Exchange the stored refresh token for a fresh access token.

        The backend rotates the refresh token on every use, so a lost write
        strands a credential on a token nobody holds. The store transaction
        deliberately spans the HTTP call to serialize refreshers against that;
        moving the POST outside it would silently drop the serialization.

        Pass ``stale_access_token`` — the token that just failed — to take the
        reuse path: a stored token that no longer matches means another caller
        already rotated, so the fresh credential is returned without burning a
        second rotation. Left ``None``, every caller exchanges unconditionally.
        """

        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=self._config.request_timeout_seconds)
        try:
            async with self._store.transaction() as accounts:
                record = accounts.get(account_id)
                if record is None:
                    raise CodexRefreshFailed(f"unknown codex account {account_id}")

                if stale_access_token is not None and record.access_token != stale_access_token:
                    logger.info(
                        "codex pool: refresh already done by another caller, reusing (%s)",
                        record.label,
                    )
                    return _credential(record)

                current_refresh = record.refresh_token
                try:
                    response = await http.post(
                        f"{self._config.auth_base}/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": self._config.oauth_client_id,
                            "refresh_token": current_refresh,
                        },
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                except httpx.HTTPError as exc:
                    # A dropped connection or a timeout is indistinguishable
                    # from a healthy credential behind a broken link.
                    raise CodexRefreshFailed(
                        f"refresh for {account_id} could not reach the auth server: {exc}",
                        permanent=False,
                    ) from exc
                if response.status_code != 200:
                    # Only an explicit rejection retires the credential. Every
                    # other status (5xx, 429, a proxy's 404) is the auth server
                    # having a bad time, not this account being dead.
                    raise CodexRefreshFailed(
                        f"refresh failed for {account_id}: HTTP {response.status_code}",
                        permanent=response.status_code in (400, 401, 403),
                    )
                # A proxy or captive portal can answer 200 with an HTML error
                # page; that must surface as a refresh failure, not a decode
                # error escaping past callers that only catch CodexRefreshFailed.
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CodexRefreshFailed(
                        f"refresh for {account_id} returned unparseable body",
                        permanent=False,
                    ) from exc
                if not isinstance(payload, dict):
                    raise CodexRefreshFailed(
                        f"refresh for {account_id} returned unparseable body",
                        permanent=False,
                    )

                access = payload.get("access_token")
                if not isinstance(access, str) or not access:
                    # A 200 with no token is the server contradicting itself;
                    # the refresh token it was handed was never rejected.
                    raise CodexRefreshFailed(
                        f"refresh for {account_id} returned no token", permanent=False
                    )

                # The endpoint usually rotates the refresh token; keep the old
                # one when it does not, rather than writing nothing over a
                # working value.
                rotated = payload.get("refresh_token")
                record.access_token = access
                record.refresh_token = (
                    rotated if isinstance(rotated, str) and rotated else current_refresh
                )
                logger.info("codex pool: refreshed account %s", record.label)
                return _credential(record)
        finally:
            if owns_client:
                await http.aclose()

    async def mark_exhausted(
        self, account_id: str, *, resets_at: datetime.datetime | None
    ) -> None:
        """Cool an account down until its quota resets."""

        target = resets_at or (
            utc_now() + datetime.timedelta(seconds=self._config.default_cooldown_seconds)
        )
        until = self._clamp(target)
        async with self._store.transaction() as accounts:
            record = accounts.get(account_id)
            # ``disabled`` is terminal. A 429 from a request that was already
            # in flight when the account was retired must not move it back to
            # ``cooling``, which ``acquire`` would revive after the cooldown.
            if record is None or record.status == "disabled":
                return
            record.status = "cooling"
            record.cooldown_until = until
            record.last_error = "usage_limit_reached"
        logger.warning("codex pool: account %s exhausted, cooling until %s", account_id, until)

    async def mark_unauthorized(self, account_id: str, *, reason: str) -> None:
        """Retire a credential that refresh could not rescue."""

        async with self._store.transaction() as accounts:
            record = accounts.get(account_id)
            if record is None:
                return
            record.status = "disabled"
            record.last_error = reason
        logger.warning("codex pool: account %s disabled (%s)", account_id, reason)

    async def status(self) -> list[AccountStatus]:
        """Full pool report. Silent attrition is the failure mode to watch."""

        async with self._store.transaction() as accounts:
            return [
                AccountStatus(
                    label=r.label,
                    account_id=r.account_id,
                    status=r.status,
                    cooldown_until=r.cooldown_until,
                )
                for r in sorted(accounts.values(), key=lambda r: r.label)
            ]


__all__ = [
    "AccountStatus",
    "CodexCredential",
    "CodexPool",
    "CodexRefreshFailed",
    "utc_now",
]
