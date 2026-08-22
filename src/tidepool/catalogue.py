"""Live Codex model catalogue.

``GET /models`` reports the currently-servable models with a priority ordering, so tier mapping is
resolved at runtime instead of pinned in source. The endpoint requires a
``client_version`` query parameter and uses it to decide which models the
caller may see — an old value silently hides new generations rather than
erroring.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from tidepool.config import TidepoolConfig

if TYPE_CHECKING:  # circular at runtime: pool imports nothing from here
    from tidepool.pool import CodexCredential

logger = logging.getLogger(__name__)

_LEADING_DIGITS = re.compile(r"\d+")

# Last-resort ids, used only when GET /models cannot be reached. Expect these to
# rot; the runtime catalogue is the real source. Last checked 2026-08-22 against
# client_version 0.147.0.
FALLBACK_TIER_MODELS: dict[str, str] = {
    "fast": "gpt-5.4-mini",
    "balanced": "gpt-5.6-terra",
    "strong": "gpt-5.6-sol",
}

# The catalogue also lists models built for one Codex feature rather than for
# general chat. They sort to the cheap end on priority, so without this the
# ``fast`` tier would quietly route ordinary prompts to a code-review model.
_NON_CHAT_MARKERS = ("auto-review",)

# OpenAI's naming for the small, cheap variant of a generation. Picking ``fast``
# by name rather than by "last in the list" survives the catalogue growing a
# tail entry that is cheap but not a chat model.
_SMALL_MARKERS = ("-mini", "-nano")


@dataclass(frozen=True)
class CodexModel:
    """One entry of the live catalogue."""

    slug: str
    display_name: str
    priority: int
    context_window: int
    minimal_client_version: str


def backend_headers(cred: CodexCredential, config: TidepoolConfig) -> dict[str, str]:
    """Headers every Codex backend call needs.

    Shared with the transport: the backend rejects or degrades requests that
    omit the originator/version identity.
    """

    return {
        "Authorization": f"Bearer {cred.access_token}",
        "chatgpt-account-id": cred.account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "version": config.client_version,
    }


def select_tier_models(slugs_by_priority: list[tuple[str, int]]) -> dict[str, str]:
    """Map the live catalogue onto ``fast``/``balanced``/``strong``.

    ``slugs_by_priority`` is ``(slug, priority)`` as reported by ``GET /models``,
    where priority 1 is the flagship and larger numbers are progressively
    cheaper/faster.

    ``strong`` takes the flagship. ``fast`` prefers a ``-mini``/``-nano`` slug —
    the cheap end of the list is not automatically a chat model, and the
    catalogue really does carry feature-specific entries down there — falling
    back to the cheapest remaining one when the generation has no small
    variant. ``balanced`` takes the next-best priority after the flagship, or
    collapses onto ``fast`` when the catalogue is too small to have a middle:
    downward rather than onto the flagship, so a two-model catalogue does not
    silently route ordinary traffic at flagship cost.

    Returns :data:`FALLBACK_TIER_MODELS` unchanged for an empty catalogue so
    the caller always gets a complete mapping.
    """

    ordered = [
        slug
        for slug, _ in sorted(slugs_by_priority, key=lambda item: item[1])
        if not any(marker in slug for marker in _NON_CHAT_MARKERS)
    ]
    if not ordered:
        return dict(FALLBACK_TIER_MODELS)

    strong = ordered[0]
    small = [slug for slug in ordered if any(m in slug for m in _SMALL_MARKERS)]
    fast = small[0] if small else ordered[-1]
    # ``balanced`` must not be whatever ``fast`` already is, or the middle tier
    # stops being a middle.
    remaining = [slug for slug in ordered if slug != fast]
    balanced = remaining[1] if len(remaining) > 1 else fast
    return {"fast": fast, "balanced": balanced, "strong": strong}


async def fetch_models(
    cred: CodexCredential,
    *,
    config: TidepoolConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[CodexModel]:
    """Return the catalogue, or an empty list when it cannot be read."""

    cfg = config or TidepoolConfig()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=cfg.request_timeout_seconds)
    try:
        response = await http.get(
            f"{cfg.api_base}/models",
            headers=backend_headers(cred, cfg),
            params={"client_version": cfg.client_version},
        )
        if response.status_code != 200:
            logger.warning(
                "codex catalogue: HTTP %s, falling back to pinned ids",
                response.status_code,
            )
            return []
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("codex catalogue: unreadable (%s)", exc)
        return []
    finally:
        if owns_client:
            await http.aclose()

    # A proxy or captive portal can answer 200 with a non-object body; treat
    # anything that is not the documented shape as "catalogue unavailable"
    # rather than letting an AttributeError escape a best-effort reader.
    if not isinstance(payload, dict):
        return []
    entries = payload.get("models")
    if not isinstance(entries, list):
        return []

    models: list[CodexModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        try:
            model = CodexModel(
                slug=slug,
                display_name=str(entry.get("display_name") or slug),
                priority=int(entry.get("priority") or 0),
                context_window=int(entry.get("context_window") or 0),
                minimal_client_version=str(entry.get("minimal_client_version") or ""),
            )
        except (TypeError, ValueError) as exc:
            # One entry the backend typed differently than documented costs
            # that entry, not the catalogue. Letting the coercion raise here
            # would empty the whole result and silently pin the tier map.
            logger.debug("codex catalogue: skipping malformed entry %r (%s)", slug, exc)
            continue
        models.append(model)

    _warn_on_gated_models(models, cfg)
    return models


def _version_tuple(value: str) -> tuple[int, ...]:
    """Parse a dotted version for ordering. Unparseable parts sort as 0."""

    parts: list[int] = []
    for chunk in value.split("."):
        digits = _LEADING_DIGITS.match(chunk)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


def _warn_on_gated_models(models: list[CodexModel], cfg: TidepoolConfig) -> None:
    """Flag models the configured ``client_version`` is too old to use.

    The backend hides gated models instead of erroring, so a stale
    ``client_version`` shows up only as a catalogue that quietly lost its top
    entries — and the tier map silently drops a generation behind. Naming both
    versions makes that one log line instead of an investigation.
    """

    ours = _version_tuple(cfg.client_version)
    for model in models:
        if not model.minimal_client_version:
            continue
        if _version_tuple(model.minimal_client_version) > ours:
            logger.warning(
                "codex catalogue: model %s needs client_version >= %s but "
                "client_version is %s; it may be hidden or rejected",
                model.slug,
                model.minimal_client_version,
                cfg.client_version,
            )


async def resolve_tier_models(
    cred: CodexCredential,
    *,
    config: TidepoolConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Map the live catalogue onto tiers, falling back to the pinned ids."""

    models = await fetch_models(cred, config=config, client=client)
    if not models:
        return dict(FALLBACK_TIER_MODELS)
    return select_tier_models([(m.slug, m.priority) for m in models])


__all__ = [
    "CodexModel",
    "FALLBACK_TIER_MODELS",
    "backend_headers",
    "fetch_models",
    "resolve_tier_models",
    "select_tier_models",
]
