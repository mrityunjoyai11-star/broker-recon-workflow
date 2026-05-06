"""Agent — Case Router (deterministic, no LLM).

Creates one Case row per reconciliation result.
Sets has_breaks / has_ghosts flags on the session state.
Logs AuditEvent per case.
"""

from __future__ import annotations

from datetime import datetime

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.db.models import Case, AuditEvent
from broker_recon_flow.schemas.canonical_trade import (
    ReconciliationResult, ReconciliationStatus,
    CaseType, CaseStatus, AuditEventType,
)
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


def run_case_routing(
    session_id: str,
    reconciliation: ReconciliationResult,
) -> dict:
    """Create Case rows from reconciliation results.

    Returns dict with:
      cases: list[dict]  — serialised Case records
      has_breaks: bool
      has_ghosts: bool
    """
    factory = get_session_factory()
    db = factory()
    cases: list[dict] = []
    has_breaks = False
    has_ghosts = False

    try:
        # Log session-level reconciled event
        db.add(AuditEvent(
            session_id=session_id,
            event_type=AuditEventType.RECONCILED.value,
            actor="system",
            details=reconciliation.summary,
        ))

        def _create_case(match, case_type: CaseType) -> dict:
            nonlocal has_breaks, has_ghosts
            if case_type == CaseType.BREAK:
                has_breaks = True
            if case_type == CaseType.GHOST:
                has_ghosts = True

            case = Case(
                session_id=session_id,
                extracted_trade_id=match.broker_trade.id if match.broker_trade else None,
                case_type=case_type.value,
                status=CaseStatus.OPEN.value,
                broker_trade_snapshot=match.broker_trade.to_dict() if match.broker_trade else None,
                ms_trade_snapshot=match.ms_trade.to_dict() if match.ms_trade else None,
                differences=match.differences or None,
                confidence_score=match.confidence_score,
            )
            db.add(case)
            db.flush()  # get case.id

            db.add(AuditEvent(
                session_id=session_id,
                case_id=case.id,
                event_type=AuditEventType.CASE_CREATED.value,
                actor="system",
                details={"case_type": case_type.value, "trade_id": (match.broker_trade.trade_id if match.broker_trade else None)},
            ))

            return {
                "case_id": case.id,
                "case_type": case_type.value,
                "status": case.status,
                "broker_trade": match.broker_trade.to_dict() if match.broker_trade else None,
                "ms_trade": match.ms_trade.to_dict() if match.ms_trade else None,
                "differences": match.differences or {},
                "confidence_score": match.confidence_score,
                "mismatch_reason": match.mismatch_reason,
            }

        for m in reconciliation.matched:
            cases.append(_create_case(m, CaseType.MATCHED))
        for m in reconciliation.mismatched:
            cases.append(_create_case(m, CaseType.BREAK))
        for m in reconciliation.new_trades:
            cases.append(_create_case(m, CaseType.GHOST))
        for m in reconciliation.missing_trades:
            cases.append(_create_case(m, CaseType.MISSING))

        db.commit()
        logger.info(
            "Case routing: %d cases (matched=%d, breaks=%d, ghosts=%d, missing=%d)",
            len(cases),
            len(reconciliation.matched),
            len(reconciliation.mismatched),
            len(reconciliation.new_trades),
            len(reconciliation.missing_trades),
        )
    except Exception as exc:
        logger.exception("Case routing error")
        db.rollback()
        raise
    finally:
        db.close()

    return {"cases": cases, "has_breaks": has_breaks, "has_ghosts": has_ghosts}
