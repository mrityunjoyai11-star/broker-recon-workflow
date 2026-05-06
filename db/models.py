"""SQLAlchemy ORM models for the brokerage reconciliation pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, Text, DateTime,
    ForeignKey, JSON
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class ReconciliationSession(Base):
    """Top-level record for every upload/run."""
    __tablename__ = "reconciliation_sessions"

    id = Column(String, primary_key=True, default=_uuid)
    flow_type = Column(String, default="receivable")   # receivable | payable
    broker_name = Column(String, nullable=True)
    invoice_id = Column(String, nullable=True)
    pdf_filename = Column(String, nullable=True)
    excel_filename = Column(String, nullable=True)
    status = Column(String, default="pending")          # pipeline status
    extraction_method = Column(String, nullable=True)
    template_type = Column(String, nullable=True)
    total_trades = Column(Integer, default=0)
    matched_count = Column(Integer, default=0)
    mismatched_count = Column(Integer, default=0)
    new_trades_count = Column(Integer, default=0)
    missing_trades_count = Column(Integer, default=0)
    output_file = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    extracted_trades = relationship("ExtractedTrade", back_populates="session", cascade="all, delete-orphan")
    reconciliation_results = relationship("ReconciliationResult", back_populates="session", cascade="all, delete-orphan")


class ExtractedTrade(Base):
    """One extracted broker trade row, stored for audit."""
    __tablename__ = "extracted_trades"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("reconciliation_sessions.id"), nullable=False)
    trade_id = Column(String, nullable=True)
    trade_date = Column(String, nullable=True)
    instrument = Column(String, nullable=True)
    exchange = Column(String, nullable=True)
    buy_sell = Column(String, nullable=True)
    quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    brokerage_rate = Column(Float, nullable=True)
    brokerage_amount = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    counterparty = Column(String, nullable=True)
    client_account = Column(String, nullable=True)
    delivery_start = Column(String, nullable=True)
    delivery_end = Column(String, nullable=True)
    source_file = Column(String, nullable=True)
    source_type = Column(String, nullable=True)
    raw_row = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ReconciliationSession", back_populates="extracted_trades")


class ReconciliationResult(Base):
    """Per-trade reconciliation outcome against MS data."""
    __tablename__ = "reconciliation_results"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("reconciliation_sessions.id"), nullable=False)
    extracted_trade_id = Column(String, ForeignKey("extracted_trades.id"), nullable=True)
    status = Column(String, nullable=False)             # MATCH / MISMATCH / NEW / MISSING
    mismatch_reason = Column(String, nullable=True)
    differences = Column(JSON, nullable=True)
    confidence_score = Column(Integer, default=0)       # 0-4
    ms_trade_id = Column(String, nullable=True)
    ms_trade_snapshot = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ReconciliationSession", back_populates="reconciliation_results")


class TemplateCache(Base):
    """Auto-learned broker column mappings (promoted after HITL approval)."""
    __tablename__ = "template_cache"

    id = Column(String, primary_key=True, default=_uuid)
    broker_name = Column(String, nullable=False, index=True)
    flow_type = Column(String, default="receivable")    # receivable | payable
    pdf_fingerprint = Column(String, nullable=True, index=True)  # SHA256 of first-3-page structure
    column_mapping = Column(JSON, nullable=False)       # {raw_column: canonical_field}
    source_filename = Column(String, nullable=True)
    extraction_method = Column(String, nullable=True)   # fuzzy_match | llm_assisted
    hitl_approved = Column(Boolean, default=False)
    use_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OptimizedPromptCache(Base):
    """SIPDO-optimized extraction prompts cached per broker + flow type."""
    __tablename__ = "optimized_prompt_cache"

    id = Column(String, primary_key=True, default=_uuid)
    broker_name = Column(String, nullable=False, index=True)
    flow_type = Column(String, default="receivable")    # receivable | payable
    pdf_fingerprint = Column(String, nullable=True, index=True)  # SHA256 of first-3-page structure
    prompt_text = Column(Text, nullable=False)
    accuracy_score = Column(Float, default=0.0)
    optimization_trace = Column(JSON, nullable=True)    # list of iteration dicts
    source_session_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── BrokerAI Phase 1–4 Models ────────────────────────────────────────────────

class Case(Base):
    """One case per trade in a reconciliation session."""
    __tablename__ = "cases"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("reconciliation_sessions.id"), nullable=False, index=True)
    extracted_trade_id = Column(String, nullable=True)    # FK to extracted_trades.id (NULL for MISSING)
    case_type = Column(String, nullable=False)            # matched / break / ghost / missing
    status = Column(String, default="open")               # open / affirmed / in_dispute / resolved / rejected / pending_booking / escalated
    broker_trade_snapshot = Column(JSON, nullable=True)    # serialised trade dict
    ms_trade_snapshot = Column(JSON, nullable=True)        # serialised MS trade dict
    differences = Column(JSON, nullable=True)              # per-field diff dict
    confidence_score = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    session = relationship("ReconciliationSession", backref="cases")


class AuditEvent(Base):
    """Append-only audit trail. NEVER update rows."""
    __tablename__ = "audit_events"

    id = Column(String, primary_key=True, default=_uuid)
    session_id = Column(String, ForeignKey("reconciliation_sessions.id"), nullable=False, index=True)
    case_id = Column(String, nullable=True, index=True)   # nullable — session-level events have no case
    event_type = Column(String, nullable=False)            # AuditEventType enum value
    actor = Column(String, default="system")               # "system" | "human"
    details = Column(JSON, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class BreakResolution(Base):
    """Resolution analysis for a MISMATCH case."""
    __tablename__ = "break_resolutions"

    id = Column(String, primary_key=True, default=_uuid)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    root_cause = Column(Text, nullable=True)               # e.g. "partial fill", "booking error"
    break_type = Column(String, nullable=True)              # partial_fill / booking_error / price_discrepancy / direction_mismatch / other
    severity = Column(String, default="medium")             # high / medium / low
    draft_broker_email = Column(Text, nullable=True)
    human_approved = Column(Boolean, default=False)
    reviewer_notes = Column(Text, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("Case", backref="resolutions")


class EvidencePackage(Base):
    """Compiled evidence for a break case."""
    __tablename__ = "evidence_packages"

    id = Column(String, primary_key=True, default=_uuid)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    sources = Column(JSON, nullable=True)                   # list of {source_name, source_type, data, corroborates_firm}
    corroboration_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("Case", backref="evidence_packages")


class EscalationRecord(Base):
    """Escalation email draft for trade support group."""
    __tablename__ = "escalation_records"

    id = Column(String, primary_key=True, default=_uuid)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    draft_email = Column(Text, nullable=True)
    urgency = Column(String, default="medium")              # high / medium / low
    human_approved = Column(Boolean, default=False)
    reviewer_notes = Column(Text, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    dispatched = Column(Boolean, default=False)             # True once email actually sent (Phase 7)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("Case", backref="escalation_records")
