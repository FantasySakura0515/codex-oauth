import datetime

import pytest

from codex_oauth.pool import CodexPool
from codex_oauth.store import AccountRecord, JsonFileStore


@pytest.fixture
def store(tmp_path):
    return JsonFileStore(tmp_path / "accounts.json")


async def seed(store, *records: AccountRecord) -> None:
    async with store.transaction() as accounts:
        for record in records:
            accounts[record.account_id] = record


def account(account_id: str, **kwargs) -> AccountRecord:
    return AccountRecord(
        account_id=account_id,
        label=kwargs.pop("label", account_id),
        access_token=kwargs.pop("access_token", f"access-{account_id}"),
        refresh_token=kwargs.pop("refresh_token", f"refresh-{account_id}"),
        **kwargs,
    )


@pytest.fixture
def make_pool(store):
    def _make(config=None):
        return CodexPool(store, config=config)

    return _make


def past(seconds: float) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds)


def future(seconds: float) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=seconds)
