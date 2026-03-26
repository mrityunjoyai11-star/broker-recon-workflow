"""Agent 6 — Persist Agent (NEW).

Writes pipeline results to SQLite:
  - Creates / updates ReconciliationSession
  - Inserts ExtractedTrade rows
  - Inserts ReconciliationResult rows
  - Upserts TemplateCache on HITL approval (hitl_approved=True)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from broker_recon_flow.db.models import (
    ReconciliationSession, ExtractedTrade, ReconciliationResult as ORMReconResult,
    TemplateCache,
)
from broker_recon_flow.schemas.canonical_trade import (
    ExtractionResult, ReconciliationResult, ReconciliationMatch,
)
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


def persist_results(
    db: Session,
    session_id: str,
    extraction: ExtractionResult,
    reconciliation: ReconciliationResult,
    broker_name: str | None = None,
    invoice_id: str | None = None,
    output_file: str | None = None,
    column_mapping: dict | None = None,
    extraction_method: str | None = None,
    template_type: str | None = None,
    hitl_approved: bool = False,
    pdf_filename: str | None = None,
    excel_filename: str | None = None,
) -> ReconciliationSession:
    """Write all pipeline results to the DB. Returns the updated session ORM object."""
    s = reconciliation.summary

    # ── 1. Update or create ReconciliationSession ───────────────────────
    session_row = db.query(ReconciliationSession).filter_by(id=session_id).first()
    if session_row is None:
        session_row = ReconciliationSession(id=session_id)
        db.add(session_row)

    session_row.broker_name = broker_name
    session_row.invoice_id = invoice_id
    session_row.pdf_filename = pdf_filename
    session_row.excel_filename = excel_filename
    session_row.status = "completed"
    session_row.extraction_method = extraction.extraction_method
    session_row.template_type = template_type
    session_row.total_trades = extraction.trade_count
    session_row.matched_count = s.get("matched_count", 0)
    session_row.mismatched_count = s.get("mismatched_count", 0)
    session_row.new_trades_count = s.get("new_trades_count", 0)
    session_row.missing_trades_count = s.get("missing_trades_count", 0)
    session_row.output_file = output_file
    session_row.updated_at = datetime.utcnow()

    # ── 2. Insert ExtractedTrade rows ────────────────────────────────────
    # Build a lookup by trade identity so we can link recon results to trades
    trade_id_to_orm: dict[str, ExtractedTrade] = {}
    for trade in extraction.trades:
        row = ExtractedTrade(
            session_id=session_id,
            trade_id=trade.trade_id,
            trade_date=trade.trade_date,
            instrument=trade.instrument,
            exchange=trade.exchange,
            buy_sell=trade.buy_sell,
            quantity=trade.quantity,
            price=trade.price,
            brokerage_rate=trade.brokerage_rate,
            brokerage_amount=trade.brokerage_amount,
            currency=trade.currency,
            counterparty=trade.counterparty,
            client_account=trade.client_account,
            delivery_start=trade.delivery_start,
            delivery_end=trade.delivery_end,
            source_file=trade.source_file,
            source_type=trade.source_type,
            raw_row=trade.raw_row,
        )
        db.add(row)
        # Key by the pydantic model id for later FK linking
        trade_id_to_orm[trade.id] = row

    # Flush to get ORM-generated IDs for reconciliation result FK
    db.flush()

    # ── 3. Insert ReconciliationResult rows ──────────────────────────────
    def _add_recon_row(match: ReconciliationMatch, status: str) -> None:
        # Link to the ExtractedTrade ORM row if the broker_trade is present
        extracted_fk = None
        if match.broker_trade and match.broker_trade.id in trade_id_to_orm:
            extracted_fk = trade_id_to_orm[match.broker_trade.id].id
        db.add(ORMReconResult(
            session_id=session_id,
            extracted_trade_id=extracted_fk,
            status=status,
            mismatch_reason=match.mismatch_reason,
            differences=match.differences or None,
            confidence_score=match.confidence_score,
            ms_trade_id=match.ms_trade.trade_id if match.ms_trade else None,
            ms_trade_snapshot=match.ms_trade.to_dict() if match.ms_trade else None,
        ))

    for m in reconciliation.matched:
        _add_recon_row(m, "MATCH")
    for m in reconciliation.mismatched:
        _add_recon_row(m, "MISMATCH")
    for m in reconciliation.new_trades:
        _add_recon_row(m, "NEW")
    for m in reconciliation.missing_trades:
        _add_recon_row(m, "MISSING")

    # ── 4. Upsert TemplateCache (only on HITL approval) ──────────────────
    if hitl_approved and column_mapping and broker_name:
        _upsert_template_cache(db, broker_name, column_mapping, extraction.extraction_method)

    db.commit()
    logger.info("Persisted session %s: %d trades, %d recon rows", session_id, extraction.trade_count,
                len(reconciliation.matched) + len(reconciliation.mismatched) +
                len(reconciliation.new_trades) + len(reconciliation.missing_trades))
    return session_row


def _upsert_template_cache(db: Session, broker_name: str, column_mapping: dict, method: str | None) -> None:
    existing = (
        db.query(TemplateCache)
        .filter(TemplateCache.broker_name.ilike(f"%{broker_name}%"))
        .first()
    )
    if existing:
        existing.column_mapping = column_mapping
        existing.extraction_method = method
        existing.hitl_approved = True
        existing.use_count += 1
        existing.updated_at = datetime.utcnow()
        logger.info("Updated TemplateCache for broker: %s", broker_name)
    else:
        db.add(TemplateCache(
            broker_name=broker_name,
            column_mapping=column_mapping,
            extraction_method=method,
            hitl_approved=True,
            use_count=1,
        ))
        logger.info("Created TemplateCache for broker: %s", broker_name)
