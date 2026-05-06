"""Pipeline endpoints — start and resume the LangGraph workflow."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from broker_recon_flow.graph.workflow import get_graph
from broker_recon_flow.graph.state import GraphState
from broker_recon_flow.services.sipdo_progress import get_progress
from broker_recon_flow.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


class StartRequest(BaseModel):
    session_id: str
    pdf_path: str
    excel_path: str | None = None
    pdf_paths: list[str] = []
    excel_paths: list[str] = []
    broker_hint: str = ""
    flow_type: str = "receivable"


class ResumeRequest(BaseModel):
    session_id: str
    approved: bool
    feedback: str = ""


class SipdoChoiceRequest(BaseModel):
    session_id: str
    strategy: str = "quick"    # "quick" or "optimize"


def _serialise_match(m) -> dict:
    """Serialise a ReconciliationMatch to a JSON-safe dict."""
    row: dict = {
        "status": m.status.value if hasattr(m.status, "value") else str(m.status),
        "mismatch_reason": m.mismatch_reason,
        "confidence_score": m.confidence_score,
        "differences": m.differences or {},
    }
    if m.broker_trade:
        row["broker_trade"] = m.broker_trade.to_dict()
    if m.ms_trade:
        row["ms_trade"] = m.ms_trade.to_dict()
    return row


def _serialise_state(state: GraphState) -> dict:
    """Convert GraphState to a JSON-safe dict for the response."""
    data = {
        "session_id": state.session_id,
        "flow_type": state.flow_type,
        "status": state.status,
        "current_step": state.current_step,
        "error": state.error,
        "broker_name": state.broker_name,
        "template_type": state.template_type,
        "hitl_pending": state.hitl_pending,
        "hitl_approved": state.hitl_approved,
        "results_persisted": state.results_persisted,
        "output_filename": state.output_filename,
        "trade_count": state.extraction.trade_count if state.extraction else 0,
        "extraction_method": state.extraction.extraction_method if state.extraction else None,
        "extraction_confidence": state.extraction.confidence if state.extraction else None,
        "extraction_warnings": state.extraction.warnings if state.extraction else [],
        "recon_summary": state.reconciliation.summary if state.reconciliation else None,
        "last_column_mapping": state.last_column_mapping,
        # SIPDO fields
        "is_unknown_broker": state.is_unknown_broker,
        "sipdo_choice_pending": state.sipdo_choice_pending,
        "sipdo_strategy": state.sipdo_strategy,
        "sipdo_optimization_trace": state.sipdo_optimization_trace,
        "sipdo_accuracy_score": state.sipdo_accuracy_score,
        # BrokerAI Phase 2–4 fields
        "cases": state.cases[:500] if state.cases else [],
        "affirmation_pending": state.affirmation_pending,
        "affirmation_decisions": state.affirmation_decisions,
        "has_breaks": state.has_breaks,
        "has_ghosts": state.has_ghosts,
        "resolution_results": state.resolution_results[:100] if state.resolution_results else [],
        "break_review_pending": state.break_review_pending,
        "break_review_decisions": state.break_review_decisions,
        "evidence_packages": state.evidence_packages[:100] if state.evidence_packages else [],
        "escalation_drafts": state.escalation_drafts[:100] if state.escalation_drafts else [],
        "escalation_pending": state.escalation_pending,
        "escalation_decisions": state.escalation_decisions,
        "logs": state.logs[-20:],  # last 20 log entries
    }
    # Include trade records for HITL review
    if state.extraction and state.extraction.trades:
        data["trades"] = [t.to_dict() for t in state.extraction.trades[:500]]

    # Include reconciliation match details (limit to first 500 per bucket)
    if state.reconciliation:
        r = state.reconciliation
        data["recon_matched"] = [_serialise_match(m) for m in r.matched[:500]]
        data["recon_mismatched"] = [_serialise_match(m) for m in r.mismatched[:500]]
        data["recon_new"] = [_serialise_match(m) for m in r.new_trades[:500]]
        data["recon_missing"] = [_serialise_match(m) for m in r.missing_trades[:500]]

    return data


@router.post("/start")
async def start_pipeline(req: StartRequest):
    """
    Phase 1: verify → classify → extract → HITL pause.
    Returns state snapshot at the HITL interrupt point.
    """
    graph, checkpointer = get_graph()
    config = {"configurable": {"thread_id": req.session_id}}

    initial_state = GraphState(
        session_id=req.session_id,
        flow_type=req.flow_type,
        pdf_path=req.pdf_path,
        excel_path=req.excel_path,
        pdf_paths=req.pdf_paths or [req.pdf_path],
        excel_paths=req.excel_paths or ([req.excel_path] if req.excel_path else []),
        broker_hint=req.broker_hint or None,
    )

    logger.info("Pipeline start: session=%s", req.session_id)
    try:
        # Stream through nodes until the interrupt_before=["hitl_gate"] pause
        final = None
        for event in graph.stream(initial_state.model_dump(), config=config):
            final = event

        # After streaming stops, get the full current state
        current = graph.get_state(config)
        state = GraphState(**current.values) if current else initial_state
        logger.info("Pipeline paused at HITL: session=%s status=%s", req.session_id, state.status)
        return JSONResponse(_serialise_state(state))

    except Exception as exc:
        logger.exception("Pipeline start error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/resume")
async def resume_pipeline(req: ResumeRequest):
    """
    Phase 2: inject HITL decision then resume reconcile → generate → persist.
    """
    graph, checkpointer = get_graph()
    config = {"configurable": {"thread_id": req.session_id}}

    # Check there's a checkpoint to resume from
    current = graph.get_state(config)
    if not current:
        raise HTTPException(status_code=404, detail=f"No pipeline state found for session {req.session_id}")

    logger.info("Pipeline resume: session=%s approved=%s", req.session_id, req.approved)
    try:
        # Inject the HITL decision into the checkpoint
        graph.update_state(
            config,
            {
                "hitl_approved": req.approved,
                "hitl_feedback": req.feedback or None,
                "hitl_pending": False,
            },
        )

        # Stream remaining nodes
        for event in graph.stream(None, config=config):
            pass  # consume stream; state is persisted in checkpointer

        current = graph.get_state(config)
        state = GraphState(**current.values) if current else GraphState(session_id=req.session_id)
        logger.info("Pipeline complete: session=%s status=%s", req.session_id, state.status)
        return JSONResponse(_serialise_state(state))

    except Exception as exc:
        logger.exception("Pipeline resume error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/sipdo-choice")
async def sipdo_choice(req: SipdoChoiceRequest):
    """
    Inject SIPDO strategy choice then resume: sipdo_choice_gate →
    either sipdo_optimize → extract or extract directly.

    For "optimize" strategy, runs the graph stream in a background thread
    so the UI can poll progress via /sipdo-progress/{session_id}.
    """
    if req.strategy not in ("quick", "optimize"):
        raise HTTPException(status_code=400, detail="strategy must be 'quick' or 'optimize'")

    graph, checkpointer = get_graph()
    config = {"configurable": {"thread_id": req.session_id}}

    current = graph.get_state(config)
    if not current:
        raise HTTPException(status_code=404, detail=f"No pipeline state for session {req.session_id}")

    logger.info("SIPDO choice: session=%s strategy=%s", req.session_id, req.strategy)
    try:
        graph.update_state(
            config,
            {
                "sipdo_strategy": req.strategy,
                "sipdo_choice_pending": False,
            },
        )

        if req.strategy == "optimize":
            # Read current state to carry broker_name and other context to the UI
            cur_state = GraphState(**current.values) if current else None

            # Run graph asynchronously so the UI can poll progress
            def _run_graph_stream():
                try:
                    for event in graph.stream(None, config=config):
                        pass
                except Exception:
                    logger.exception("SIPDO optimize background stream error: session=%s", req.session_id)

            thread = threading.Thread(target=_run_graph_stream, daemon=True)
            thread.start()

            # Return immediately with "optimizing" status — include context fields
            return JSONResponse({
                "session_id": req.session_id,
                "status": "optimizing",
                "current_step": "sipdo_optimize",
                "broker_name": cur_state.broker_name if cur_state else None,
                "is_unknown_broker": cur_state.is_unknown_broker if cur_state else True,
                "flow_type": cur_state.flow_type if cur_state else "receivable",
                "logs": list(cur_state.logs[-10:]) if cur_state and cur_state.logs else [],
            })

        # "quick" strategy — also run async to avoid HTTP timeout on large PDFs
        cur_state = GraphState(**current.values) if current else None

        def _run_quick_stream():
            try:
                for event in graph.stream(None, config=config):
                    pass
                final = graph.get_state(config)
                if final:
                    s = GraphState(**final.values)
                    logger.info("SIPDO choice done: session=%s status=%s", req.session_id, s.status)
            except Exception:
                logger.exception("SIPDO quick background stream error: session=%s", req.session_id)

        thread = threading.Thread(target=_run_quick_stream, daemon=True)
        thread.start()

        # Return immediately — UI will poll /state/{session_id}
        return JSONResponse({
            "session_id": req.session_id,
            "status": "extracting",
            "current_step": "extract",
            "broker_name": cur_state.broker_name if cur_state else None,
            "is_unknown_broker": cur_state.is_unknown_broker if cur_state else True,
            "flow_type": cur_state.flow_type if cur_state else "receivable",
            "logs": list(cur_state.logs[-10:]) if cur_state and cur_state.logs else [],
        })

    except Exception as exc:
        logger.exception("SIPDO choice error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/state/{session_id}")
async def get_pipeline_state(session_id: str):
    """Retrieve the current pipeline state for a session (polling endpoint)."""
    graph, _ = get_graph()
    config = {"configurable": {"thread_id": session_id}}
    current = graph.get_state(config)
    if not current:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    state = GraphState(**current.values)
    return JSONResponse(_serialise_state(state))


@router.get("/sipdo-progress/{session_id}")
async def sipdo_progress(session_id: str):
    """Return live SIPDO optimization progress from the in-memory side-channel."""
    return JSONResponse(get_progress(session_id))


# ── BrokerAI Gate Endpoints ──────────────────────────────────────────────────

class AffirmRequest(BaseModel):
    session_id: str
    affirmation_decisions: dict = {}   # {case_id: "affirm"|"request_resolution"|"reject"|"flag_booking"|"escalate_tsg"}


class ApproveResolutionRequest(BaseModel):
    session_id: str
    break_review_decisions: dict = {}  # {case_id: {approved: bool, reviewer_notes: str, edited_email: str}}


class ApproveEscalationRequest(BaseModel):
    session_id: str
    escalation_decisions: dict = {}    # {case_id: {approved: bool, reviewer_notes: str}}


@router.post("/affirm")
async def affirm_pipeline(req: AffirmRequest):
    """Gate 2: inject affirmation decisions, update case statuses, resume graph."""
    graph, _ = get_graph()
    config = {"configurable": {"thread_id": req.session_id}}

    current = graph.get_state(config)
    if not current:
        raise HTTPException(status_code=404, detail=f"No pipeline state for session {req.session_id}")

    logger.info("Affirm: session=%s decisions=%d", req.session_id, len(req.affirmation_decisions))

    try:
        # Update case statuses in DB
        from broker_recon_flow.db.database import get_session_factory
        from broker_recon_flow.db.models import Case, AuditEvent
        from broker_recon_flow.schemas.canonical_trade import AuditEventType
        from datetime import datetime

        factory = get_session_factory()
        db = factory()
        try:
            for case_id, decision in req.affirmation_decisions.items():
                case = db.query(Case).filter(Case.id == case_id).first()
                if case:
                    if decision == "affirm":
                        case.status = "affirmed"
                        case.resolved_at = datetime.utcnow()
                    elif decision == "request_resolution":
                        case.status = "in_dispute"
                    elif decision == "reject":
                        case.status = "rejected"
                        case.resolved_at = datetime.utcnow()
                    elif decision == "flag_booking":
                        case.status = "pending_booking"
                    elif decision == "escalate_tsg":
                        case.status = "escalated"

                    db.add(AuditEvent(
                        session_id=req.session_id,
                        case_id=case_id,
                        event_type=AuditEventType.AFFIRMED.value,
                        actor="human",
                        details={"decision": decision},
                    ))
            db.commit()
        finally:
            db.close()

        graph.update_state(
            config,
            {
                "affirmation_decisions": req.affirmation_decisions,
                "affirmation_pending": False,
            },
        )

        for event in graph.stream(None, config=config):
            pass

        current = graph.get_state(config)
        state = GraphState(**current.values) if current else GraphState(session_id=req.session_id)
        logger.info("Affirm complete: session=%s status=%s", req.session_id, state.status)
        return JSONResponse(_serialise_state(state))

    except Exception as exc:
        logger.exception("Affirm error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/approve-resolution")
async def approve_resolution(req: ApproveResolutionRequest):
    """Gate 3: inject break review decisions, update resolutions, resume graph."""
    graph, _ = get_graph()
    config = {"configurable": {"thread_id": req.session_id}}

    current = graph.get_state(config)
    if not current:
        raise HTTPException(status_code=404, detail=f"No pipeline state for session {req.session_id}")

    logger.info("Approve resolution: session=%s decisions=%d", req.session_id, len(req.break_review_decisions))

    try:
        # Update BreakResolution rows in DB
        from broker_recon_flow.db.database import get_session_factory
        from broker_recon_flow.db.models import BreakResolution, AuditEvent
        from broker_recon_flow.schemas.canonical_trade import AuditEventType
        from datetime import datetime

        factory = get_session_factory()
        db = factory()
        try:
            for case_id, decision in req.break_review_decisions.items():
                if decision.get("approved"):
                    br = db.query(BreakResolution).filter(BreakResolution.case_id == case_id).first()
                    if br:
                        br.human_approved = True
                        br.reviewer_notes = decision.get("reviewer_notes", "")
                        if decision.get("edited_email"):
                            br.draft_broker_email = decision["edited_email"]
                        br.approved_at = datetime.utcnow()

                    db.add(AuditEvent(
                        session_id=req.session_id,
                        case_id=case_id,
                        event_type=AuditEventType.RESOLUTION_APPROVED.value,
                        actor="human",
                        details={"approved": True, "reviewer_notes": decision.get("reviewer_notes", "")},
                    ))
            db.commit()
        finally:
            db.close()

        graph.update_state(
            config,
            {
                "break_review_decisions": req.break_review_decisions,
                "break_review_pending": False,
            },
        )

        for event in graph.stream(None, config=config):
            pass

        current = graph.get_state(config)
        state = GraphState(**current.values) if current else GraphState(session_id=req.session_id)
        logger.info("Approve resolution complete: session=%s status=%s", req.session_id, state.status)
        return JSONResponse(_serialise_state(state))

    except Exception as exc:
        logger.exception("Approve resolution error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/approve-escalation")
async def approve_escalation(req: ApproveEscalationRequest):
    """Gate 4: inject escalation decisions, update records, resume graph."""
    graph, _ = get_graph()
    config = {"configurable": {"thread_id": req.session_id}}

    current = graph.get_state(config)
    if not current:
        raise HTTPException(status_code=404, detail=f"No pipeline state for session {req.session_id}")

    logger.info("Approve escalation: session=%s decisions=%d", req.session_id, len(req.escalation_decisions))

    try:
        from broker_recon_flow.db.database import get_session_factory
        from broker_recon_flow.db.models import EscalationRecord, AuditEvent
        from broker_recon_flow.schemas.canonical_trade import AuditEventType
        from datetime import datetime

        factory = get_session_factory()
        db = factory()
        try:
            for case_id, decision in req.escalation_decisions.items():
                if decision.get("approved"):
                    er = db.query(EscalationRecord).filter(EscalationRecord.case_id == case_id).first()
                    if er:
                        er.human_approved = True
                        er.reviewer_notes = decision.get("reviewer_notes", "")
                        er.approved_at = datetime.utcnow()
                        # dispatched stays False — actual send wired in Phase 7

                    db.add(AuditEvent(
                        session_id=req.session_id,
                        case_id=case_id,
                        event_type=AuditEventType.ESCALATION_APPROVED.value,
                        actor="human",
                        details={"approved": True, "reviewer_notes": decision.get("reviewer_notes", "")},
                    ))
            db.commit()
        finally:
            db.close()

        graph.update_state(
            config,
            {
                "escalation_decisions": req.escalation_decisions,
                "escalation_pending": False,
            },
        )

        for event in graph.stream(None, config=config):
            pass

        current = graph.get_state(config)
        state = GraphState(**current.values) if current else GraphState(session_id=req.session_id)
        logger.info("Approve escalation complete: session=%s status=%s", req.session_id, state.status)
        return JSONResponse(_serialise_state(state))

    except Exception as exc:
        logger.exception("Approve escalation error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))

# ── Email Draft Endpoint ─────────────────────────────────────────────────────

class SaveEmailDraftRequest(BaseModel):
    session_id: str
    broker_name: str = ""
    subject: str = ""
    body: str = ""


def _email_drafts_dir() -> Path:
    """Return the email_drafts_saved directory, creating it if needed."""
    d = Path(__file__).parent.parent.parent.parent / "data" / "email_drafts_saved"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize(name: str) -> str:
    for ch in r'/\:*?"<>|':
        name = name.replace(ch, "_")
    return name.replace(" ", "_")


@router.post("/save-email-draft")
async def save_email_draft(req: SaveEmailDraftRequest):
    """Save email draft to file + audit log. Includes the recon Excel as attachment ref."""
    from datetime import datetime
    try:
        # ── 1. Save draft file ───────────────────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_broker = _sanitize(req.broker_name or "unknown")
        draft_filename = f"email_draft_{safe_broker}_{req.session_id[:8]}_{timestamp}.txt"
        drafts_dir = _email_drafts_dir()

        # Find the matching recon Excel for this session
        output_dir = Path(__file__).parent.parent.parent.parent / "data" / "normalized_output"
        attachment_name = None
        if output_dir.exists():
            for f in sorted(output_dir.iterdir(), reverse=True):
                if f.name.endswith(".xlsx") and safe_broker.lower() in f.name.lower():
                    attachment_name = f.name
                    break
            # Fallback — scan by session_id prefix
            if not attachment_name:
                from broker_recon_flow.db.database import get_session_factory
                from broker_recon_flow.db.models import ReconciliationSession
                factory = get_session_factory()
                db = factory()
                try:
                    sess = db.query(ReconciliationSession).filter_by(id=req.session_id).first()
                    if sess and sess.output_file:
                        attachment_name = sess.output_file
                finally:
                    db.close()

        # Write the draft file
        draft_content = (
            f"Subject: {req.subject}\n"
            f"Session: {req.session_id}\n"
            f"Broker: {req.broker_name}\n"
            f"Saved: {datetime.now().isoformat()}\n"
            f"Attachment: {attachment_name or 'none'}\n"
            f"{'=' * 60}\n\n"
            f"{req.body}"
        )
        draft_path = drafts_dir / draft_filename
        draft_path.write_text(draft_content, encoding="utf-8")
        logger.info("Saved email draft file: %s", draft_path)

        # ── 2. Audit log entry ───────────────────────────────────────────
        from broker_recon_flow.db.database import get_session_factory
        from broker_recon_flow.db.models import AuditEvent
        from broker_recon_flow.schemas.canonical_trade import AuditEventType

        factory = get_session_factory()
        db = factory()
        try:
            db.add(AuditEvent(
                session_id=req.session_id,
                event_type=AuditEventType.EMAIL_DRAFT_SAVED.value,
                details={
                    "subject": req.subject,
                    "draft_file": draft_filename,
                    "attachment": attachment_name,
                },
                timestamp=datetime.utcnow(),
            ))
            db.commit()
        finally:
            db.close()

        return JSONResponse({
            "status": "saved",
            "draft_file": draft_filename,
            "attachment": attachment_name,
        })
    except Exception as exc:
        logger.exception("Save email draft error: session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/email-draft/{session_id}")
async def load_email_draft(session_id: str):
    """Load the most recent email draft for a session from the drafts directory."""
    try:
        drafts_dir = _email_drafts_dir()
        sid_short = session_id[:8]

        # Find the latest draft matching this session
        matches = sorted(
            [f for f in drafts_dir.iterdir() if sid_short in f.name and f.name.endswith(".txt")],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            return JSONResponse({"found": False})

        draft_path = matches[0]
        content = draft_path.read_text(encoding="utf-8")

        # Parse header fields
        subject = ""
        attachment = None
        body_start = content.find("\n\n")
        header_section = content[:body_start] if body_start > 0 else ""
        body = content[body_start + 2:] if body_start > 0 else content

        for line in header_section.split("\n"):
            if line.startswith("Subject: "):
                subject = line[len("Subject: "):]
            elif line.startswith("Attachment: "):
                val = line[len("Attachment: "):]
                if val and val != "none":
                    attachment = val

        return JSONResponse({
            "found": True,
            "subject": subject,
            "body": body,
            "attachment": attachment,
            "draft_file": draft_path.name,
            "saved_at": draft_path.stat().st_mtime,
        })
    except Exception as exc:
        logger.exception("Load email draft error: session=%s", session_id)
        raise HTTPException(status_code=500, detail=str(exc))