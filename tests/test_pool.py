import datetime

import httpx
import pytest

from codex_pool.config import CodexPoolConfig
from codex_pool.pool import CodexRefreshFailed

from conftest import account, future, past, seed


async def test_acquire_returns_none_when_empty(make_pool):
    assert await make_pool().acquire() is None


async def test_acquire_promotes_lru_available(make_pool, store):
    await seed(
        store,
        account("a", last_used_at=past(10)),
        account("b", last_used_at=past(100)),  # least recently used
    )
    cred = await make_pool().acquire()
    assert cred.account_id == "b"


async def test_acquire_is_sticky(make_pool, store):
    await seed(store, account("a"), account("b"))
    pool = make_pool()
    first = await pool.acquire()
    second = await pool.acquire()
    assert first.account_id == second.account_id  # same active account reused


async def test_never_used_account_wins_over_used(make_pool, store):
    await seed(store, account("used", last_used_at=past(5)), account("fresh"))
    cred = await make_pool().acquire()
    assert cred.account_id == "fresh"  # NULLS FIRST


async def test_expired_cooldown_revived_on_acquire(make_pool, store):
    await seed(store, account("a", status="cooling", cooldown_until=past(1)))
    cred = await make_pool().acquire()
    assert cred.account_id == "a"


async def test_future_cooldown_not_acquired(make_pool, store):
    await seed(store, account("a", status="cooling", cooldown_until=future(3600)))
    assert await make_pool().acquire() is None


async def test_disabled_never_acquired(make_pool, store):
    await seed(store, account("a", status="disabled"))
    assert await make_pool().acquire() is None


async def test_mark_exhausted_uses_resets_at(make_pool, store):
    await seed(store, account("a", status="active"))
    pool = make_pool()
    resets = future(7200)
    await pool.mark_exhausted("a", resets_at=resets)
    [status] = await pool.status()
    assert status.status == "cooling"
    assert status.cooldown_until is not None


async def test_mark_exhausted_clamps_to_min(make_pool, store):
    await seed(store, account("a", status="active"))
    config = CodexPoolConfig(min_cooldown_seconds=300, max_cooldown_seconds=99999)
    pool = make_pool(config)
    await pool.mark_exhausted("a", resets_at=future(2))  # below floor
    [status] = await pool.status()
    delta = (status.cooldown_until - datetime.datetime.now(datetime.UTC)).total_seconds()
    assert delta >= 250  # floored up to ~300, not left at 2


async def test_mark_exhausted_passes_through_elapsed_resets(make_pool, store):
    await seed(store, account("a", status="active"))
    config = CodexPoolConfig(min_cooldown_seconds=300)
    pool = make_pool(config)
    # resets_at already in the past → quota window turned over, don't manufacture a penalty
    await pool.mark_exhausted("a", resets_at=past(5))
    cred = await pool.acquire()  # should be immediately revivable
    assert cred is not None


async def test_mark_exhausted_ignores_disabled(make_pool, store):
    await seed(store, account("a", status="disabled"))
    pool = make_pool()
    await pool.mark_exhausted("a", resets_at=future(60))
    [status] = await pool.status()
    assert status.status == "disabled"  # terminal, not moved to cooling


async def test_mark_unauthorized_is_terminal(make_pool, store):
    await seed(store, account("a", status="active"))
    pool = make_pool()
    await pool.mark_unauthorized("a", reason="token rejected")
    [status] = await pool.status()
    assert status.status == "disabled"


async def test_refresh_success_rotates_tokens(make_pool, store):
    await seed(store, account("a", status="active"))

    def handler(request):
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cred = await make_pool().refresh("a", client=client)
    assert cred.access_token == "new-access"
    assert cred.refresh_token == "new-refresh"


async def test_refresh_keeps_old_refresh_when_not_rotated(make_pool, store):
    await seed(store, account("a", refresh_token="original", status="active"))

    def handler(request):
        return httpx.Response(200, json={"access_token": "new-access"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cred = await make_pool().refresh("a", client=client)
    assert cred.refresh_token == "original"  # not overwritten with None


async def test_refresh_401_is_permanent(make_pool, store):
    await seed(store, account("a", status="active"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(CodexRefreshFailed) as exc:
        await make_pool().refresh("a", client=client)
    assert exc.value.permanent is True


async def test_refresh_500_is_transient(make_pool, store):
    await seed(store, account("a", status="active"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(CodexRefreshFailed) as exc:
        await make_pool().refresh("a", client=client)
    assert exc.value.permanent is False


async def test_refresh_network_error_is_transient(make_pool, store):
    await seed(store, account("a", status="active"))

    def boom(request):
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(CodexRefreshFailed) as exc:
        await make_pool().refresh("a", client=client)
    assert exc.value.permanent is False


async def test_refresh_reuse_path_skips_exchange(make_pool, store):
    # Stored token differs from the stale one → another worker already rotated.
    await seed(store, account("a", access_token="already-rotated", status="active"))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "x", "refresh_token": "y"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cred = await make_pool().refresh("a", stale_access_token="old-stale", client=client)
    assert calls["n"] == 0  # no network exchange
    assert cred.access_token == "already-rotated"


async def test_refresh_unknown_account_raises_permanent(make_pool):
    with pytest.raises(CodexRefreshFailed) as exc:
        await make_pool().refresh("ghost")
    assert exc.value.permanent is True


async def test_refresh_200_without_token_is_transient(make_pool, store):
    await seed(store, account("a", status="active"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(CodexRefreshFailed) as exc:
        await make_pool().refresh("a", client=client)
    assert exc.value.permanent is False


async def test_status_persists_across_instances(make_pool, store):
    await seed(store, account("a", status="active"))
    await make_pool().mark_unauthorized("a", reason="dead")
    # A brand-new pool over the same store sees the committed state.
    [status] = await make_pool().status()
    assert status.status == "disabled"
