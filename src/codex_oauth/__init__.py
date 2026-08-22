"""codex-oauth — an LLM client backed by ChatGPT subscription quota.

Point it at a credential store and call it. Account rotation, 429 cooldowns
keyed to the backend's own reset time, OAuth refresh, live model resolution and
strict-mode structured output all happen inside a single call::

    from codex_oauth import CodexOAuthClient

    llm = CodexOAuthClient.from_env()          # or CodexOAuthClient("accounts.json")

    text = llm.complete("一句話介紹光合作用")
    report = llm.complete_structured(response_model=Report, messages="...")
    for delta in llm.stream("寫一首短詩"):
        print(delta, end="")

:class:`LLMClient` is the protocol to type application code against, and
:class:`LLMUnavailableError` is the only failure it has to handle — a reply
that does not fit ``response_model`` raises pydantic's ``ValidationError``
instead, which callers can retry at the prompt level.

Bootstrap credentials with ``codex-oauth-import`` (see :mod:`codex_oauth.import_cli`).

The stack underneath — :class:`CodexPool`, :class:`CodexTransport`,
:class:`CompositeProvider`, the wire codec, the catalogue reader — stays
exported for services that drive it directly, e.g. to plug a DB-backed
:class:`AccountStore` into a multi-worker deployment.
"""

from codex_oauth.catalogue import (
    FALLBACK_TIER_MODELS,
    CodexModel,
    backend_headers,
    fetch_models,
    resolve_tier_models,
    select_tier_models,
)
from codex_oauth.client import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    CodexOAuthClient,
    LLMClient,
    LLMUnavailableError,
    Prompt,
)
from codex_oauth.composite import CodexPoolDryError, CompositeProvider
from codex_oauth.config import CodexOAuthConfig
from codex_oauth.import_cli import import_credentials
from codex_oauth.pool import (
    AccountStatus,
    CodexCredential,
    CodexPool,
    CodexRefreshFailed,
)
from codex_oauth.store import AccountRecord, AccountStore, JsonFileStore
from codex_oauth.transport import (
    CodexAuthExpiredError,
    CodexTransport,
    CodexTransportError,
    CodexUsageLimitError,
)
from codex_oauth.types import (
    ChatRequest,
    ChatResponse,
    Message,
    Role,
    StreamChunk,
    TokenUsage,
)
from codex_oauth.wire import drop_nulls, strict_schema

__all__ = [
    # The interface most callers need.
    "CodexOAuthClient",
    "LLMClient",
    "LLMUnavailableError",
    "Prompt",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    # Configuration and credential storage.
    "AccountRecord",
    "AccountStatus",
    "AccountStore",
    "CodexOAuthConfig",
    "JsonFileStore",
    "import_credentials",
    # The stack underneath, for callers assembling their own.
    "ChatRequest",
    "ChatResponse",
    "CodexAuthExpiredError",
    "CodexCredential",
    "CodexModel",
    "CodexPool",
    "CodexPoolDryError",
    "CodexRefreshFailed",
    "CodexTransport",
    "CodexTransportError",
    "CodexUsageLimitError",
    "CompositeProvider",
    "FALLBACK_TIER_MODELS",
    "Message",
    "Role",
    "StreamChunk",
    "TokenUsage",
    "backend_headers",
    "drop_nulls",
    "fetch_models",
    "resolve_tier_models",
    "select_tier_models",
    "strict_schema",
]
