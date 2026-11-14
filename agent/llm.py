"""One switch for the model: LLM_MODE selects ollama, Gemini (Vertex ADC), haiku, or mock.

deepagents sees the same BaseChatModel interface in every mode. Roles let each sub-agent get
its own model object (the mock needs a separate script per role; real models are identical).
"""

from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel

from station.config import ConfigError, Settings, load_settings

ROLES = ("commander", "triage", "drone-pilot", "evidence")
HAIKU_MODEL = "claude-haiku-5-5"


def get_chat_model(role: str = "commander", settings: Settings | None = None) -> BaseChatModel:
    if role not in ROLES:
        raise ValueError(f"unknown agent role '{role}'")
    settings = settings or load_settings()
    if settings.llm_mode == "mock":
        from agent.mock import MockChatModel

        return MockChatModel(role=role)
    if settings.llm_mode == "gemini":
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
        from langchain_google_genai import ChatGoogleGenerativeAI

        try:
            credentials, adc_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except DefaultCredentialsError as exc:
            raise ConfigError(
                "Gemini needs ADC: run gcloud auth application-default login, then set "
                "GOOGLE_CLOUD_PROJECT. No Gemini API key is required."
            ) from exc
        project = settings.google_cloud_project or adc_project
        if not project:
            raise ConfigError("Gemini ADC is available, but GOOGLE_CLOUD_PROJECT is missing.")
        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            vertexai=True,
            credentials=credentials,
            project=project,
            location=settings.google_cloud_location,
            max_tokens=2048,
            max_retries=2,
            timeout=float(os.getenv("GEMINI_TIMEOUT_S", "60")),
        )
    if settings.llm_mode == "haiku":
        from langchain_anthropic import ChatAnthropic

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ConfigError("Haiku needs ANTHROPIC_API_KEY.")
        # No temperature: current Claude models reject sampling parameters (400 "Extra inputs").
        return ChatAnthropic(
            model=HAIKU_MODEL, max_tokens=2048, max_retries=2,
            default_request_timeout=float(os.getenv("ANTHROPIC_TIMEOUT_S", "60")),  # never hang a wake-up
        )
    if settings.llm_mode != "ollama":
        raise ConfigError(f"Unknown LLM_MODE: {settings.llm_mode}")
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
