"""One switch for the model: LLM_MODE in .env picks ollama (default), haiku, or mock.

deepagents sees the same BaseChatModel interface in every mode. Roles let each sub-agent get
its own model object (the mock needs a separate script per role; real models are identical).
"""

from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel

from station.config import Settings, load_settings

ROLES = ("commander", "triage", "drone-pilot", "evidence")
HAIKU_MODEL = "claude-haiku-5-5"


def get_chat_model(role: str = "commander", settings: Settings | None = None) -> BaseChatModel:
    if role not in ROLES:
        raise ValueError(f"unknown agent role '{role}'")
    settings = settings or load_settings()
    if settings.llm_mode == "mock":
        from agent.mock import MockChatModel

        return MockChatModel(role=role)
    if settings.llm_mode == "haiku":
        from langchain_anthropic import ChatAnthropic

        # Costs money: only reached when the person sets LLM_MODE=haiku themselves.
        return ChatAnthropic(model=HAIKU_MODEL, temperature=0, max_tokens=2048, max_retries=2)
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=settings.ollama_model,
        temperature=0,
        # Ollama's default context is far too small for the deepagents prompt + tool schemas;
        # a truncated prompt silently breaks tool calling.
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "16384")),
        reasoning=os.getenv("OLLAMA_REASONING", "false").lower() == "true",
        base_url=os.getenv("OLLAMA_BASE_URL") or None,
    )
