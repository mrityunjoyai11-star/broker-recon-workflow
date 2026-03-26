"""Canonical data models for the brokerage reconciliation pipeline."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────

class FlowType(str, Enum):
    RECEIVABLE = "receivable"   # MS receives brokerage from brokers
    PAYABLE = "payable"         # MS pays brokerage to brokers


class BuySell(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class ReconciliationStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NEW = "NEW"          # In broker, not in MS
    MISSING = "MISSING"  # In MS, not in broker


class MismatchReason(str, Enum):
    MISMATCH_QTY = "MISMATCH_QTY"
    MISMATCH_PRICE = "MISMATCH_PRICE"
    MISMATCH_BROKERAGE = "MISMATCH_BROKERAGE"
    MISMATCH_CURRENCY = "MISMATCH_CURRENCY"
    MISMATCH_DIRECTION = "MISMATCH_DIRECTION"
    MULTIPLE_ISSUES = "MULTIPLE_ISSUES"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    VERIFYING = "verifying"
    CLASSIFYING = "classifying"
    SIPDO_CHOICE = "sipdo_choice"      # awaiting user: quick vs optimize
    OPTIMIZING = "optimizing"          # SIPDO prompt optimization in progress
    EXTRACTING = "extracting"
    HITL_REVIEW = "hitl_review"
    RECONCILING = "reconciling"
    GENERATING = "generating"
    PERSISTING = "persisting"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Core Trade Models ───────────────────────────────────────────────────────

class TradeRecord(BaseModel):
    """Canonical trade record — every broker format normalized to this."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    flow_type: str = FlowType.RECEIVABLE.value   # receivable | payable
    invoice_id: Optional[str] = None
    broker_name: Optional[str] = None
    invoice_date: Optional[str] = None
    trade_id: Optional[str] = None
    trade_date: Optional[str] = None
    instrument: Optional[str] = None
    exchange: Optional[str] = None
    buy_sell: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    delivery_start: Optional[str] = None
    delivery_end: Optional[str] = None
    counterparty: Optional[str] = None
    client_account: Optional[str] = None
    brokerage_rate: Optional[float] = None
    brokerage_amount: Optional[float] = None
    currency: Optional[str] = None
    source_file: Optional[str] = None
    source_type: Optional[str] = None   # "pdf" | "excel"
    raw_row: Optional[dict] = None

    def to_dict(self) -> dict:
        return self.model_dump(exclude={"raw_row"})


class MSTradeRecord(BaseModel):
    """Row from the MS receivables / payables database."""
    flow_type: str = FlowType.RECEIVABLE.value   # receivable | payable
    trade_id: Optional[str] = None
    trade_date: Optional[str] = None
    instrument: Optional[str] = None
    buy_sell: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    client_account: Optional[str] = None
    brokerage_amount: Optional[float] = None
    commission_rate: Optional[float] = None
    currency: Optional[str] = None
    broker_code: Optional[str] = None
    raw_row: Optional[dict] = None

    def to_dict(self) -> dict:
        return self.model_dump(exclude={"raw_row"})


# ── Agent Result Models ─────────────────────────────────────────────────────

class VerificationResult(BaseModel):
    broker_detected: Optional[str] = None
    invoice_id: Optional[str] = None
    doc_match: bool = False
    confidence: float = 0.0
    pdf_metadata: dict = Field(default_factory=dict)
    excel_metadata: dict = Field(default_factory=dict)
    mismatches: List[str] = Field(default_factory=list)
    message: str = ""


class ClassificationResult(BaseModel):
    template_type: Optional[str] = None
    parser_strategy: Optional[str] = None
    confidence: float = 0.0
    detected_keywords: List[str] = Field(default_factory=list)
    broker_name_detected: Optional[str] = None
    method: str = "rule_based"


class ExtractionResult(BaseModel):
    trades: List[TradeRecord] = Field(default_factory=list)
    trade_count: int = 0
    extraction_method: str = "structured"   # template | cached_template | fuzzy_match | llm_assisted
    confidence: float = 0.0
    warnings: List[str] = Field(default_factory=list)


class ReconciliationMatch(BaseModel):
    """Single reconciliation outcome for one broker trade."""
    broker_trade: Optional[TradeRecord] = None
    ms_trade: Optional[MSTradeRecord] = None
    status: ReconciliationStatus = ReconciliationStatus.NEW
    mismatch_reason: Optional[str] = None
    differences: dict = Field(default_factory=dict)
    confidence_score: int = 0               # 0–4: trade_id(1) + qty(1) + price(1) + brokerage(1)
    calculated_brokerage: Optional[float] = None


class ReconciliationResult(BaseModel):
    matched: List[ReconciliationMatch] = Field(default_factory=list)
    mismatched: List[ReconciliationMatch] = Field(default_factory=list)
    new_trades: List[ReconciliationMatch] = Field(default_factory=list)       # broker only
    missing_trades: List[ReconciliationMatch] = Field(default_factory=list)   # MS only
    summary: dict = Field(default_factory=dict)
