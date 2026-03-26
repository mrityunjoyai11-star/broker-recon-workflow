"""LangGraph node functions — one for each pipeline step.

7 core nodes + 3 SIPDO-related nodes:
  verify → classify → [sipdo_choice_gate] → [sipdo_optimize] → extract →
  [hitl_gate] → reconcile → generate → persist → [sipdo_background]
"""

from __future__ import annotations

from broker_recon_flow.agents import (
    verify_agent, classify_agent, extract_agent,
    reconcile_agent, template_agent, persist_agent,
)
from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.graph.state import GraphState
from broker_recon_flow.schemas.canonical_trade import PipelineStatus
from broker_recon_flow.services import ms_data_service as ms_svc
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


# ── Node 1: Verify ───────────────────────────────────────────────────────────

def verify_node(state: GraphState) -> dict:
    logger.info("[verify_node] session=%s", state.session_id)
    updates: dict = {"status": PipelineStatus.VERIFYING.value, "current_step": "verify"}

    if not state.pdf_path or not state.excel_path:
        return {**updates, "error": "Both PDF and Excel paths are required", "status": PipelineStatus.FAILED.value}

    try:
        result = verify_agent.run_verification(state.pdf_path, state.excel_path)
        updates["verification"] = result
        if result.broker_detected:
            updates["broker_name"] = result.broker_detected
        log = f"Verify: match={result.doc_match}, confidence={result.confidence:.2f}, broker={result.broker_detected}"
        updates["logs"] = state.logs + [log]
    except Exception as exc:
        logger.exception("verify_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value

    return updates


# ── Node 2: Classify ─────────────────────────────────────────────────────────

def classify_node(state: GraphState) -> dict:
    logger.info("[classify_node] session=%s", state.session_id)
    updates: dict = {"status": PipelineStatus.CLASSIFYING.value, "current_step": "classify"}

    # Open a DB session for TemplateCache lookup
    factory = get_session_factory()
    db = factory()
    try:
        result = classify_agent.run_classification(
            pdf_path=state.pdf_path,
            excel_path=state.excel_path,
            broker_hint=state.broker_hint or state.broker_name,
            db_session=db,
        )
        updates["classification"] = result
        if result.broker_name_detected:
            updates["broker_name"] = result.broker_name_detected
        if result.template_type:
            updates["template_type"] = result.template_type

        # If cached_template strategy, load the mapping from DB
        if result.parser_strategy == "cached_template" and result.broker_name_detected:
            mapping = classify_agent._check_template_cache(db, result.broker_name_detected)
            if mapping:
                updates["cached_column_mapping"] = mapping

        # Determine if this is an unknown broker (no YAML template, no cached
        # mapping) — if so, also check for a SIPDO-optimized prompt cache hit.
        has_template = result.template_type is not None
        has_cache = result.parser_strategy == "cached_template"
        has_sipdo = False
        if not has_template and not has_cache:
            try:
                from broker_recon_flow.services.prompt_cache import get_cached_prompt
                broker = result.broker_name_detected or state.broker_name
                if broker and get_cached_prompt(broker):
                    has_sipdo = True
            except Exception:
                pass

        updates["is_unknown_broker"] = not (has_template or has_cache or has_sipdo)
        if updates["is_unknown_broker"]:
            updates["sipdo_choice_pending"] = True

        log = f"Classify: template={result.template_type}, strategy={result.parser_strategy}, broker={result.broker_name_detected}, unknown={updates['is_unknown_broker']}"
        updates["logs"] = state.logs + [log]
    except Exception as exc:
        logger.exception("classify_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value
    finally:
        db.close()

    return updates


# ── Node 3: Extract ──────────────────────────────────────────────────────────

def extract_node(state: GraphState) -> dict:
    logger.info("[extract_node] session=%s", state.session_id)
    updates: dict = {"status": PipelineStatus.EXTRACTING.value, "current_step": "extract"}

    try:
        result, column_mapping_used = extract_agent.run_extraction(
            pdf_path=state.pdf_path,
            excel_path=state.excel_path,
            template_type=state.template_type,
            broker_name=state.broker_name,
            invoice_id=state.verification.invoice_id if state.verification else None,
            cached_column_mapping=state.cached_column_mapping,
            sipdo_prompt=state.sipdo_optimized_prompt,
        )
        updates["extraction"] = result
        updates["hitl_pending"] = True   # always pause for HITL after extract
        updates["status"] = PipelineStatus.HITL_REVIEW.value  # set BEFORE interrupt
        if column_mapping_used:
            updates["last_column_mapping"] = column_mapping_used
        log = f"Extract: {result.trade_count} trades via {result.extraction_method} (confidence={result.confidence:.2f})"
        updates["logs"] = state.logs + [log]
    except Exception as exc:
        logger.exception("extract_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value

    return updates


# ── Node 4: HITL Gate ────────────────────────────────────────────────────────
# This node just rotates the status to HITL_REVIEW. LangGraph interrupt_before
# pauses here; the FastAPI /resume endpoint calls graph.update_state then streams.

def hitl_gate_node(state: GraphState) -> dict:
    logger.info("[hitl_gate] session=%s awaiting human review", state.session_id)
    return {
        "status": PipelineStatus.HITL_REVIEW.value,
        "current_step": "hitl_gate",
        "logs": state.logs + ["HITL gate: awaiting reviewer approval"],
    }


# ── Node 5: Reconcile ────────────────────────────────────────────────────────

def reconcile_node(state: GraphState) -> dict:
    logger.info("[reconcile_node] session=%s", state.session_id)
    updates: dict = {"status": PipelineStatus.RECONCILING.value, "current_step": "reconcile"}

    # Ensure MS data is loaded
    if not state.ms_data_loaded:
        ms_svc.load_ms_data()
        updates["ms_data_loaded"] = True

    try:
        broker_trades = state.extraction.trades if state.extraction else []
        result = reconcile_agent.run_reconciliation(
            broker_trades=broker_trades,
            broker_name=state.broker_name,
        )
        updates["reconciliation"] = result
        log = (
            f"Reconcile: matched={result.summary.get('matched_count', 0)}, "
            f"mismatched={result.summary.get('mismatched_count', 0)}, "
            f"new={result.summary.get('new_trades_count', 0)}, "
            f"missing={result.summary.get('missing_trades_count', 0)}"
        )
        updates["logs"] = state.logs + [log]
    except Exception as exc:
        logger.exception("reconcile_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value

    return updates


# ── Node 6: Generate ─────────────────────────────────────────────────────────

def generate_node(state: GraphState) -> dict:
    logger.info("[generate_node] session=%s", state.session_id)
    updates: dict = {"status": PipelineStatus.GENERATING.value, "current_step": "generate"}

    try:
        output = template_agent.run_template_generation(
            extraction=state.extraction,
            reconciliation=state.reconciliation,
            broker_name=state.broker_name,
        )
        filename = list(output.keys())[0] if output else None
        updates["output_files"] = output
        updates["output_filename"] = filename
        updates["logs"] = state.logs + [f"Generate: created {filename}"]
    except Exception as exc:
        logger.exception("generate_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value

    return updates


# ── Node 7: Persist ──────────────────────────────────────────────────────────

def persist_node(state: GraphState) -> dict:
    logger.info("[persist_node] session=%s", state.session_id)
    updates: dict = {"status": PipelineStatus.PERSISTING.value, "current_step": "persist"}

    factory = get_session_factory()
    db = factory()
    try:
        # Save output file bytes to disk if present
        output_file_path = None
        if state.output_files and state.output_filename:
            from broker_recon_flow.services.storage_service import save_output_file
            data = state.output_files[state.output_filename]
            saved = save_output_file(data, state.output_filename)
            output_file_path = str(saved)

        persist_agent.persist_results(
            db=db,
            session_id=state.session_id,
            extraction=state.extraction,
            reconciliation=state.reconciliation,
            broker_name=state.broker_name,
            invoice_id=state.verification.invoice_id if state.verification else None,
            output_file=output_file_path,
            column_mapping=state.last_column_mapping,
            extraction_method=state.extraction.extraction_method if state.extraction else None,
            template_type=state.template_type,
            hitl_approved=state.hitl_approved,
            pdf_filename=state.pdf_path,
            excel_filename=state.excel_path,
        )
        updates["results_persisted"] = True
        updates["db_session_id"] = state.session_id
        updates["status"] = PipelineStatus.COMPLETED.value
        updates["logs"] = state.logs + ["Persist: results saved to database"]
    except Exception as exc:
        logger.exception("persist_node error")
        updates["error"] = str(exc)
        updates["status"] = PipelineStatus.FAILED.value
    finally:
        db.close()

    return updates


# ── Node: SIPDO Choice Gate ──────────────────────────────────────────────────
# LangGraph interrupt_before pauses here for unknown brokers.
# The FastAPI /sipdo-choice endpoint calls graph.update_state to inject
# sipdo_strategy ("quick" or "optimize"), then resumes streaming.

def sipdo_choice_gate_node(state: GraphState) -> dict:
    logger.info("[sipdo_choice_gate] session=%s, strategy=%s", state.session_id, state.sipdo_strategy)
    return {
        "status": PipelineStatus.SIPDO_CHOICE.value,
        "current_step": "sipdo_choice_gate",
        "logs": state.logs + [
            f"SIPDO choice gate: unknown broker '{state.broker_name}' — "
            f"strategy={'pending' if not state.sipdo_strategy else state.sipdo_strategy}"
        ],
    }


# ── Node: SIPDO Optimize ────────────────────────────────────────────────────
# Runs the full SIPDO prompt optimization pipeline inline (user chose "optimize").

def sipdo_optimize_node(state: GraphState) -> dict:
    logger.info("[sipdo_optimize] session=%s, broker=%s", state.session_id, state.broker_name)
    updates: dict = {
        "status": PipelineStatus.OPTIMIZING.value,
        "current_step": "sipdo_optimize",
    }

    try:
        from broker_recon_flow.services.prompt_optimizer import run_optimization

        logs_so_far = list(state.logs)

        def progress_cb(msg: str):
            logs_so_far.append(msg)

        result = run_optimization(
            broker_name=state.broker_name or "unknown",
            pdf_path=state.pdf_path,
            excel_path=state.excel_path,
            progress_callback=progress_cb,
        )
        updates["sipdo_optimized_prompt"] = result.get("optimized_prompt")
        updates["sipdo_optimization_trace"] = result.get("trace", [])
        logs_so_far.append(
            f"SIPDO optimization complete: accuracy={result.get('accuracy_score', 0):.0%}, "
            f"iterations={result.get('iteration_count', 0)}"
        )
        updates["logs"] = logs_so_far

        # Cache the result
        from broker_recon_flow.services.prompt_cache import save_optimized_prompt
        save_optimized_prompt(
            broker_name=state.broker_name or "unknown",
            prompt_text=result.get("optimized_prompt", ""),
            accuracy_score=result.get("accuracy_score", 0.0),
            optimization_trace=result.get("trace", []),
            source_session_id=state.session_id,
        )
    except Exception as exc:
        logger.exception("sipdo_optimize error")
        updates["logs"] = state.logs + [f"SIPDO optimization failed: {exc} — falling back to generic LLM"]
        # Don't set error/FAILED — fall through to generic extraction

    return updates


# ── Node: SIPDO Background ──────────────────────────────────────────────────
# After persist (quick path only): runs SIPDO using HITL-approved trades as
# ground truth and caches the result for future uploads from this broker.

def sipdo_background_node(state: GraphState) -> dict:
    logger.info("[sipdo_background] session=%s, broker=%s", state.session_id, state.broker_name)
    updates: dict = {"current_step": "sipdo_background"}

    try:
        from broker_recon_flow.services.prompt_optimizer import run_optimization

        approved_trades = None
        if state.extraction and state.extraction.trades:
            approved_trades = [t.to_dict() for t in state.extraction.trades]

        result = run_optimization(
            broker_name=state.broker_name or "unknown",
            pdf_path=state.pdf_path,
            excel_path=state.excel_path,
            expected_trades=approved_trades,
            progress_callback=lambda msg: logger.info("[sipdo_background] %s", msg),
        )

        from broker_recon_flow.services.prompt_cache import save_optimized_prompt
        save_optimized_prompt(
            broker_name=state.broker_name or "unknown",
            prompt_text=result.get("optimized_prompt", ""),
            accuracy_score=result.get("accuracy_score", 0.0),
            optimization_trace=result.get("trace", []),
            source_session_id=state.session_id,
        )
        updates["logs"] = state.logs + [
            f"Background SIPDO: optimized prompt cached for '{state.broker_name}' "
            f"(accuracy={result.get('accuracy_score', 0):.0%})"
        ]
    except Exception as exc:
        logger.exception("sipdo_background error")
        updates["logs"] = state.logs + [f"Background SIPDO failed (non-fatal): {exc}"]

    updates["status"] = PipelineStatus.COMPLETED.value
    return updates


# ── Routing functions ────────────────────────────────────────────────────────

def route_after_verify(state: GraphState) -> str:
    if state.error or state.status == PipelineStatus.FAILED.value:
        return "end"
    return "classify"


def route_after_classify(state: GraphState) -> str:
    if state.error or state.status == PipelineStatus.FAILED.value:
        return "end"
    if state.is_unknown_broker:
        return "sipdo_choice_gate"
    return "extract"


def route_after_sipdo_choice(state: GraphState) -> str:
    """After user picks quick or optimize."""
    if state.sipdo_strategy == "optimize":
        return "sipdo_optimize"
    return "extract"       # "quick" or default


def route_after_sipdo_optimize(state: GraphState) -> str:
    """After SIPDO optimization completes, proceed to extraction."""
    return "extract"


def route_after_hitl(state: GraphState) -> str:
    """After HITL gate resumes: check if approved."""
    if not state.hitl_approved:
        return "end"
    return "reconcile"


def route_after_persist(state: GraphState) -> str:
    """After persist: run SIPDO in background if user chose quick on unknown broker."""
    if state.is_unknown_broker and state.sipdo_strategy == "quick":
        return "sipdo_background"
    return "end"


def route_after_extract(state: GraphState) -> str:
    if state.error:
        return "end"
    return "hitl_gate"
