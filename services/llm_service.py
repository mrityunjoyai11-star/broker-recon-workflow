"""LLM Service — Claude via LangChain Anthropic."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from broker_recon_flow.config import get_llm_config
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

_llm_instance: ChatAnthropic | None = None
_llm_fast_instance: ChatAnthropic | None = None


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


def get_llm_fast() -> ChatAnthropic:
    """Return the fast/cheap LLM for SIPDO sub-agents (synthetic gen, eval, error analysis)."""
    global _llm_fast_instance
    if _llm_fast_instance is None:
        cfg = get_llm_config()
        api_key = _resolve_api_key(cfg)
        fast_model = cfg.get("sipdo_model", "claude-3-haiku-20240307")
        # Haiku models cap at 4096 output tokens
        fast_max = min(cfg.get("max_tokens", 4096), 4096)
        _llm_fast_instance = ChatAnthropic(
            model=fast_model,
            anthropic_api_key=api_key,
            max_tokens=fast_max,
            temperature=cfg.get("temperature", 0.0),
        )
        logger.info("Initialized fast LLM: %s", fast_model)
    return _llm_fast_instance


def invoke_llm_fast(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> str:
    """Invoke the fast/cheap LLM (for SIPDO sub-agents that don't need full Sonnet)."""
    if max_tokens:
        cfg = get_llm_config()
        api_key = _resolve_api_key(cfg)
        llm = ChatAnthropic(
            model=cfg.get("sipdo_model", "claude-3-haiku-20240307"),
            anthropic_api_key=api_key,
            max_tokens=min(max_tokens, 4096),
            temperature=cfg.get("temperature", 0.0),
        )
    else:
        llm = get_llm_fast()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    response = llm.invoke(messages)
    return response.content


def invoke_llm_json_fast(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> dict[str, Any]:
    """Invoke fast LLM and parse JSON response (for SIPDO sub-agents)."""
    raw = invoke_llm_fast(system_prompt, user_prompt, max_tokens=max_tokens)
    return _parse_json_response(raw)


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


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Parse a raw LLM response string into a JSON dict with multi-stage repair."""
    cleaned = raw.strip()

    # Strip markdown code fences — they may appear at the start or after
    # explanatory text the LLM prepends before the JSON block.
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    elif cleaned.startswith("```"):
        first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
        cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()
            cleaned = cleaned[: cleaned.rfind("```")]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: handle truncated fenced responses (response hit max_tokens,
    # so the closing ``` fence is missing). Look for an opening fence and
    # take everything after it.
    unclosed_fence = re.search(r"```(?:json)?\s*\n(.*)", cleaned, re.DOTALL)
    if unclosed_fence:
        candidate = unclosed_fence.group(1).strip()
        # Try to repair truncated JSON by closing open brackets/braces
        repaired = _repair_truncated_json(candidate)
        if repaired is not None:
            return repaired

    # Last resort: find the first { or [ and try to parse from there
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        idx = cleaned.find(start_char)
        if idx >= 0:
            candidate = cleaned[idx:]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                repaired = _repair_truncated_json(candidate)
                if repaired is not None:
                    return repaired

    logger.error("Failed to parse LLM JSON response: %s", raw[:500])
    return {"raw_response": raw, "parse_error": True}


def invoke_llm_json(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> dict[str, Any]:
    raw = invoke_llm(system_prompt, user_prompt, max_tokens=max_tokens)
    return _parse_json_response(raw)


def _repair_truncated_json(text: str):
    """Attempt to repair truncated JSON by closing open brackets/braces.

    Returns the parsed object on success, or None on failure.
    """
    # Try as-is first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Walk the string to track open brackets/braces (outside of strings)
    closers = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ('{', '['):
            closers.append('}' if ch == '{' else ']')
        elif ch in ('}', ']'):
            if closers:
                closers.pop()

    if not closers:
        return None

    # If we're inside a string, close it first
    if in_string:
        text += '"'

    # Remove any trailing comma or partial key before closing
    text = re.sub(r',\s*$', '', text)
    text = re.sub(r':\s*$', ': null', text)
    # Close all remaining open brackets/braces
    text += ''.join(reversed(closers))

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
