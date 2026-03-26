"""LLM Service — Claude via LangChain Anthropic."""

from __future__ import annotations

import json
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from broker_recon_flow.config import get_llm_config
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

_llm_instance: ChatAnthropic | None = None


def _resolve_api_key(cfg: dict) -> str:
    """Resolve API key: prefer env var, fall back to config."""
    import os
    key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("api_key", "")
    if key.startswith("${"):
        # Unresolved placeholder — try env var name inside ${}
        var_name = key.strip("${}").strip()
        key = os.environ.get(var_name, "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Export it as an environment variable."
        )
    return key


def get_llm() -> ChatAnthropic:
    global _llm_instance
    if _llm_instance is None:
        cfg = get_llm_config()
        api_key = _resolve_api_key(cfg)
        _llm_instance = ChatAnthropic(
            model=cfg.get("model", "claude-sonnet-4-20250514"),
            anthropic_api_key=api_key,
            max_tokens=cfg.get("max_tokens", 4096),
            temperature=cfg.get("temperature", 0.0),
        )
        logger.info("Initialized Claude LLM: %s", cfg.get("model"))
    return _llm_instance


def invoke_llm(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> str:
    if max_tokens:
        cfg = get_llm_config()
        api_key = _resolve_api_key(cfg)
        llm = ChatAnthropic(
            model=cfg.get("model", "claude-sonnet-4-20250514"),
            anthropic_api_key=api_key,
            max_tokens=max_tokens,
            temperature=cfg.get("temperature", 0.0),
        )
    else:
        llm = get_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    response = llm.invoke(messages)
    return response.content


def invoke_llm_json(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> dict[str, Any]:
    raw = invoke_llm(system_prompt, user_prompt, max_tokens=max_tokens)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
        cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()
            cleaned = cleaned[: cleaned.rfind("```")]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM JSON response: %s", raw[:500])
        return {"raw_response": raw, "parse_error": True}
