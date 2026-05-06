"""LangGraph shared state for the reconciliation pipeline."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from broker_recon_flow.schemas.canonical_trade import (
    VerificationResult, ClassificationResult, ExtractionResult,
    ReconciliationResult, PipelineStatus, FlowType,
)


class GraphState(BaseModel):
    """Shared state flowing through all nodes of the pipeline."""

    # ── Input ────────────────────────────────────────────────────────────
    session_id: str = ""
    flow_type: str = FlowType.RECEIVABLE.value   # "receivable" | "payable"
    pdf_path: Optional[str] = None
    excel_path: Optional[str] = None
    # Multi-file support: additional file paths beyond the primary pair
    pdf_paths: List[str] = Field(default_factory=list)
    excel_paths: List[str] = Field(default_factory=list)
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
    sipdo_accuracy_score: Optional[float] = None      # final SIPDO accuracy 0.0-1.0

    # ── HITL ─────────────────────────────────────────────────────────────
    hitl_pending: bool = False
    hitl_approved: bool = False              # set by resume endpoint
    hitl_feedback: Optional[str] = None     # free-text from reviewer

    # ── Persist ──────────────────────────────────────────────────────────
    db_session_id: Optional[str] = None      # same as session_id, stored on persist
    results_persisted: bool = False
    parsed_file_path: Optional[str] = None   # path to saved parsed-trades Excel
    # ── MS data ──────────────────────────────────────────────────────────
    ms_data_loaded: bool = False

    # ── Case Management (Phase 2) ────────────────────────────────────────
    cases: List[Dict] = Field(default_factory=list)              # serialised Case dicts from case_router
    affirmation_pending: bool = False
    affirmation_decisions: Dict = Field(default_factory=dict)    # {case_id: "affirm"|"request_resolution"|"reject"|"flag_booking"|"escalate_tsg"}
    has_breaks: bool = False
    has_ghosts: bool = False

    # ── Resolution (Phase 3) ─────────────────────────────────────────────
    resolution_results: List[Dict] = Field(default_factory=list)
    break_review_pending: bool = False
    break_review_decisions: Dict = Field(default_factory=dict)   # {case_id: {approved, reviewer_notes, edited_email}}

    # ── Evidence + Escalation (Phase 4) ──────────────────────────────────
    evidence_packages: List[Dict] = Field(default_factory=list)
    escalation_drafts: List[Dict] = Field(default_factory=list)
    escalation_pending: bool = False
    escalation_decisions: Dict = Field(default_factory=dict)     # {case_id: {approved, reviewer_notes}}

    # ── Audit log ────────────────────────────────────────────────────────
    logs: List[str] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True
