"""Configuration loader for the Brokerage Reconciliation System."""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache

import yaml


@lru_cache(maxsize=1)
def load_config(config_path: str | None = None) -> dict:
    if config_path is None:
        config_path = str(Path(__file__).parent / "dev.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_llm_config() -> dict:
    return load_config().get("llm", {})

def get_storage_config() -> dict:
    return load_config().get("storage", {})

def get_agent_config(agent_name: str) -> dict:
    return load_config().get("agents", {}).get(agent_name, {})

def get_hitl_config() -> dict:
    return load_config().get("hitl", {})

def get_broker_configs() -> list:
    return load_config().get("brokers", [])

def get_db_config() -> dict:
    return load_config().get("database", {})

def get_ms_data_config() -> dict:
    return load_config().get("ms_data", {})

def get_server_config() -> dict:
    return load_config().get("server", {})

def get_ui_config() -> dict:
    return load_config().get("ui", {})
