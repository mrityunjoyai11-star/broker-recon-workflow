"""Agent — Break Resolution (LLM).

For each MISMATCH case where user requested resolution:
  1. Classify break type (partial fill / booking error / price discrepancy / etc.)
  2. Rate severity (HIGH / MEDIUM / LOW)
  3. Draft a professional email to the broker
  4. Persist BreakResolution + AuditEvent
"""

from __future__ import annotations

import json
from datetime import datetime

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.db.models import BreakResolution, AuditEvent
from broker_recon_flow.schemas.canonical_trade import AuditEventType, Severity
from broker_recon_flow.services.llm_service import invoke_llm_json
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

RESOLUTION_SYSTEM_PROMPT = """You are a senior trade operations analyst specializing in broker reconciliation.

Given a list of trade breaks (mismatches between broker recap and internal records), for EACH break:
1. Classify the break type: partial_fill | booking_error | price_discrepancy | direction_mismatch | brokerage_discrepancy | other
2. Identify the root cause — explain WHY the mismatch likely occurred.
3. Rate severity:
   - HIGH: both quantity and price affected, or direction mismatch
   - MEDIUM: single critical field (quantity OR price)
   - LOW: brokerage-only discrepancy
4. Draft a professional email paragraph to the broker referencing the specific discrepancy and requesting an amended recap.

Return ONLY valid JSON (no markdown fences):
{
  "resolutions": [
    {
      "case_id": "...",
      "break_type": "...",
      "root_cause": "...",
      "severity": "high|medium|low",
      "email_paragraph": "Dear [Broker], We have identified a discrepancy in trade [trade_id]..."
    }
  ]
}"""


def run_resolution(
    session_id: str,
    broker_name: str,
    break_cases: list[dict],
) -> list[dict]:
    """Analyze break cases and produce resolutions.

    Args:
        break_cases: list of case dicts with case_id, broker_trade, ms_trade, differences, mismatch_reason
    Returns:
        list of resolution dicts
    """
    if not break_cases:
        return []

    # Build the context for the LLM
    breaks_context = []
    for case in break_cases:
        bt = case.get("broker_trade", {})
        mt = case.get("ms_trade", {})
        breaks_context.append({
            "case_id": case["case_id"],
            "trade_id": bt.get("trade_id", "unknown"),
            "instrument": bt.get("instrument", "unknown"),
            "trade_date": bt.get("trade_date", "unknown"),
            "broker_values": {
                "quantity": bt.get("quantity"),
                "price": bt.get("price"),
                "brokerage_amount": bt.get("brokerage_amount"),
                "buy_sell": bt.get("buy_sell"),
                "currency": bt.get("currency"),
            },
            "ms_values": {
                "quantity": mt.get("quantity"),
                "price": mt.get("price"),
                "brokerage_amount": mt.get("brokerage_amount"),
                "buy_sell": mt.get("buy_sell"),
                "currency": mt.get("currency"),
            },
            "differences": case.get("differences", {}),
            "mismatch_reason": case.get("mismatch_reason", ""),
        })

    user_prompt = (
        f"Broker: {broker_name}\n"
        f"Number of breaks: {len(breaks_context)}\n\n"
        f"Breaks:\n{json.dumps(breaks_context, indent=2, default=str)}"
    )

    # Single LLM call for all breaks
    llm_result = invoke_llm_json(RESOLUTION_SYSTEM_PROMPT, user_prompt)
    raw_resolutions = llm_result.get("resolutions", [])

    # Persist to DB
    factory = get_session_factory()
    db = factory()
    result_dicts: list[dict] = []

    try:
        for res in raw_resolutions:
            case_id = res.get("case_id", "")
            # Validate case_id is in our break_cases
            if not any(c["case_id"] == case_id for c in break_cases):
                logger.warning("Resolution for unknown case_id=%s, skipping", case_id)
                continue

            severity = res.get("severity", "medium").lower()
            if severity not in ("high", "medium", "low"):
                severity = "medium"

            # Build full email from paragraph
            email_paragraph = res.get("email_paragraph", "")
            draft_email = (
                f"Subject: Trade Discrepancy — Request for Amended Recap\n\n"
                f"Dear {broker_name} Operations Team,\n\n"
                f"{email_paragraph}\n\n"
                f"Please review and provide an amended recap at your earliest convenience.\n\n"
                f"Best regards,\n"
                f"Trade Operations"
            )

            br = BreakResolution(
                case_id=case_id,
                root_cause=res.get("root_cause", ""),
                break_type=res.get("break_type", "other"),
                severity=severity,
                draft_broker_email=draft_email,
            )
            db.add(br)
            db.flush()

            db.add(AuditEvent(
                session_id=session_id,
                case_id=case_id,
                event_type=AuditEventType.RESOLUTION_DRAFTED.value,
                actor="system",
                details={"severity": severity, "break_type": res.get("break_type"), "resolution_id": br.id},
            ))

            result_dicts.append({
                "resolution_id": br.id,
                "case_id": case_id,
                "break_type": res.get("break_type", "other"),
                "root_cause": res.get("root_cause", ""),
                "severity": severity,
                "draft_broker_email": draft_email,
                "human_approved": False,
            })

        db.commit()
        logger.info("Resolution agent: %d resolutions drafted for %d breaks", len(result_dicts), len(break_cases))
    except Exception as exc:
        logger.exception("Resolution agent DB error")
        db.rollback()
        raise
    finally:
        db.close()

    return result_dicts
