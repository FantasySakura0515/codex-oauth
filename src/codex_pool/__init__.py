"""codex-pool — ChatGPT-subscription Codex backend with a rotating account pool.

Every seam is a small protocol, so any service can embed it.

Typical wiring::

    from codex_pool import (
        ChatRequest, CodexPool, CodexPoolConfig, CodexTransport,
        CompositeProvider, JsonFileStore, Message, Role, resolve_tier_models,
    )

    config = CodexPoolConfig()
    pool = CodexPool(JsonFileStore("codex-accounts.json"), config=config)
    provider = CompositeProvider(
        pool=pool,
        codex=CodexTransport(config=config),
        resolver=lambda cred: resolve_tier_models(cred, config=config),
    )
    response = await provider.chat(ChatRequest(
        model="balanced",   # tier name, or a concrete slug like "gpt-5.6"
        messages=[Message(role=Role.USER, content="hi")],
    ))

Bootstrap credentials with ``codex-pool-import`` (see :mod:`codex_pool.import_cli`).
"""

from codex_pool.catalogue import (
    FALLBACK_TIER_MODELS,
    CodexModel,
    backend_headers,
    fetch_models,
    resolve_tier_models,
    select_tier_models,
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
from codex_pool.wire import strict_schema

__all__ = [
    "AccountRecord",
    "AccountStatus",
    "AccountStore",
    "ChatRequest",
    "ChatResponse",
    "CodexAuthExpiredError",
    "CodexCredential",
    "CodexModel",
    "CodexPool",
    "CodexPoolConfig",
    "CodexPoolDryError",
    "CodexRefreshFailed",
    "CodexTransport",
    "CodexTransportError",
    "CodexUsageLimitError",
    "CompositeProvider",
    "FALLBACK_TIER_MODELS",
    "JsonFileStore",
    "Message",
    "Role",
    "StreamChunk",
    "TokenUsage",
    "backend_headers",
    "fetch_models",
    "import_credentials",
    "resolve_tier_models",
    "select_tier_models",
    "strict_schema",
]
