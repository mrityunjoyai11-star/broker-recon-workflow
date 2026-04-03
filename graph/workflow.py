"""LangGraph StateGraph for the brokerage reconciliation pipeline.

Topology (with SIPDO prompt optimization + HITL re-extract loop):
  verify → classify → [conditional] →
    ├─ known broker: extract → [interrupt] hitl_gate →
    │      ├─ approved → reconcile → generate → persist → END
    │      └─ rejected → re_extract_gate → [interrupt] sipdo_choice_gate → ...
    └─ unknown broker: [interrupt] sipdo_choice_gate →
         ├─ "optimize": sipdo_optimize → extract → [interrupt] hitl_gate → ...
         └─ "quick": extract → [interrupt] hitl_gate → ... → persist → sipdo_background → END

HITL pause via interrupt_before=["hitl_gate"].
SIPDO choice pause via interrupt_before=["sipdo_choice_gate"].
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from broker_recon_flow.graph.state import GraphState
from broker_recon_flow.graph.nodes import (
    verify_node,
    classify_node,
    extract_node,
    hitl_gate_node,
    re_extract_gate_node,
    reconcile_node,
    generate_node,
    persist_node,
    sipdo_choice_gate_node,
    sipdo_optimize_node,
    sipdo_background_node,
    route_after_verify,
    route_after_classify,
    route_after_sipdo_choice,
    route_after_sipdo_optimize,
    route_after_extract,
    route_after_hitl,
    route_after_re_extract_gate,
    route_after_persist,
)
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# Module-level compiled graph (lazy-initialized via get_graph())
_graph = None
_checkpointer = None


def build_workflow():
    """Build and compile the StateGraph. Returns (compiled_graph, checkpointer)."""
    builder = StateGraph(GraphState)

    # ── Add nodes ────────────────────────────────────────────────────────
    builder.add_node("verify", verify_node)
    builder.add_node("classify", classify_node)
    builder.add_node("sipdo_choice_gate", sipdo_choice_gate_node)
    builder.add_node("sipdo_optimize", sipdo_optimize_node)
    builder.add_node("extract", extract_node)
    builder.add_node("hitl_gate", hitl_gate_node)
    builder.add_node("re_extract_gate", re_extract_gate_node)
    builder.add_node("reconcile", reconcile_node)
    builder.add_node("generate", generate_node)
    builder.add_node("persist", persist_node)
    builder.add_node("sipdo_background", sipdo_background_node)

    # ── Entry point ──────────────────────────────────────────────────────
    builder.set_entry_point("verify")

    # ── Edges ────────────────────────────────────────────────────────────
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {"classify": "classify", "end": END},
    )
    # classify → known broker → extract | unknown broker → sipdo_choice_gate
    builder.add_conditional_edges(
        "classify",
        route_after_classify,
        {"extract": "extract", "sipdo_choice_gate": "sipdo_choice_gate", "end": END},
    )
    # sipdo_choice_gate → "optimize" → sipdo_optimize | "quick" → extract
    builder.add_conditional_edges(
        "sipdo_choice_gate",
        route_after_sipdo_choice,
        {"sipdo_optimize": "sipdo_optimize", "extract": "extract"},
    )
    # sipdo_optimize → extract (always)
    builder.add_conditional_edges(
        "sipdo_optimize",
        route_after_sipdo_optimize,
        {"extract": "extract"},
    )
    builder.add_conditional_edges(
        "extract",
        route_after_extract,
        {"hitl_gate": "hitl_gate", "end": END},
    )
    builder.add_conditional_edges(
        "hitl_gate",
        route_after_hitl,
        {"reconcile": "reconcile", "re_extract_gate": "re_extract_gate"},
    )
    # re_extract_gate → sipdo_choice_gate (user picks new extraction strategy)
    builder.add_conditional_edges(
        "re_extract_gate",
        route_after_re_extract_gate,
        {"sipdo_choice_gate": "sipdo_choice_gate"},
    )
    builder.add_edge("reconcile", "generate")
    builder.add_edge("generate", "persist")
    # persist → sipdo_background (quick + unknown) | END
    builder.add_conditional_edges(
        "persist",
        route_after_persist,
        {"sipdo_background": "sipdo_background", "end": END},
    )
    builder.add_edge("sipdo_background", END)

    checkpointer = MemorySaver()
    compiled = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["sipdo_choice_gate", "hitl_gate"],
    )
    logger.info("LangGraph workflow compiled (11 nodes, SIPDO choice + HITL interrupts, re-extract loop)")
    return compiled, checkpointer


def get_graph():
    """Return a module-level singleton compiled graph."""
    global _graph, _checkpointer
    if _graph is None:
        _graph, _checkpointer = build_workflow()
    return _graph, _checkpointer
