"""Settings and YAML boardroom config loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

MODULE_DIR = Path(__file__).resolve().parent
# Config packaged alongside this module; last-resort fallback for discovery.
PACKAGED_CONFIG_PATH = MODULE_DIR / "default_config.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLYGNOSIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_base_url: str = "https://ai-gateway.vercel.sh/v1"
    api_key: str = ""
    default_model: str = "anthropic/claude-sonnet-4"
    # Empty means auto-resolve via resolve_config_path().
    config_path: str = ""
    artifacts_dir: str = "./artifacts"
    corrections_buffer: str = "./.corrections_buffer.json"
    host: str = "0.0.0.0"
    port: int = 8080

    service_api_key: str = ""
    max_in_flight: int = 2
    jobs_db: str = "./data/jobs.db"
    llm_max_retries: int = 3
    reflexion_enabled: bool = False
    objective_max_chars: int = 20000

    max_llm_concurrency: int = 8
    temperature_solver: float = 0.5
    temperature_critic: float = 0.2
    temperature_scorer: float = 0.0
    temperature_default: float = 0.3
    max_tokens_solver: int = 8192
    max_tokens_default: int = 4096
    job_lease_seconds: int = 3600


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Locate the boardroom config file.

    Discovery order:
      1. explicit argument / POLYGNOSIS_CONFIG_PATH env, if it exists on disk
      2. ./config.yaml in the current working directory, if it exists
      3. packaged default_config.yaml next to this module (always returned last)
    """
    candidate = explicit or os.environ.get("POLYGNOSIS_CONFIG_PATH")
    if candidate:
        explicit_path = Path(candidate)
        if explicit_path.exists():
            return explicit_path

    cwd_config = Path.cwd() / "config.yaml"
    if cwd_config.exists():
        return cwd_config

    return PACKAGED_CONFIG_PATH


def load_boardroom_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    data = yaml.safe_load(cfg_path.read_text()) or {}
    if "models" not in data or "settings" not in data:
        raise ValueError("config.yaml must contain 'models' and 'settings'")
    return data


def get_solver_model_name(cfg: dict[str, Any], idx: int) -> str:
    solver_list = cfg.get("solver_models") or []
    if solver_list and idx < len(solver_list) and solver_list[idx]:
        return str(solver_list[idx])
    key = f"solver_{idx + 1}"
    model = (cfg.get("models") or {}).get(key, "")
    if model:
        return str(model)
    return str((cfg.get("models") or {}).get("fallback", "") or "")


def get_role_model(cfg: dict[str, Any], role: str) -> str:
    models = cfg.get("models") or {}
    return str(models.get(role) or models.get("fallback") or "")
