"""Pool configuration as a plain dataclass — no settings framework required.

Values come from wherever the embedding application wants: env vars, a config
file, or the defaults below. ``CodexPoolClient.from_env`` fills one of these
from the ``CODEX_POOL_*`` variables.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodexPoolConfig:
    """ChatGPT-subscription Codex backend + account pool settings."""

    # The backend gates which models an account may use on this value and
    # rejects /models outright without it. Keep in step with the codex CLI that
    # produced the credentials.
    client_version: str = "0.147.0"
    # Used only when a 429 body carries no parseable resets_at.
    default_cooldown_seconds: float = 18000.0
    # Clamp on any cooldown, however derived. The upper bound is 30 days
    # because observed resets_in_seconds reached ~6.5 days; a 24h ceiling would
    # return still-throttled accounts to the pool early.
    min_cooldown_seconds: float = 60.0
    max_cooldown_seconds: float = 2592000.0
    # How long a resolved tier->model map is trusted before the live catalogue
    # is read again. The backend ships a new generation every few weeks, so a
    # long-running service that never re-reads stays a generation behind until
    # it restarts. 0 or less pins the first answer for the process lifetime.
    catalogue_ttl_seconds: float = 86400.0
    api_base: str = "https://chatgpt.com/backend-api/codex"
    auth_base: str = "https://auth.openai.com"
    # The Codex CLI's public OAuth client id — credentials imported from
    # ``codex login`` were issued against it, so refresh must use it too.
    oauth_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    request_timeout_seconds: float = 120.0


__all__ = ["CodexPoolConfig"]
