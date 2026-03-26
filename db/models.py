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
    column_mapping = Column(JSON, nullable=False)       # {raw_column: canonical_field}
    source_filename = Column(String, nullable=True)
    extraction_method = Column(String, nullable=True)   # fuzzy_match | llm_assisted
    hitl_approved = Column(Boolean, default=False)
    use_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OptimizedPromptCache(Base):
    """SIPDO-optimized extraction prompts cached per broker."""
    __tablename__ = "optimized_prompt_cache"

    id = Column(String, primary_key=True, default=_uuid)
    broker_name = Column(String, nullable=False, unique=True, index=True)
    prompt_text = Column(Text, nullable=False)
    accuracy_score = Column(Float, default=0.0)
    optimization_trace = Column(JSON, nullable=True)    # list of iteration dicts
    source_session_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
