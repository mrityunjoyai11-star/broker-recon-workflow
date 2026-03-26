"""LangGraph shared state for the 7-node reconciliation pipeline."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from broker_recon_flow.schemas.canonical_trade import (
    VerificationResult, ClassificationResult, ExtractionResult,
    ReconciliationResult, PipelineStatus,
)


class GraphState(BaseModel):
    """Shared state flowing through all 7 nodes of the pipeline."""

    # ── Input ────────────────────────────────────────────────────────────
    session_id: str = ""
    pdf_path: Optional[str] = None
    excel_path: Optional[str] = None
    broker_hint: Optional[str] = None   # optional hint from upload form

    # ── Pipeline control ─────────────────────────────────────────────────
    status: str = PipelineStatus.PENDING.value
    current_step: str = ""
    error: Optional[str] = None

    # ── Node outputs ─────────────────────────────────────────────────────
    verification: Optional[VerificationResult] = None

    classification: Optional[ClassificationResult] = None
    broker_name: Optional[str] = None
    template_type: Optional[str] = None
    cached_column_mapping: Optional[dict] = None   # from TemplateCache DB

    extraction: Optional[ExtractionResult] = None
    last_column_mapping: Optional[dict] = None     # mapping used in extract (for caching)

    reconciliation: Optional[ReconciliationResult] = None

    output_files: Dict[str, bytes] = Field(default_factory=dict)
    output_filename: Optional[str] = None

    # ── SIPDO prompt optimization ────────────────────────────────────────
    is_unknown_broker: bool = False          # set by classify when no template/cache/SIPDO prompt
    sipdo_choice_pending: bool = False       # True while waiting for user choice
    sipdo_strategy: Optional[str] = None     # None | "quick" | "optimize"
    sipdo_optimized_prompt: Optional[str] = None      # generated extraction prompt
    sipdo_optimization_trace: Optional[list] = None   # iteration logs

    # ── HITL ─────────────────────────────────────────────────────────────
    hitl_pending: bool = False
    hitl_approved: bool = False              # set by resume endpoint
    hitl_feedback: Optional[str] = None     # free-text from reviewer

    # ── Persist ──────────────────────────────────────────────────────────
    db_session_id: Optional[str] = None      # same as session_id, stored on persist
    results_persisted: bool = False

    # ── MS data ──────────────────────────────────────────────────────────
    ms_data_loaded: bool = False

    # ── Audit log ────────────────────────────────────────────────────────
    logs: List[str] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True
