"""Credential persistence behind a transaction protocol.

Rotation leans on two guarantees: one ``active`` account promoted atomically,
and a refresh serialized so two workers never both spend the one-shot refresh
token. A store provides them through a single primitive — an async transaction
yielding every account record, committed atomically on clean exit and discarded
on exception.

:class:`JsonFileStore` is the built-in implementation: an ``asyncio.Lock``
around a JSON file, held for the whole transaction including the refresh HTTP
call. That is real serialization **within one process**. Multi-worker
deployments need a DB-backed store implementing the same protocol, where
``SELECT ... FOR UPDATE`` and a partial unique index on ``active`` do the same
job across processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Account lifecycle: available -> active (in use, sticky) -> cooling (429, comes
# back at cooldown_until) / disabled (terminal; only a re-import revives it).
STATUSES = ("available", "active", "cooling", "disabled")


@dataclass
class AccountRecord:
    """One ChatGPT-subscription credential, keyed by ``account_id``."""

    account_id: str
    label: str
    access_token: str
    refresh_token: str
    status: str = "available"
    cooldown_until: datetime.datetime | None = None
    last_used_at: datetime.datetime | None = None
    last_error: str | None = None


class AccountStore(Protocol):
    """The one primitive the pool needs.

    ``transaction()`` is an async context manager yielding a mutable mapping of
    ``account_id -> AccountRecord``. Mutations (including adding or deleting
    keys) are committed atomically when the block exits cleanly and discarded
    when it raises. Implementations must serialize transactions — the pool's
    correctness (single active account, one refresh at a time) rests on it.
    """

    def transaction(
        self,
    ) -> contextlib.AbstractAsyncContextManager[MutableMapping[str, AccountRecord]]: ...


def _to_json(record: AccountRecord) -> dict:
    return {
        "account_id": record.account_id,
        "label": record.label,
        "access_token": record.access_token,
        "refresh_token": record.refresh_token,
        "status": record.status,
        "cooldown_until": record.cooldown_until.isoformat() if record.cooldown_until else None,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "last_error": record.last_error,
    }


def _parse_dt(raw: object) -> datetime.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Stored values are always aware; tolerate a hand-edited naive one as UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def _from_json(raw: dict) -> AccountRecord | None:
    account_id = raw.get("account_id")
    access = raw.get("access_token")
    refresh = raw.get("refresh_token")
    if not all(isinstance(v, str) and v for v in (account_id, access, refresh)):
        return None
    status = raw.get("status")
    return AccountRecord(
        account_id=str(account_id),
        label=str(raw.get("label") or account_id),
        access_token=str(access),
        refresh_token=str(refresh),
        status=status if status in STATUSES else "available",
        cooldown_until=_parse_dt(raw.get("cooldown_until")),
        last_used_at=_parse_dt(raw.get("last_used_at")),
        last_error=raw.get("last_error") if isinstance(raw.get("last_error"), str) else None,
    )


@dataclass
class JsonFileStore:
    """Single-process store: an ``asyncio.Lock`` around a JSON file.

    The file holds bearer and refresh tokens in the clear — treat it like a
    private key. It is created with owner-only permissions where the platform
    supports that (POSIX); on Windows the containing directory's ACL is the
    protection, same as ``~/.codex/auth.json`` itself.
    """

    path: Path
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _load(self) -> dict[str, AccountRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # An unreadable store must not silently become an empty pool that a
            # later commit overwrites: that would destroy every credential.
            raise RuntimeError(f"codex-pool store {self.path} is unreadable") from None
        accounts = {}
        for raw in payload.get("accounts", []) if isinstance(payload, dict) else []:
            record = _from_json(raw) if isinstance(raw, dict) else None
            if record is not None:
                accounts[record.account_id] = record
        return accounts

    def _save(self, accounts: MutableMapping[str, AccountRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {"accounts": [_to_json(r) for r in accounts.values()]},
            ensure_ascii=False,
            indent=2,
        )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        with contextlib.suppress(OSError):  # POSIX only; harmless no-op on Windows
            tmp.chmod(0o600)
        tmp.replace(self.path)

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[MutableMapping[str, AccountRecord]]:
        async with self._lock:
            accounts = self._load()
            yield accounts
            # Reached only on clean exit — an exception skips the save, which
            # is the rollback.
            self._save(accounts)


__all__ = ["AccountRecord", "AccountStore", "JsonFileStore", "STATUSES"]
