"""Status / history endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from broker_recon_flow.db.database import get_db
from broker_recon_flow.db.models import (
    ReconciliationSession, ExtractedTrade, ReconciliationResult as ReconResultORM,
)
from broker_recon_flow.services.ms_data_service import ms_data_stats, load_ms_data
from broker_recon_flow.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/ms-data")
def ms_data_info():
    """Return stats about the loaded MS receivables data."""
    return JSONResponse(ms_data_stats())


@router.get("/sessions")
def list_sessions(limit: int = 50, db: Session = Depends(get_db)):
    """Return the most recent reconciliation sessions."""
    rows = (
        db.query(ReconciliationSession)
        .order_by(ReconciliationSession.created_at.desc())
        .limit(limit)
        .all()
    )
    return JSONResponse([
        {
            "id": r.id,
            "broker_name": r.broker_name,
            "invoice_id": r.invoice_id,
            "status": r.status,
            "total_trades": r.total_trades,
            "matched_count": r.matched_count,
            "mismatched_count": r.mismatched_count,
            "new_trades_count": r.new_trades_count,
            "missing_trades_count": r.missing_trades_count,
            "output_file": r.output_file,
            "created_at": str(r.created_at),
        }
        for r in rows
    ])


@router.get("/sessions/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)):
    """Return detailed info for a single reconciliation session."""
    row = db.query(ReconciliationSession).filter_by(id=session_id).first()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return JSONResponse({
        "id": row.id,
        "broker_name": row.broker_name,
        "invoice_id": row.invoice_id,
        "pdf_filename": row.pdf_filename,
        "excel_filename": row.excel_filename,
        "status": row.status,
        "extraction_method": row.extraction_method,
        "template_type": row.template_type,
        "total_trades": row.total_trades,
        "matched_count": row.matched_count,
        "mismatched_count": row.mismatched_count,
        "new_trades_count": row.new_trades_count,
        "missing_trades_count": row.missing_trades_count,
        "output_file": row.output_file,
        "error_message": row.error_message,
        "created_at": str(row.created_at),
        "updated_at": str(row.updated_at),
    })


@router.get("/sessions/{session_id}/results")
def get_session_results(session_id: str, db: Session = Depends(get_db)):
    """Return trade-level reconciliation results for a session (from DB)."""
    session = db.query(ReconciliationSession).filter_by(id=session_id).first()
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    # Extracted trades
    trades = db.query(ExtractedTrade).filter_by(session_id=session_id).all()
    trades_data = [
        {
            "id": t.id,
            "trade_id": t.trade_id,
            "trade_date": t.trade_date,
            "instrument": t.instrument,
            "exchange": t.exchange,
            "buy_sell": t.buy_sell,
            "quantity": t.quantity,
            "price": t.price,
            "brokerage_amount": t.brokerage_amount,
            "currency": t.currency,
            "counterparty": t.counterparty,
            "client_account": t.client_account,
            "source_file": t.source_file,
            "source_type": t.source_type,
        }
        for t in trades
    ]

    # Reconciliation results
    results = db.query(ReconResultORM).filter_by(session_id=session_id).all()
    results_data = [
        {
            "id": r.id,
            "extracted_trade_id": r.extracted_trade_id,
            "status": r.status,
            "mismatch_reason": r.mismatch_reason,
            "differences": r.differences,
            "confidence_score": r.confidence_score,
            "ms_trade_id": r.ms_trade_id,
            "ms_trade_snapshot": r.ms_trade_snapshot,
        }
        for r in results
    ]

    return JSONResponse({
        "session_id": session_id,
        "broker_name": session.broker_name,
        "status": session.status,
        "total_trades": session.total_trades,
        "matched_count": session.matched_count,
        "mismatched_count": session.mismatched_count,
        "new_trades_count": session.new_trades_count,
        "missing_trades_count": session.missing_trades_count,
        "output_file": session.output_file,
        "extracted_trades": trades_data,
        "reconciliation_results": results_data,
    })


@router.get("/ms-data/preview")
def ms_data_preview(limit: int = Query(default=50, ge=1, le=500)):
    """Return a sample of the loaded MS receivables data."""
    df = load_ms_data()
    if df.empty:
        return JSONResponse({"rows": [], "columns": [], "total": 0})
    sample = df.head(limit)
    return JSONResponse({
        "rows": sample.to_dict(orient="records"),
        "columns": list(df.columns),
        "total": len(df),
    })


@router.get("/sipdo/prompts")
def list_sipdo_prompts():
    """Return all cached SIPDO-optimized prompts."""
    from broker_recon_flow.services.prompt_cache import list_all_prompts
    return JSONResponse(list_all_prompts())
