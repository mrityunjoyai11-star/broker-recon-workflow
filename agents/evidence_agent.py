"""Agent — Evidence Compilation (deterministic, no LLM).

For each approved break resolution, compiles an evidence package from
available sources to support the firm's position.
"""

from __future__ import annotations

from datetime import datetime

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.db.models import EvidencePackage, AuditEvent
from broker_recon_flow.schemas.canonical_trade import AuditEventType
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


def run_evidence_compilation(
    session_id: str,
    broker_name: str,
    approved_cases: list[dict],
    resolution_results: list[dict],
) -> list[dict]:
    """Compile evidence packages for approved break cases.

    Args:
        approved_cases: list of case dicts (break cases where resolution was approved)
        resolution_results: list of resolution dicts from resolution agent
    Returns:
        list of evidence package dicts
    """
    if not approved_cases:
        return []

    # Build resolution lookup
    res_by_case = {r["case_id"]: r for r in resolution_results}

    factory = get_session_factory()
    db = factory()
    packages: list[dict] = []

    try:
        for case in approved_cases:
            case_id = case["case_id"]
            bt = case.get("broker_trade", {})
            mt = case.get("ms_trade", {})
            diffs = case.get("differences", {})
            resolution = res_by_case.get(case_id, {})

            sources = [
                {
                    "source_name": "Broker Trade Extract",
                    "source_type": "pdf_extraction",
                    "data": {
                        "trade_id": bt.get("trade_id"),
                        "trade_date": bt.get("trade_date"),
                        "instrument": bt.get("instrument"),
                        "quantity": bt.get("quantity"),
                        "price": bt.get("price"),
                        "brokerage_amount": bt.get("brokerage_amount"),
                        "buy_sell": bt.get("buy_sell"),
                        "broker_name": broker_name,
                    },
                    "corroborates_firm": False,  # this is the broker's claim
                },
                {
                    "source_name": "MS Internal Booking",
                    "source_type": "oms_record",
                    "data": {
                        "trade_id": mt.get("trade_id"),
                        "trade_date": mt.get("trade_date"),
                        "instrument": mt.get("instrument"),
                        "quantity": mt.get("quantity"),
                        "price": mt.get("price"),
                        "brokerage_amount": mt.get("brokerage_amount"),
                        "buy_sell": mt.get("buy_sell"),
                    },
                    "corroborates_firm": True,
                },
                {
                    "source_name": "Reconciliation Diff",
                    "source_type": "recon_engine",
                    "data": diffs,
                    "corroborates_firm": True,
                },
                {
                    "source_name": "Resolution Analysis",
                    "source_type": "ai_analysis",
                    "data": {
                        "root_cause": resolution.get("root_cause", ""),
                        "break_type": resolution.get("break_type", ""),
                        "severity": resolution.get("severity", ""),
                    },
                    "corroborates_firm": True,
                },
                {
                    "source_name": "Bloomberg Execution Data",
                    "source_type": "market_data",
                    "data": "pending_integration",
                    "corroborates_firm": None,  # not yet connected
                },
            ]

            corroborating = sum(1 for s in sources if s["corroborates_firm"] is True)
            summary = (
                f"{corroborating} of {len(sources)} evidence sources corroborate the firm's position. "
                f"Break type: {resolution.get('break_type', 'unknown')}. "
                f"Severity: {resolution.get('severity', 'unknown')}."
            )

            ep = EvidencePackage(
                case_id=case_id,
                sources=sources,
                corroboration_summary=summary,
            )
            db.add(ep)
            db.flush()

            db.add(AuditEvent(
                session_id=session_id,
                case_id=case_id,
                event_type=AuditEventType.EVIDENCE_COMPILED.value,
                actor="system",
                details={"evidence_id": ep.id, "source_count": len(sources), "corroborating": corroborating},
            ))

            packages.append({
                "evidence_id": ep.id,
                "case_id": case_id,
                "sources": sources,
                "corroboration_summary": summary,
            })

        db.commit()
        logger.info("Evidence agent: %d packages compiled", len(packages))
    except Exception as exc:
        logger.exception("Evidence compilation error")
        db.rollback()
        raise
    finally:
        db.close()

    return packages
