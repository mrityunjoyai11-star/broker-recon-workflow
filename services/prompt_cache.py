"""Prompt cache — CRUD for SIPDO-optimized extraction prompts."""

from __future__ import annotations

from typing import Optional

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


def get_cached_prompt(broker_name: str) -> Optional[str]:
    """Return the cached SIPDO-optimized prompt text for a broker, or None."""
    from broker_recon_flow.db.models import OptimizedPromptCache
    factory = get_session_factory()
    db = factory()
    try:
        row = (
            db.query(OptimizedPromptCache)
            .filter(OptimizedPromptCache.broker_name.ilike(f"%{broker_name}%"))
            .order_by(OptimizedPromptCache.accuracy_score.desc())
            .first()
        )
        return row.prompt_text if row else None
    except Exception as exc:
        logger.warning("Prompt cache lookup failed: %s", exc)
        return None
    finally:
        db.close()


def save_optimized_prompt(
    broker_name: str,
    prompt_text: str,
    accuracy_score: float = 0.0,
    optimization_trace: list | None = None,
    source_session_id: str | None = None,
) -> None:
    """Save (upsert) a SIPDO-optimized prompt for a broker."""
    from broker_recon_flow.db.models import OptimizedPromptCache
    factory = get_session_factory()
    db = factory()
    try:
        existing = (
            db.query(OptimizedPromptCache)
            .filter(OptimizedPromptCache.broker_name == broker_name)
            .first()
        )
        if existing:
            existing.prompt_text = prompt_text
            existing.accuracy_score = accuracy_score
            existing.optimization_trace = optimization_trace or []
            existing.source_session_id = source_session_id
        else:
            row = OptimizedPromptCache(
                broker_name=broker_name,
                prompt_text=prompt_text,
                accuracy_score=accuracy_score,
                optimization_trace=optimization_trace or [],
                source_session_id=source_session_id,
            )
            db.add(row)
        db.commit()
        logger.info("Saved optimized prompt for broker=%s (accuracy=%.2f)", broker_name, accuracy_score)
    except Exception as exc:
        db.rollback()
        logger.error("Failed to save optimized prompt: %s", exc)
    finally:
        db.close()


def list_all_prompts() -> list[dict]:
    """Return all cached prompts as dicts (for API listing)."""
    from broker_recon_flow.db.models import OptimizedPromptCache
    factory = get_session_factory()
    db = factory()
    try:
        rows = db.query(OptimizedPromptCache).order_by(
            OptimizedPromptCache.created_at.desc()
        ).all()
        return [
            {
                "id": r.id,
                "broker_name": r.broker_name,
                "accuracy_score": r.accuracy_score,
                "source_session_id": r.source_session_id,
                "created_at": str(r.created_at),
                "updated_at": str(r.updated_at),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Failed to list prompts: %s", exc)
        return []
    finally:
        db.close()
