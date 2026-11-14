"""Settings read from .env. One place to switch models and ports."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LLM_MODES = ("ollama", "haiku", "mock")

MODEL_LABELS = {
    "ollama": "Local",
    "haiku": "Claude Haiku",
    "mock": "Mock",
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    llm_mode: str
    ollama_model: str
    host: str
    port: int

    @property
    def model_label(self) -> str:
        if self.llm_mode == "ollama":
            return f"{MODEL_LABELS['ollama']} · {self.ollama_model}"
        return MODEL_LABELS[self.llm_mode]


def load_settings() -> Settings:
    mode = os.getenv("LLM_MODE", "ollama").strip().lower()
    if mode not in LLM_MODES:
        raise ConfigError(f"LLM_MODE must be one of {', '.join(LLM_MODES)} (got '{mode}').")
    if mode == "haiku" and not os.getenv("ANTHROPIC_API_KEY"):
        raise ConfigError("LLM_MODE=haiku needs ANTHROPIC_API_KEY in .env.")
    return Settings(
        llm_mode=mode,
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
