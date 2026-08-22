import json

import httpx
from conftest import account, seed

from codex_pool.catalogue import (
    FALLBACK_TIER_MODELS,
    fetch_models,
    resolve_tier_models,
    select_tier_models,
)
from codex_pool.config import CodexPoolConfig
from codex_pool.import_cli import import_credentials
from codex_pool.pool import CodexCredential, CodexPool

CRED = CodexCredential(account_id="acct", label="a", access_token="tok", refresh_token="r")


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------- catalogue


def test_select_tier_models_three_or_more():
    tiers = select_tier_models([("flagship", 1), ("middle", 2), ("cheap", 3)])
    assert tiers == {"strong": "flagship", "balanced": "middle", "fast": "cheap"}


def test_select_tier_models_collapses_balanced_downward():
    # Two models: balanced must not silently route ordinary traffic at flagship cost.
    tiers = select_tier_models([("flagship", 1), ("cheap", 2)])
    assert tiers["balanced"] == "cheap"


def test_select_tier_models_empty_returns_fallback():
    assert select_tier_models([]) == FALLBACK_TIER_MODELS


def test_fast_prefers_a_small_variant_over_the_cheapest_slot():
    # The real catalogue (2026-08) ends with codex-auto-review at priority 43 —
    # cheapest by priority, but built for one Codex feature, not for chat.
    tiers = select_tier_models(
        [
            ("gpt-5.6-sol", 1),
            ("gpt-5.6-terra", 2),
            ("gpt-5.4-mini", 23),
            ("codex-auto-review", 43),
        ]
    )
    assert tiers == {
        "strong": "gpt-5.6-sol",
        "balanced": "gpt-5.6-terra",
        "fast": "gpt-5.4-mini",
    }


def test_non_chat_models_are_never_selected():
    tiers = select_tier_models([("flagship", 1), ("codex-auto-review", 9)])
    assert "codex-auto-review" not in tiers.values()


def test_catalogue_of_only_non_chat_models_falls_back():
    assert select_tier_models([("codex-auto-review", 1)]) == FALLBACK_TIER_MODELS


def test_balanced_is_never_the_same_as_fast_when_a_middle_exists():
    tiers = select_tier_models([("a", 1), ("b", 2), ("c-mini", 3), ("d", 4)])
    assert tiers["fast"] == "c-mini"
    assert tiers["balanced"] == "b"


async def test_fetch_models_parses_catalogue():
    payload = {"models": [{"slug": "a", "priority": 1}, {"slug": "b", "priority": 2}]}
    models = await fetch_models(
        CRED, client=client_for(lambda r: httpx.Response(200, json=payload))
    )
    assert [m.slug for m in models] == ["a", "b"]


async def test_fetch_models_skips_malformed_entries_not_whole_catalogue():
    payload = {"models": [{"slug": "good", "priority": 1}, {"no_slug": True}, "junk"]}
    models = await fetch_models(
        CRED, client=client_for(lambda r: httpx.Response(200, json=payload))
    )
    assert [m.slug for m in models] == ["good"]


async def test_fetch_models_http_error_returns_empty():
    models = await fetch_models(CRED, client=client_for(lambda r: httpx.Response(500)))
    assert models == []


async def test_fetch_models_non_object_body_returns_empty():
    models = await fetch_models(CRED, client=client_for(lambda r: httpx.Response(200, json=["x"])))
    assert models == []


async def test_resolve_tier_models_falls_back_when_unreachable():
    tiers = await resolve_tier_models(CRED, client=client_for(lambda r: httpx.Response(503)))
    assert tiers == FALLBACK_TIER_MODELS


async def test_catalogue_request_carries_client_version():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"models": []})

    config = CodexPoolConfig(client_version="9.9.9")
    await fetch_models(CRED, config=config, client=client_for(handler))
    assert seen["params"]["client_version"] == "9.9.9"


# ----------------------------------------------------------------- import


def write_auth(home, account_id="acct-1", access="a", refresh="r"):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": account_id,
                    "access_token": access,
                    "refresh_token": refresh,
                }
            }
        ),
        encoding="utf-8",
    )
    return home


async def test_import_adds_account(tmp_path, store):
    home = write_auth(tmp_path / "acct-a")
    labels = await import_credentials([home], store)
    assert labels == ["acct-a"]
    [status] = await CodexPool(store).status()
    assert status.account_id == "acct-1"
    assert status.status == "available"


async def test_reimport_rescues_disabled_account(tmp_path, store):
    await seed(store, account("acct-1", status="disabled", last_error="dead"))
    home = write_auth(tmp_path / "acct-a", access="fresh-token")
    await import_credentials([home], store)
    [status] = await CodexPool(store).status()
    assert status.status == "available"  # failure state cleared


async def test_import_skips_unreadable_directory(tmp_path, store):
    good = write_auth(tmp_path / "good", account_id="acct-good")
    missing = tmp_path / "missing"
    missing.mkdir()
    labels = await import_credentials([good, missing], store)
    assert labels == ["good"]  # one bad directory must not strand the others


async def test_import_skips_incomplete_tokens(tmp_path, store):
    home = tmp_path / "partial"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": "x"}}), encoding="utf-8")
    assert await import_credentials([home], store) == []


async def test_import_is_idempotent(tmp_path, store):
    home = write_auth(tmp_path / "acct-a")
    await import_credentials([home], store)
    await import_credentials([home], store)
    assert len(await CodexPool(store).status()) == 1
