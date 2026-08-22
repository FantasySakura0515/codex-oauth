"""codex-pool — an LLM client backed by ChatGPT subscription quota.

Point it at a credential store and call it. Account rotation, 429 cooldowns
keyed to the backend's own reset time, OAuth refresh, live model resolution and
strict-mode structured output all happen inside a single call::

    from codex_pool import CodexPoolClient

    llm = CodexPoolClient.from_env()          # or CodexPoolClient("accounts.json")

    text = llm.complete("一句話介紹光合作用")
    report = llm.complete_structured(response_model=Report, messages="...")
    for delta in llm.stream("寫一首短詩"):
        print(delta, end="")

:class:`LLMClient` is the protocol to type application code against, and
:class:`LLMUnavailableError` is the only failure it has to handle — a reply
that does not fit ``response_model`` raises pydantic's ``ValidationError``
instead, which callers can retry at the prompt level.

Bootstrap credentials with ``codex-pool-import`` (see :mod:`codex_pool.import_cli`).

The stack underneath — :class:`CodexPool`, :class:`CodexTransport`,
:class:`CompositeProvider`, the wire codec, the catalogue reader — stays
exported for services that drive it directly, e.g. to plug a DB-backed
:class:`AccountStore` into a multi-worker deployment.
"""

from codex_pool.catalogue import (
    FALLBACK_TIER_MODELS,
    CodexModel,
    backend_headers,
    fetch_models,
    resolve_tier_models,
    select_tier_models,
)
from codex_pool.client import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    CodexPoolClient,
    LLMClient,
    LLMUnavailableError,
    Prompt,
)
from codex_pool.composite import CodexPoolDryError, CompositeProvider
from codex_pool.config import CodexPoolConfig
from codex_pool.import_cli import import_credentials
from codex_pool.pool import (
    AccountStatus,
    CodexCredential,
    CodexPool,
    CodexRefreshFailed,
)
from codex_pool.store import AccountRecord, AccountStore, JsonFileStore
from codex_pool.transport import (
    CodexAuthExpiredError,
    CodexTransport,
    CodexTransportError,
    CodexUsageLimitError,
)
from codex_pool.types import (
    ChatRequest,
    ChatResponse,
    Message,
    Role,
    StreamChunk,
    TokenUsage,
)
from codex_pool.wire import drop_nulls, strict_schema

__all__ = [
    # The interface most callers need.
    "CodexPoolClient",
    "LLMClient",
    "LLMUnavailableError",
    "Prompt",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    # Configuration and credential storage.
    "AccountRecord",
    "AccountStatus",
    "AccountStore",
    "CodexPoolConfig",
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
