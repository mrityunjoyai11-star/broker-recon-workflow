"""Prompt cache — CRUD for SIPDO-optimized extraction prompts.

Cache keying strategy
=====================
Prompts are primarily keyed by a **PDF structure fingerprint** derived from the
first 3 pages of the source PDF, not the broker name.  This means:

  - Any PDF with the same table layout / column headers will reuse the same
    optimized prompt, even if the broker name was mis-detected.
  - Broker name is stored as metadata and used as a secondary / fallback key
    when no PDF is available (e.g. Excel-only runs).

Fingerprint algorithm
---------------------
1. Open the PDF with pdfplumber.
2. Extract pdfplumber tables from the first MIN(3, n_pages) pages.
3. Collect all column names seen, normalise them (strip, lower), sort.
4. If no tables found, fall back to the first 2000 chars of plain text
   with all digits and punctuation removed (pure structural text).
5. SHA-256 hash of the resulting string.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

_FINGERPRINT_PAGES = 3      # how many pages to sample for the fingerprint


def compute_pdf_fingerprint(pdf_path: str) -> Optional[str]:
    """Return a SHA-256 fingerprint of the PDF's structural layout.

    Reads the first _FINGERPRINT_PAGES pages and derives a stable hash from
    the column headers found by pdfplumber.  Falls back to normalised plain
    text (digits stripped) if no tables are present on those pages.
    Returns None on any error so callers can fall back gracefully.
    """
    try:
        import pdfplumber
        col_names: list[str] = []
        text_sample = ""
        with pdfplumber.open(pdf_path) as pdf:
            pages_to_scan = pdf.pages[:_FINGERPRINT_PAGES]
            for page in pages_to_scan:
                for table in (page.extract_tables() or []):
                    if table and table[0]:
                        col_names.extend(
                            str(h).strip().lower() for h in table[0] if h
                        )
                if not text_sample:
                    text_sample += (page.extract_text() or "")

        if col_names:
            # Unique, sorted column names → stable structural fingerprint
            fingerprint_src = "|".join(sorted(set(col_names)))
        else:
            # No tables — use plain-text structure (remove numbers / punctuation)
            normalised = re.sub(r"[\d\W]+", " ", text_sample).lower()
            fingerprint_src = " ".join(normalised.split()[:300])  # first 300 words

        if not fingerprint_src.strip():
            return None

        digest = hashlib.sha256(fingerprint_src.encode()).hexdigest()
        logger.debug("PDF fingerprint for %s: %s (src=%s...)", pdf_path, digest[:12], fingerprint_src[:60])
        return digest
    except Exception as exc:
        logger.warning("Could not compute PDF fingerprint for %s: %s", pdf_path, exc)
        return None


def get_cached_prompt(
    broker_name: str,
    flow_type: str = "receivable",
    pdf_path: Optional[str] = None,
) -> Optional[str]:
    """Return the cached SIPDO-optimized prompt, or None.

    Lookup order:
      1. If pdf_path given → compute fingerprint → exact fingerprint match
      2. Broker name fuzzy match (fallback / Excel-only runs)
    """
    from broker_recon_flow.db.models import OptimizedPromptCache
    factory = get_session_factory()
    db = factory()
    try:
        # ── 1. Fingerprint match (preferred) ─────────────────────────────
        if pdf_path:
            fp = compute_pdf_fingerprint(pdf_path)
            if fp:
                row = (
                    db.query(OptimizedPromptCache)
                    .filter(
                        OptimizedPromptCache.pdf_fingerprint == fp,
                        OptimizedPromptCache.flow_type == flow_type,
                    )
                    .order_by(OptimizedPromptCache.accuracy_score.desc())
                    .first()
                )
                if row:
                    logger.info(
                        "Prompt cache HIT (fingerprint) for pdf=%s → broker=%s",
                        pdf_path, row.broker_name,
                    )
                    return row.prompt_text

        # ── 2. Broker name fallback ───────────────────────────────────────
        if broker_name:
            row = (
                db.query(OptimizedPromptCache)
                .filter(
                    OptimizedPromptCache.broker_name.ilike(f"%{broker_name}%"),
                    OptimizedPromptCache.flow_type == flow_type,
                )
                .order_by(OptimizedPromptCache.accuracy_score.desc())
                .first()
            )
            if row:
                logger.info(
                    "Prompt cache HIT (broker name) for broker=%s", broker_name,
                )
                return row.prompt_text

        return None
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
    flow_type: str = "receivable",
    pdf_path: Optional[str] = None,
) -> None:
    """Save (upsert) a SIPDO-optimized prompt keyed by fingerprint + broker."""
    from broker_recon_flow.db.models import OptimizedPromptCache
    fp = compute_pdf_fingerprint(pdf_path) if pdf_path else None

    factory = get_session_factory()
    db = factory()
    try:
        # Prefer upsert by fingerprint when available
        existing = None
        if fp:
            existing = (
                db.query(OptimizedPromptCache)
                .filter(
                    OptimizedPromptCache.pdf_fingerprint == fp,
                    OptimizedPromptCache.flow_type == flow_type,
                )
                .first()
            )
        if existing is None:
            existing = (
                db.query(OptimizedPromptCache)
                .filter(
                    OptimizedPromptCache.broker_name == broker_name,
                    OptimizedPromptCache.flow_type == flow_type,
                )
                .first()
            )

        if existing:
            existing.prompt_text = prompt_text
            existing.accuracy_score = accuracy_score
            existing.optimization_trace = optimization_trace or []
            existing.source_session_id = source_session_id
            if fp:
                existing.pdf_fingerprint = fp
        else:
            db.add(OptimizedPromptCache(
                broker_name=broker_name,
                flow_type=flow_type,
                pdf_fingerprint=fp,
                prompt_text=prompt_text,
                accuracy_score=accuracy_score,
                optimization_trace=optimization_trace or [],
                source_session_id=source_session_id,
            ))
        db.commit()
        logger.info(
            "Saved optimized prompt for broker=%s fp=%s (accuracy=%.2f)",
            broker_name, fp[:8] if fp else "none", accuracy_score,
        )
    except Exception as exc:
        db.rollback()
        logger.error("Failed to save optimized prompt: %s", exc)
    finally:
        db.close()


def get_cached_column_mapping(
    broker_name: str,
    flow_type: str = "receivable",
    pdf_path: Optional[str] = None,
) -> Optional[dict]:
    """Return a cached column mapping from TemplateCache.

    Lookup order:
      1. Fingerprint match (if pdf_path given)
      2. Broker name fuzzy match (fallback)
    """
    from broker_recon_flow.db.models import TemplateCache
    factory = get_session_factory()
    db = factory()
    try:
        if pdf_path:
            fp = compute_pdf_fingerprint(pdf_path)
            if fp:
                row = (
                    db.query(TemplateCache)
                    .filter(
                        TemplateCache.pdf_fingerprint == fp,
                        TemplateCache.flow_type == flow_type,
                        TemplateCache.hitl_approved == True,
                    )
                    .order_by(TemplateCache.use_count.desc())
                    .first()
                )
                if row:
                    logger.info(
                        "TemplateCache HIT (fingerprint) for pdf=%s → broker=%s",
                        pdf_path, row.broker_name,
                    )
                    return row.column_mapping

        if broker_name:
            row = (
                db.query(TemplateCache)
                .filter(
                    TemplateCache.broker_name.ilike(f"%{broker_name}%"),
                    TemplateCache.flow_type == flow_type,
                    TemplateCache.hitl_approved == True,
                )
                .order_by(TemplateCache.use_count.desc())
                .first()
            )
            if row:
                logger.info("TemplateCache HIT (broker name) for broker=%s", broker_name)
                return row.column_mapping

        return None
    except Exception as exc:
        logger.warning("TemplateCache lookup failed: %s", exc)
        return None
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
