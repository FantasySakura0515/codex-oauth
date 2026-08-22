"""Import ``codex login`` credential directories into the account pool.

Run once per credential directory, produced by::

    CODEX_HOME=~/.tidepool-accounts/acct-a codex login
    CODEX_HOME=~/.tidepool-accounts/acct-b codex login

The directories are a bootstrap source only. After import the pool owns refresh
and never reads or writes them again — the backend rotates refresh tokens on
every use, so two writers would eventually invalidate each other. The
directories can be deleted afterwards, and deployment targets need no codex CLI.

⚠️ Do **not** import a ``CODEX_HOME`` the codex CLI still uses day-to-day
(e.g. the default ``~/.codex``): the first refresh the pool performs rotates
the refresh token and strands the CLI's copy. Dedicate a directory per account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from tidepool.pool import utc_now
from tidepool.store import AccountRecord, AccountStore, JsonFileStore

logger = logging.getLogger(__name__)


def _read_auth(home: Path) -> tuple[str, str, str] | None:
    """Return ``(account_id, access_token, refresh_token)`` or ``None``."""

    auth_file = home / "auth.json"
    if not auth_file.is_file():
        logger.warning("codex import: %s has no auth.json, skipping", home)
        return None
    try:
        payload = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("codex import: %s unreadable (%s), skipping", auth_file, exc)
        return None

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        logger.warning("codex import: %s has no tokens block, skipping", auth_file)
        return None
    account_id = tokens.get("account_id")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not all(isinstance(v, str) and v for v in (account_id, access, refresh)):
        logger.warning("codex import: %s is missing token fields, skipping", auth_file)
        return None
    return str(account_id), str(access), str(refresh)


async def import_credentials(dirs: list[Path], store: AccountStore) -> list[str]:
    """Upsert every readable credential directory. Returns imported labels.

    Exactly one label is appended per accepted directory and unreadable ones
    are skipped with a warning, so ``len(dirs) - len(result)`` is the skip
    count — which is what :func:`main` reports and exits on.
    """

    imported: list[str] = []
    async with store.transaction() as accounts:
        for home in dirs:
            parsed = _read_auth(home)
            if parsed is None:
                continue
            account_id, access, refresh = parsed
            label = home.name

            record = accounts.get(account_id)
            if record is None:
                accounts[account_id] = AccountRecord(
                    account_id=account_id,
                    label=label,
                    access_token=access,
                    refresh_token=refresh,
                    status="available",
                )
            else:
                # Re-importing is the documented way to rescue a disabled
                # account, so this must clear the failure state too.
                record.label = label
                record.access_token = access
                record.refresh_token = refresh
                record.status = "available"
                record.cooldown_until = None
                record.last_error = None
                record.last_used_at = record.last_used_at or utc_now()
            imported.append(label)
    return imported


def main() -> None:
    """Console entry point (``tidepool-import``)."""

    parser = argparse.ArgumentParser(
        description="Import codex CLI credential directories into the account pool."
    )
    parser.add_argument("--store", required=True, help="Path to the pool's JSON store file.")
    parser.add_argument("dirs", nargs="+", help="CODEX_HOME directories to import.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    dirs = [Path(d).expanduser() for d in args.dirs]
    store = JsonFileStore(Path(args.store).expanduser())
    labels = asyncio.run(import_credentials(dirs, store))
    skipped = len(dirs) - len(labels)
    print(f"imported {len(labels)}, skipped {skipped}: {', '.join(labels) or '-'}")

    # A skip is never fatal mid-run — one unreadable directory must not strand
    # the others. But when *nothing* landed there is no pool, and exiting 0
    # would tell a deploy script that provisioning succeeded.
    if skipped and not labels:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
