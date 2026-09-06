"""LLM summarization.

    base.py                the LLMProvider interface and the SummaryOutput schema
    prompt.py              the prompt, shared by every provider
    anthropic_provider.py  Claude, via the official SDK's structured output
    openai_provider.py     OpenAI, via Responses / Chat Completions parsing

``get_provider()`` resolves configuration to an implementation. Calling code
depends only on ``LLMProvider`` and never names a vendor, so switching provider
is a change to ``LLM_PROVIDER`` in the environment.
"""

import logging
from typing import Dict, List, Optional

from app.config import settings
from app.services.llm.base import (
    LLMProvider,
    SummaryOutput,
    SummaryRequest,
    SummaryResult,
)

logger = logging.getLogger(__name__)

# Registered by name. A new provider is one module plus one entry here.
_REGISTRY: Dict[str, type] = {}
_builtins_loaded = False


def register(name: str, factory: type) -> None:
    """Add a provider under ``name``.

    Loads the built-ins first: a caller registering their own provider before
    anything else has touched the registry must not end up with a registry
    that contains only theirs.
    """
    _load_builtins()
    _REGISTRY[name] = factory


def _load_builtins() -> None:
    """Import the built-in providers, once.

    Imports are deferred and individually guarded so a missing optional SDK
    disables one provider instead of breaking the module. Tracked by an
    explicit flag rather than by whether the registry is empty — an
    externally registered provider would otherwise look like "already loaded"
    and suppress the built-ins entirely.
    """
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    try:
        from app.services.llm.anthropic_provider import AnthropicProvider

        register("anthropic", AnthropicProvider)
    except Exception as error:  # pragma: no cover - import-time only
        logger.debug("anthropic provider unavailable: %s", error)
    try:
        from app.services.llm.openai_provider import OpenAIProvider

        register("openai", OpenAIProvider)
    except Exception as error:  # pragma: no cover - import-time only
        logger.debug("openai provider unavailable: %s", error)


def provider_names() -> List[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def get_provider(name: Optional[str] = None) -> Optional[LLMProvider]:
    """Resolve a provider.

    An explicit name is honoured even when it is not configured, so a
    misconfiguration is reported as "no credentials" rather than silently
    running on a different vendor than the operator asked for. With no name,
    the first provider that actually has credentials wins.
    """
    _load_builtins()
    requested = (name or settings.llm_provider or "").strip().lower()

    if requested:
        factory = _REGISTRY.get(requested)
        if factory is None:
            logger.warning(
                "unknown LLM_PROVIDER %r; known providers: %s", requested, provider_names()
            )
            return None
        return factory()

    for candidate in ("anthropic", "openai"):
        factory = _REGISTRY.get(candidate)
        if factory is None:
            continue
        provider = factory()
        if provider.available():
            logger.info("auto-selected LLM provider %r (%s)", candidate, provider.model)
            return provider

    # Nothing is configured. Returning the first known provider means the
    # caller reports a specific, actionable "key not set" rather than a vague
    # "no provider".
    for candidate in ("anthropic", "openai"):
        if candidate in _REGISTRY:
            return _REGISTRY[candidate]()
    return None


__all__ = [
    "LLMProvider",
    "SummaryOutput",
    "SummaryRequest",
    "SummaryResult",
    "get_provider",
    "provider_names",
    "register",
]
