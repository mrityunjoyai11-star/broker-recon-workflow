"""Pipeline endpoints — start and resume the LangGraph workflow."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from broker_recon_flow.graph.workflow import get_graph
from broker_recon_flow.graph.state import GraphState
from broker_recon_flow.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


class StartRequest(BaseModel):
    session_id: str
    pdf_path: str
    excel_path: str
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
        excel_paths=req.excel_paths or [req.excel_path],
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
    Runs until the next interrupt (hitl_gate).
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

        # Stream until next interrupt (hitl_gate)
        for event in graph.stream(None, config=config):
            pass

        current = graph.get_state(config)
        state = GraphState(**current.values) if current else GraphState(session_id=req.session_id)
        logger.info("SIPDO choice done: session=%s status=%s", req.session_id, state.status)
        return JSONResponse(_serialise_state(state))

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
