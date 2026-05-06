"""Agent — Escalation (LLM).

Drafts formal escalation emails to the Trade Support Group for
approved break cases with compiled evidence.
"""

from __future__ import annotations

import json
from datetime import datetime

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.db.models import EscalationRecord, AuditEvent
from broker_recon_flow.schemas.canonical_trade import AuditEventType
from broker_recon_flow.services.llm_service import invoke_llm_json
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

ESCALATION_SYSTEM_PROMPT = """You are a senior trade operations analyst drafting escalation emails
to the Trade Support Group (TSG).

For EACH break case, draft a formal escalation email that includes:
1. Case reference (case_id)
2. Break summary (what mismatched: fields, broker vs MS values)
3. Evidence summary (list of corroborating sources)
4. Urgency rating (HIGH / MEDIUM / LOW)
5. Recommended next steps

Return ONLY valid JSON (no markdown fences):
{
  "escalations": [
    {
      "case_id": "...",
      "urgency": "high|medium|low",
      "email_body": "Subject: ...\n\nDear TSG,\n\n..."
    }
  ]
}"""


def run_escalation(
    session_id: str,
    broker_name: str,
    break_cases: list[dict],
    evidence_packages: list[dict],
    resolution_results: list[dict],
) -> list[dict]:
    """Draft escalation emails for break cases.

    Returns list of escalation dicts.
    """
    if not break_cases:
        return []

    ev_by_case = {ep["case_id"]: ep for ep in evidence_packages}
    res_by_case = {r["case_id"]: r for r in resolution_results}

    context_items = []
    for case in break_cases:
        case_id = case["case_id"]
        ev = ev_by_case.get(case_id, {})
        res = res_by_case.get(case_id, {})
        context_items.append({
            "case_id": case_id,
            "trade_id": case.get("broker_trade", {}).get("trade_id", "unknown"),
            "break_type": res.get("break_type", "unknown"),
            "severity": res.get("severity", "medium"),
            "root_cause": res.get("root_cause", ""),
            "differences": case.get("differences", {}),
            "evidence_sources": [s["source_name"] for s in ev.get("sources", [])],
            "corroboration_summary": ev.get("corroboration_summary", ""),
        })

    user_prompt = (
        f"Broker: {broker_name}\n"
        f"Session: {session_id[:8]}\n"
        f"Break cases requiring escalation:\n{json.dumps(context_items, indent=2, default=str)}"
    )

    llm_result = invoke_llm_json(ESCALATION_SYSTEM_PROMPT, user_prompt)
    raw_escalations = llm_result.get("escalations", [])

    factory = get_session_factory()
    db = factory()
    drafts: list[dict] = []

    try:
        for esc in raw_escalations:
            case_id = esc.get("case_id", "")
            if not any(c["case_id"] == case_id for c in break_cases):
                continue

            urgency = esc.get("urgency", "medium").lower()
            if urgency not in ("high", "medium", "low"):
                urgency = "medium"

            er = EscalationRecord(
                case_id=case_id,
                draft_email=esc.get("email_body", ""),
                urgency=urgency,
            )
            db.add(er)
            db.flush()

            db.add(AuditEvent(
                session_id=session_id,
                case_id=case_id,
                event_type=AuditEventType.ESCALATION_DRAFTED.value,
                actor="system",
                details={"escalation_id": er.id, "urgency": urgency},
            ))

            drafts.append({
                "escalation_id": er.id,
                "case_id": case_id,
                "urgency": urgency,
                "draft_email": esc.get("email_body", ""),
                "human_approved": False,
            })

        db.commit()
        logger.info("Escalation agent: %d drafts created", len(drafts))
    except Exception as exc:
        logger.exception("Escalation agent error")
        db.rollback()
        raise
    finally:
        db.close()

    return drafts
