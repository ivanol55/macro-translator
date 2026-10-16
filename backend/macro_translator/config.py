"""Settings from environment variables. See .env.example for the full list."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in {"1", "true", "yes"}


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = "http://localhost:1234/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen3.5-9b"
    llm_timeout: float = 180.0
    llm_response_format: str = "auto"  # auto | json_schema | json_object | none
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_max_tokens: int | None = None
    llm_extra_body: dict = field(default_factory=dict)
    db_path: Path = ROOT / "data" / "macros.duckdb"
    max_input_chars: int = 2000
    debug: bool = False
    frontend_dir: Path = ROOT / "frontend"
    prompts_dir: Path = ROOT / "backend" / "prompts"


def load_settings() -> Settings:
    response_format = os.environ.get("LLM_RESPONSE_FORMAT", "auto").strip().lower() or "auto"
    if response_format not in {"auto", "json_schema", "json_object", "none"}:
        raise ValueError(f"LLM_RESPONSE_FORMAT has an incorrect value: {response_format!r}")
    extra = os.environ.get("LLM_EXTRA_BODY", "").strip()
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
    max_tokens = _int("LLM_MAX_TOKENS", 0)
    return Settings(
        llm_base_url=os.environ.get("LLM_BASE_URL", Settings.llm_base_url).rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("LLM_MODEL", Settings.llm_model).strip(),
        llm_timeout=_float("LLM_TIMEOUT_SECONDS", Settings.llm_timeout),
        llm_response_format=response_format,
        llm_max_retries=max(0, min(2, _int("LLM_MAX_RETRIES", 2))),
        llm_temperature=_float("LLM_TEMPERATURE", 0.0),
        llm_max_tokens=max_tokens or None,
        llm_extra_body=json.loads(extra) if extra else {},
        db_path=Path(os.environ.get("DB_PATH") or data_dir / "macros.duckdb"),
        max_input_chars=_int("MAX_INPUT_CHARS", 2000),
        debug=_bool("DEBUG"),
        frontend_dir=Path(os.environ.get("FRONTEND_DIR") or Settings.frontend_dir),
        prompts_dir=Path(os.environ.get("PROMPTS_DIR") or Settings.prompts_dir),
    )
