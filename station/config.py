"""Settings read from .env. One place to switch models and ports."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LLM_MODES = ("ollama", "haiku", "gemini", "mock")

MODEL_LABELS = {
    "ollama": "Local",
    "haiku": "Claude Haiku",
    "gemini": "Gemini",
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
    gemini_model: str = "gemini-3.5-flash"
    google_cloud_project: str = ""
    google_cloud_location: str = "global"

    @property
    def model_label(self) -> str:
        if self.llm_mode == "ollama":
            return f"{MODEL_LABELS['ollama']} · {self.ollama_model}"
        if self.llm_mode == "gemini":
            return f"Gemini · {self.gemini_model} (ADC)"
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
        gemini_model=os.getenv("GEMINI_MODEL") or "gemini-3.5-flash",
        google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT") or "",
        google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION") or "global",
    )
