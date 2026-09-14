from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from .models import Contract
from .storage import atomic_json, read_json


class Settings(Contract):
    gemini_key: SecretStr = SecretStr("")
    deepseek_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.8-flash"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_fast_model: str = "deepseek-flash"
    deepseek_reasoning_effort: Literal["low", "high", "max"] = "high"
    gemini_concurrency: int = Field(default=2, ge=1, le=8)
    deepseek_concurrency: int = Field(default=2, ge=1, le=8)

    @field_validator("gemini_model", "deepseek_model", "deepseek_fast_model")
    @classmethod
    def model_name(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value):
            raise ValueError("Nome modello non valido")
        return value

    def public(self):
        return {"gemini_configured": bool(self.gemini_key.get_secret_value()),
                "deepseek_configured": bool(self.deepseek_key.get_secret_value()),
                "gemini_model": self.gemini_model, "deepseek_model": self.deepseek_model,
                "deepseek_fast_model": self.deepseek_fast_model,
                "deepseek_reasoning_effort": self.deepseek_reasoning_effort,
                "gemini_concurrency": self.gemini_concurrency,
                "deepseek_concurrency": self.deepseek_concurrency}


class SettingsUpdate(Contract):
    gemini_key: SecretStr = SecretStr("")
    deepseek_key: SecretStr = SecretStr("")
    gemini_model: str = Field(default="gemini-3.8-flash", max_length=100)
    deepseek_model: str = Field(default="deepseek-v4-pro", max_length=100)
    deepseek_fast_model: str = Field(default="deepseek-flash", max_length=100)
    deepseek_reasoning_effort: Literal["low", "high", "max"] = "high"
    # The browser deliberately leaves concurrency to the local installation
    # (.env). An omitted value must not reset an environment-tuned setting.
    gemini_concurrency: int | None = Field(default=None, ge=1, le=8)
    deepseek_concurrency: int | None = Field(default=None, ge=1, le=8)
    remember: bool = False
    clear_keys: bool = False


def data_root() -> Path:
    load_dotenv()
    return Path(os.environ.get("STUDYGENIUS_DATA_DIR", ".studygenius")).resolve()


def load_settings(root: Path) -> Settings:
    load_dotenv()
    saved = read_json(root / "private-settings.json") if (root / "private-settings.json").exists() else {}
    env = {"gemini_key": "GEMINI_API_KEY", "deepseek_key": "DEEPSEEK_API_KEY",
           "gemini_model": "GEMINI_MODEL", "deepseek_model": "DEEPSEEK_MODEL",
           "deepseek_fast_model": "DEEPSEEK_FAST_MODEL",
           "deepseek_reasoning_effort": "DEEPSEEK_REASONING_EFFORT",
           "gemini_concurrency": "GEMINI_CONCURRENCY",
           "deepseek_concurrency": "DEEPSEEK_CONCURRENCY"}
    for key, name in env.items():
        if os.environ.get(name):
            saved[key] = os.environ[name]
    return Settings.model_validate(saved)


def save_settings(root: Path, settings: Settings):
    # Plain text by explicit local opt-in; never return this data over the API.
    value = settings.model_dump(mode="json")
    value["gemini_key"] = settings.gemini_key.get_secret_value()
    value["deepseek_key"] = settings.deepseek_key.get_secret_value()
    path = root / "private-settings.json"
    atomic_json(path, value)
    path.chmod(0o600)
