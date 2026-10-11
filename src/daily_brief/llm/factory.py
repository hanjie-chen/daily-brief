"""Select an explicitly configured provider for CLI and library entry points."""
from __future__ import annotations

import os
from collections.abc import Mapping

from .gemini_backend import GeminiBackend
from .openrouter_backend import OpenRouterBackend


def create_model_backend(env: Mapping[str, str] | None = None, *, evaluation=False):
    environment = os.environ if env is None else env
    provider = environment.get("LLM_PROVIDER", "gemini").strip().lower()
    # Preserve the unset default; .env.example explicitly selects OpenRouter.
    options = {} if env is None else {"env": environment}
    if provider == "gemini":
        if evaluation:
            options["summarizer_fallback_models"] = ()
        return GeminiBackend.from_environment(**options)
    if provider == "openrouter":
        return OpenRouterBackend.from_environment(**options)
    raise ValueError("LLM_PROVIDER must be gemini or openrouter")
