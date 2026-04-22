"""Streamlit UI — thin frontend that calls the FastAPI backend.

Pages / sections:
  1. Upload — PDF + Excel pair upload + broker hint
  2. Review — show extracted trades table, approve/reject (HITL)
  3. Results — reconciliation summary + per-tab results, download button
  4. History — past reconciliation sessions with drill-down
  5. MS Data — MS receivables data stats + preview
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

# ── Config ───────────────────────────────────────────────────────────────────
try:
    import yaml
    _cfg_path = Path(__file__).parent.parent / "dev.yaml"
    with open(_cfg_path) as f:
        _cfg = yaml.safe_load(f)
    API_BASE = _cfg.get("ui", {}).get("api_base_url", "http://localhost:8001")
except Exception:
    API_BASE = "http://localhost:8001"

st.set_page_config(
    page_title="Brokerage Reconciliation",
    page_icon="📊",
    layout="wide",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stMetric { border: 1px solid #e0e0e0; border-radius: 8px; padding: 8px; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
    .match-badge { background-color: #d4edda; color: #155724; padding: 2px 8px;
                   border-radius: 4px; font-weight: bold; font-size: 0.8rem; }
    .mismatch-badge { background-color: #f8d7da; color: #721c24; padding: 2px 8px;
                      border-radius: 4px; font-weight: bold; font-size: 0.8rem; }
    .new-badge { background-color: #cce5ff; color: #004085; padding: 2px 8px;
                 border-radius: 4px; font-weight: bold; font-size: 0.8rem; }
    .missing-badge { background-color: #fff3cd; color: #856404; padding: 2px 8px;
                     border-radius: 4px; font-weight: bold; font-size: 0.8rem; }
    .step-done { color: #28a745; font-weight: 600; }
    .step-active { color: #fd7e14; font-weight: 600; }
    .step-pending { color: #6c757d; }
    .step-failed { color: #dc3545; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

# ── Session state defaults ───────────────────────────────────────────────────
_DEFAULTS = {
    "page": "Upload",
    "session_id": None,
    "pipeline_state": None,
    "pdf_path": None,
    "excel_path": None,
    "history_detail_id": None,
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Navigation ───────────────────────────────────────────────────────────────

# Pipeline steps in order, keyed by the status value that means this step is
# currently running.  Steps earlier than the current status are "done".
_PIPELINE_STEPS = [
    ("verifying",    "Verify Documents"),
    ("classifying",  "Classify Broker"),
    ("sipdo_choice", "SIPDO Choice"),
    ("optimizing",   "Optimize Prompt"),
    ("extracting",   "Extract Trades"),
    ("hitl_review",  "HITL Review"),
    ("reconciling",  "Reconcile vs MS"),
    ("generating",   "Generate Report"),
    ("persisting",   "Persist Results"),
    ("completed",    "Done"),
]


def _render_step_tracker(state: dict):
    """Render a step-by-step progress indicator in the sidebar."""
    status = state.get("status", "")
    is_unknown = state.get("is_unknown_broker", False)

    # Build ordered list; skip sipdo_choice/optimizing when broker is known
    steps = []
    for key, label in _PIPELINE_STEPS:
        if key in ("sipdo_choice", "optimizing") and not is_unknown:
            continue
        steps.append((key, label))

    # Find current step index
    current_idx = -1
    for i, (key, _) in enumerate(steps):
        if key == status:
            current_idx = i
            break

    # If status is "failed", mark the failed step
    failed = status == "failed"
    failed_step = state.get("current_step", "")

    st.markdown("**Pipeline Progress**")
    for i, (key, label) in enumerate(steps):
        if failed and key == failed_step:
            st.markdown(f'<span class="step-failed">✗ {label}</span>', unsafe_allow_html=True)
        elif failed and i < current_idx:
            st.markdown(f'<span class="step-done">✓ {label}</span>', unsafe_allow_html=True)
        elif i < current_idx:
            st.markdown(f'<span class="step-done">✓ {label}</span>', unsafe_allow_html=True)
        elif i == current_idx:
            if key == "completed":
                st.markdown(f'<span class="step-done">✓ {label}</span>', unsafe_allow_html=True)
            else:
                st.markdown(f'<span class="step-active">⏳ {label}</span>', unsafe_allow_html=True)
        else:
            st.markdown(f'<span class="step-pending">○ {label}</span>', unsafe_allow_html=True)

    # Show recent pipeline log entries below the tracker
    logs = state.get("logs", [])
    if logs:
        st.markdown("---")
        st.markdown("**Recent Activity**")
        for log in logs[-4:]:
            st.caption(log)


def nav():
    pages = ["Upload", "Review", "Results", "History", "MS Data", "Prompt Cache"]
    with st.sidebar:
        st.title("📊 Brokerage Recon")
        st.markdown("---")
        for p in pages:
            if st.button(p, key=f"nav_{p}", use_container_width=True):
                st.session_state.page = p

        if st.session_state.session_id:
            st.markdown("---")
            state = st.session_state.pipeline_state or {}
            st.caption(f"**Session:** `{st.session_state.session_id[:8]}…`")
            _render_step_tracker(state)


# ── API helpers ──────────────────────────────────────────────────────────────
def _post(path: str, timeout: int = 120, **kwargs) -> dict:
    try:
        r = httpx.post(f"{API_BASE}{path}", timeout=timeout, **kwargs)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text[:400]}")
        return {}
    except Exception as e:
        st.error(f"Request failed: {e}")
        return {}


def _get(path: str, timeout: int = 30) -> dict | list:
    try:
        r = httpx.get(f"{API_BASE}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Request failed: {e}")
        return {}


def _poll_state(session_id: str, target_statuses: set[str], max_wait: int = 5) -> dict | None:
    """Poll pipeline state until status is in target_statuses or max_wait seconds."""
    for _ in range(max_wait):
        state = _get(f"/api/pipeline/state/{session_id}")
        if state and state.get("status") in target_statuses:
            return state
        time.sleep(1)
    return state if state else None


# ── Page: Upload ─────────────────────────────────────────────────────────────
def page_upload():
    st.header("📤 Upload Broker Documents")
    st.markdown("Upload PDF broker invoices. Excel confirmation files are optional — the pipeline works with PDF only.")

    with st.form("upload_form"):
        col_a, col_b = st.columns(2)
        with col_a:
            flow_type = st.selectbox(
                "Flow Type",
                ["receivable", "payable"],
                format_func=lambda x: "Receivable (MS receives)" if x == "receivable" else "Payable (MS pays)",
                help="Receivable: MS receives brokerage from broker. Payable: MS pays brokerage to broker.",
            )
        with col_b:
            broker_hint = st.text_input(
                "Broker name (optional hint)",
                placeholder="e.g. BNP Paribas, JP Morgan, Marex…",
            )
        pdf_files = st.file_uploader("PDF Statement(s) *", type=["pdf"], accept_multiple_files=True)
        excel_files = st.file_uploader("Excel Confirmation(s) — optional", type=["xlsx", "xls", "csv"], accept_multiple_files=True)
        submitted = st.form_submit_button("Upload & Run Pipeline", type="primary")

    if submitted:
        if not pdf_files:
            st.error("Please upload at least one PDF file.")
            return

        # Upload files — send as multi-file
        with st.spinner("Uploading files…"):
            files_payload = []
            for pf in pdf_files:
                files_payload.append(("pdf_file", (pf.name, pf.getvalue(), "application/pdf")))
            for ef in (excel_files or []):
                files_payload.append(("excel_file", (ef.name, ef.getvalue(),
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")))

            resp = _post(
                "/api/upload",
                files=files_payload,
                data={"broker_hint": broker_hint, "flow_type": flow_type},
            )

        if not resp:
            return

        st.session_state.session_id = resp["session_id"]
        st.session_state.pdf_path = resp["pdf_path"]
        st.session_state.excel_path = resp.get("excel_path")
        st.success(f"Uploaded! Session `{resp['session_id'][:8]}…` ({resp.get('flow_type', 'receivable')})")

        n_excels = len(excel_files) if excel_files else 0
        if len(pdf_files) > 1 or n_excels > 1:
            st.info(f"📎 Multi-file session: {len(pdf_files)} PDFs, {n_excels} Excels")
        elif n_excels == 0:
            st.info("📄 PDF-only upload — Excel cross-check will be skipped")

        # Run Phase 1: verify → classify → extract → HITL
        progress = st.progress(0, text="Phase 1: Verifying documents…")
        start_payload = {
            "session_id": resp["session_id"],
            "flow_type": flow_type,
            "pdf_path": resp["pdf_path"],
            "pdf_paths": resp.get("pdf_paths", [resp["pdf_path"]]),
            "broker_hint": broker_hint,
        }
        if resp.get("excel_path"):
            start_payload["excel_path"] = resp["excel_path"]
            start_payload["excel_paths"] = resp.get("excel_paths", [resp["excel_path"]])
        state = _post("/api/pipeline/start", json=start_payload)
        progress.progress(100, text="Phase 1 complete!")

        if not state:
            return

        st.session_state.pipeline_state = state

        if state.get("error"):
            st.error(f"Pipeline error: {state['error']}")
            return

        # Pipeline may have paused at SIPDO choice (unknown broker) or HITL
        if state.get("sipdo_choice_pending"):
            st.warning(f"New broker format detected: **{state.get('broker_name', 'Unknown')}**. "
                       "Proceed to the **Review** tab to choose extraction strategy.")
        else:
            st.success(f"Extracted **{state.get('trade_count', 0)}** trades "
                       f"via `{state.get('extraction_method', 'unknown')}`")
            st.info("Proceed to the **Review** tab to inspect and approve/reject.")
        st.session_state.page = "Review"
        st.rerun()


# ── Page: Review (HITL) ──────────────────────────────────────────────────────
def page_review():
    st.header("🔍 Review Extracted Trades")
    state = st.session_state.pipeline_state

    if not state:
        st.info("No active pipeline. Upload files first.")
        return

    status = state.get("status", "")
    hitl_pending = state.get("hitl_pending", False)
    sipdo_choice_pending = state.get("sipdo_choice_pending", False)

    # ── SIPDO Choice Screen (unknown broker) ─────────────────────────────
    if sipdo_choice_pending or status == "sipdo_choice":
        _render_sipdo_choice(state)
        return

    # ── SIPDO Optimization Progress ──────────────────────────────────────
    if status == "optimizing":
        _render_sipdo_progress(state)
        return

    # If pipeline is still running, show progress with auto-refresh
    if status not in ("hitl_review", "completed", "failed") and not hitl_pending:
        st.info(f"Pipeline is running… (status: **{status}**)")
        # Show recent logs so user sees what's happening
        logs = state.get("logs", [])
        if logs:
            st.markdown("##### Live Progress")
            for log in logs[-5:]:
                st.caption(log)
        with st.spinner("Waiting for extraction to complete…"):
            refreshed = _poll_state(
                state["session_id"],
                {"hitl_review", "completed", "failed"},
            )
        if refreshed:
            st.session_state.pipeline_state = refreshed
            st.rerun()
        if st.button("🔄 Refresh"):
            refreshed = _get(f"/api/pipeline/state/{state['session_id']}")
            if refreshed:
                st.session_state.pipeline_state = refreshed
                st.rerun()
        return

    if status == "failed":
        st.error(f"Pipeline failed: {state.get('error')}")
        return

    # Summary bar
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Broker", state.get("broker_name") or "Unknown")
    col2.metric("Method", state.get("extraction_method") or "—")
    col3.metric("Confidence", f"{(state.get('extraction_confidence') or 0):.0%}")
    col4.metric("Trades", state.get("trade_count", 0))

    # Column mapping used (useful for reviewers to validate mapping quality)
    mapping = state.get("last_column_mapping")
    if mapping:
        with st.expander("📋 Column Mapping Used", expanded=False):
            mapping_df = pd.DataFrame(
                [{"Source Column": k, "Mapped To": v} for k, v in mapping.items()]
            )
            st.dataframe(mapping_df, use_container_width=True, hide_index=True)

    # Extraction warnings
    warnings = state.get("extraction_warnings", [])
    if warnings:
        with st.expander(f"⚠️ Extraction Warnings ({len(warnings)})", expanded=True):
            for w in warnings:
                st.warning(w)

    # Trades table
    trades_data = state.get("trades")
    if trades_data:
        st.markdown(f"#### Extracted Trades ({len(trades_data)} rows)")
        trades_df = pd.DataFrame(trades_data)
        display_cols = [c for c in [
            "trade_id", "trade_date", "instrument", "buy_sell", "quantity",
            "price", "brokerage_amount", "currency", "counterparty",
            "client_account", "exchange",
        ] if c in trades_df.columns]
        st.dataframe(
            trades_df[display_cols] if display_cols else trades_df,
            use_container_width=True,
            hide_index=True,
            height=min(400, 35 * len(trades_df) + 38),
        )

    # Pipeline logs
    with st.expander("📝 Pipeline Logs", expanded=False):
        for log in (state.get("logs") or []):
            st.caption(log)

    # HITL approval buttons
    if status == "hitl_review" or hitl_pending:
        st.markdown("---")
        st.markdown("### Approve or Reject")
        feedback = st.text_area("Feedback (optional)", placeholder="Add reviewer notes…")

        col_approve, col_reject, _ = st.columns([1, 1, 2])
        with col_approve:
            if st.button("✅ Approve & Reconcile", type="primary", use_container_width=True):
                progress = st.progress(0, text="Phase 2: Reconciling…")
                resumed = _post(
                    "/api/pipeline/resume",
                    json={
                        "session_id": state["session_id"],
                        "approved": True,
                        "feedback": feedback,
                    },
                )
                progress.progress(100, text="Pipeline complete!")
                if resumed:
                    st.session_state.pipeline_state = resumed
                    st.success("Pipeline complete! See the **Results** tab.")
                    st.session_state.page = "Results"
                    st.rerun()

        with col_reject:
            if st.button("❌ Reject & Re-extract", use_container_width=True):
                resumed = _post(
                    "/api/pipeline/resume",
                    json={"session_id": state["session_id"], "approved": False, "feedback": feedback},
                )
                if resumed:
                    st.session_state.pipeline_state = resumed
                    st.info("Extraction rejected. Choose a new extraction strategy below.")
                    st.rerun()
                else:
                    st.warning("Failed to resume pipeline. Try re-uploading.")
                    st.session_state.pipeline_state = None
                    st.session_state.page = "Upload"
                    st.rerun()


def _render_sipdo_progress(state: dict):
    """Show live SIPDO optimization progress by polling the side-channel."""
    session_id = state.get("session_id", "")
    broker = state.get("broker_name") or "Unknown"

    st.info(f"🎯 **SIPDO Prompt Optimization in progress for {broker}…**")
    st.markdown("Generating an optimized extraction prompt. This typically takes 2-5 minutes.")

    sipdo_stages = [
        ("Step 1", "Analyzing document structure"),
        ("Step 2", "Decomposing extraction fields"),
        ("Step 3", "Generating seed extraction prompt"),
        ("Step 4", "Optimization iterations"),
        ("Step 5", "Consistency audit"),
    ]

    # Container that we'll update inside the polling loop
    progress_bar = st.empty()
    stages_container = st.empty()
    iter_container = st.empty()
    status_text = st.empty()

    # Poll the side-channel endpoint until done
    max_polls = 600   # up to ~10 minutes (600 * 1s)
    for poll_idx in range(max_polls):
        progress = _get(f"/api/pipeline/sipdo-progress/{session_id}")
        if not progress:
            time.sleep(2)
            continue

        messages = progress.get("messages", [])
        done = progress.get("done", False)

        # Also check if the graph has already moved past SIPDO (e.g. SIPDO
        # failed/aborted and the pipeline continued to extraction/HITL)
        if poll_idx % 3 == 0:  # check every ~6 seconds
            graph_state = _get(f"/api/pipeline/state/{session_id}")
            if graph_state and graph_state.get("status") in ("hitl_review", "extracting", "completed", "failed"):
                real_status = graph_state.get("status")
                if "failed" in messages or any("aborting" in m.lower() for m in messages) or any("failed" in m.lower() for m in messages):
                    status_text.warning("⚠️ SIPDO optimization could not produce a good prompt — pipeline continued with direct AI extraction.")
                else:
                    status_text.success("✅ Optimization complete! Loading results…")
                time.sleep(1)
                st.session_state.pipeline_state = graph_state
                st.rerun()

        # Parse which stages are complete
        completed_steps = 0
        for key, _ in sipdo_stages:
            if any(key in m for m in messages):
                completed_steps += 1

        pct = min(int(completed_steps / len(sipdo_stages) * 100), 95) if not done else 100
        progress_bar.progress(pct, text=f"SIPDO: {completed_steps}/{len(sipdo_stages)} stages complete")

        # Build stage checklist
        stage_lines = []
        active_found = False
        for key, label in sipdo_stages:
            found = any(key in m for m in messages)
            if found:
                stage_lines.append(f"✅ {label}")
            elif not active_found and completed_steps > 0:
                stage_lines.append(f"⏳ {label}")
                active_found = True
            else:
                stage_lines.append(f"⬜ {label}")
        stages_container.markdown("##### Optimization Stages\n" + "\n\n".join(stage_lines))

        # Show iteration details
        iter_msgs = [m for m in messages if "iteration" in m.lower() or "accuracy" in m.lower()]
        if iter_msgs:
            detail = "##### Iteration Details\n" + "\n\n".join(f"- {m}" for m in iter_msgs[-8:])
            iter_container.markdown(detail)

        if done:
            # Check if SIPDO failed or produced low accuracy — inform user
            has_failure = any("failed" in m.lower() or "aborting" in m.lower() for m in messages)
            if has_failure:
                status_text.warning("⚠️ SIPDO optimization could not produce a good prompt — pipeline continued with direct AI extraction.")
            else:
                status_text.success("✅ Optimization complete! Loading results…")
            time.sleep(1)
            # Fetch the final graph state (node has returned, state is persisted)
            refreshed = _poll_state(session_id, {"hitl_review", "extracting", "completed", "failed"}, max_wait=15)
            if refreshed:
                st.session_state.pipeline_state = refreshed
                st.rerun()
            # Fallback — try once more
            refreshed = _get(f"/api/pipeline/state/{session_id}")
            if refreshed:
                st.session_state.pipeline_state = refreshed
                st.rerun()
            break

        time.sleep(2)

    # If we exhausted polling, offer manual refresh
    st.warning("Optimization is taking longer than expected.")
    if st.button("🔄 Refresh Status"):
        refreshed = _get(f"/api/pipeline/state/{session_id}")
        if refreshed:
            st.session_state.pipeline_state = refreshed
            st.rerun()


def _render_sipdo_choice(state: dict):
    """Show SIPDO strategy choice screen for unknown brokers."""
    broker = state.get("broker_name") or "Unknown"
    st.markdown("---")
    st.markdown(f"### 🆕 New Broker Format Detected: **{broker}**")
    st.markdown(
        "No template, cached mapping, or optimized prompt exists for this broker. "
        "Choose how to proceed:"
    )

    col_quick, col_optimize = st.columns(2)

    with col_quick:
        st.markdown("#### ⚡ Quick Extract")
        st.markdown(
            "- Generic AI extraction (~30s)\n"
            "- Good enough for review\n"
            "- Optimization runs **silently in background** after you approve\n"
            "- Next upload from this broker will use the optimized prompt"
        )
        if st.button("⚡ Quick Extract", type="secondary", use_container_width=True, key="sipdo_quick"):
            with st.spinner("Running quick extraction…"):
                result = _post(
                    "/api/pipeline/sipdo-choice",
                    json={"session_id": state["session_id"], "strategy": "quick"},
                )
            if result:
                st.session_state.pipeline_state = result
                st.rerun()

    with col_optimize:
        st.markdown("#### 🎯 Optimize First")
        st.markdown(
            "- Generates a broker-specific extraction prompt (~2-5 min)\n"
            "- Higher accuracy extraction\n"
            "- You'll see **live progress** (iteration-by-iteration)\n"
            "- Future uploads from this broker will be instant"
        )
        if st.button("🎯 Optimize First", type="primary", use_container_width=True, key="sipdo_optimize"):
            result = _post(
                "/api/pipeline/sipdo-choice",
                json={"session_id": state["session_id"], "strategy": "optimize"},
                timeout=30,
            )
            if result:
                st.session_state.pipeline_state = result
                st.rerun()

    # Show pipeline logs so far
    with st.expander("📝 Pipeline Logs", expanded=False):
        for log in (state.get("logs") or []):
            st.caption(log)


# ── Page: Results ────────────────────────────────────────────────────────────
def page_results():
    st.header("📊 Reconciliation Results")
    state = st.session_state.pipeline_state

    if not state or state.get("status") != "completed":
        st.info("No completed pipeline. Run the full pipeline first.")
        return

    summary = state.get("recon_summary") or {}

    # ── KPI Row ──────────────────────────────────────────────────────────
    cols = st.columns(6)
    kpis = [
        ("Broker Trades", summary.get("broker_trade_count", 0)),
        ("MS Trades", summary.get("ms_trade_count", 0)),
        ("Matched ✅", summary.get("matched_count", 0)),
        ("Mismatched ⚠️", summary.get("mismatched_count", 0)),
        ("New 🆕", summary.get("new_trades_count", 0)),
        ("Missing ❓", summary.get("missing_trades_count", 0)),
    ]
    for col, (label, val) in zip(cols, kpis):
        col.metric(label, val)

    # Brokerage summary
    st.markdown(
        f"**Match rate:** {summary.get('match_rate', 'N/A')}  |  "
        f"**Broker brokerage:** {summary.get('broker_total_brokerage', 0):,.2f}  |  "
        f"**MS brokerage:** {summary.get('ms_total_brokerage', 0):,.2f}  |  "
        f"**Difference:** {summary.get('difference', 0):,.2f}"
    )

    # ── Download button ──────────────────────────────────────────────────
    output_fn = state.get("output_filename")
    if output_fn:
        try:
            r = httpx.get(f"{API_BASE}/api/download/{output_fn}", timeout=30)
            r.raise_for_status()
            st.download_button(
                label="⬇️ Download Excel Report",
                data=r.content,
                file_name=output_fn,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
            )
        except Exception as e:
            st.error(f"Download failed: {e}")

    st.markdown("---")

    # ── Tabbed reconciliation detail ─────────────────────────────────────
    tab_matched, tab_mismatched, tab_new, tab_missing, tab_all_trades = st.tabs([
        f"Matched ({summary.get('matched_count', 0)})",
        f"Mismatched ({summary.get('mismatched_count', 0)})",
        f"New / Broker Only ({summary.get('new_trades_count', 0)})",
        f"Missing / MS Only ({summary.get('missing_trades_count', 0)})",
        "All Extracted Trades",
    ])

    with tab_matched:
        _render_match_table(state.get("recon_matched", []), "MATCH")

    with tab_mismatched:
        _render_match_table(state.get("recon_mismatched", []), "MISMATCH")

    with tab_new:
        _render_exception_table(state.get("recon_new", []), "NEW")

    with tab_missing:
        _render_exception_table(state.get("recon_missing", []), "MISSING")

    with tab_all_trades:
        trades = state.get("trades", [])
        if trades:
            df = pd.DataFrame(trades)
            display_cols = [c for c in [
                "trade_id", "trade_date", "instrument", "buy_sell", "quantity",
                "price", "brokerage_amount", "currency", "counterparty", "client_account",
            ] if c in df.columns]
            st.dataframe(df[display_cols] if display_cols else df,
                         use_container_width=True, hide_index=True)
        else:
            st.info("No trade data in state.")


def _render_match_table(matches: list[dict], category: str):
    """Render a matched/mismatched reconciliation table with broker vs MS comparison."""
    if not matches:
        st.info(f"No {category.lower()} trades.")
        return

    rows = []
    for m in matches:
        bt = m.get("broker_trade", {})
        mt = m.get("ms_trade", {})
        row = {
            "Trade ID": bt.get("trade_id") or mt.get("trade_id") or "—",
            "Trade Date": bt.get("trade_date") or "—",
            "Instrument": bt.get("instrument") or "—",
            "Buy/Sell": bt.get("buy_sell") or "—",
            "Broker Qty": bt.get("quantity"),
            "MS Qty": mt.get("quantity"),
            "Broker Price": bt.get("price"),
            "MS Price": mt.get("price"),
            "Broker Brokerage": bt.get("brokerage_amount"),
            "MS Brokerage": mt.get("brokerage_amount"),
            "Confidence": m.get("confidence_score", 0),
        }
        if category == "MISMATCH":
            row["Reason"] = m.get("mismatch_reason") or "—"
            diffs = m.get("differences", {})
            row["Differences"] = ", ".join(
                f"{k}: broker={v.get('broker')} vs ms={v.get('ms')}"
                for k, v in diffs.items()
            ) if diffs else "—"
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True,
                 height=min(500, 35 * len(df) + 38))


def _render_exception_table(matches: list[dict], category: str):
    """Render new (broker-only) or missing (MS-only) trades."""
    if not matches:
        st.info(f"No {category.lower()} trades.")
        return

    rows = []
    for m in matches:
        if category == "NEW":
            t = m.get("broker_trade", {})
            source = "Broker"
        else:
            t = m.get("ms_trade", {})
            source = "MS"
        rows.append({
            "Source": source,
            "Trade ID": t.get("trade_id") or "—",
            "Trade Date": t.get("trade_date") or "—",
            "Instrument": t.get("instrument") or "—",
            "Buy/Sell": t.get("buy_sell") or "—",
            "Quantity": t.get("quantity"),
            "Price": t.get("price"),
            "Brokerage": t.get("brokerage_amount"),
            "Currency": t.get("currency") or "—",
            "Account": t.get("client_account") or "—",
            "Reason": m.get("mismatch_reason") or "—",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True,
                 height=min(500, 35 * len(df) + 38))


# ── Page: History ────────────────────────────────────────────────────────────
def page_history():
    st.header("📋 Reconciliation History")

    data = _get("/api/status/sessions")
    if not data or not isinstance(data, list) or not data:
        st.info("No reconciliation sessions found.")
        return

    # Session list as a selectable table
    sessions_df = pd.DataFrame(data)
    display_cols = [c for c in [
        "id", "broker_name", "status", "total_trades",
        "matched_count", "mismatched_count", "new_trades_count",
        "missing_trades_count", "created_at",
    ] if c in sessions_df.columns]
    sessions_df = sessions_df[display_cols] if display_cols else sessions_df

    # Rename for display
    rename_map = {
        "id": "Session ID", "broker_name": "Broker", "status": "Status",
        "total_trades": "Trades", "matched_count": "Matched",
        "mismatched_count": "Mismatched", "new_trades_count": "New",
        "missing_trades_count": "Missing", "created_at": "Created",
    }
    sessions_df = sessions_df.rename(columns=rename_map)

    st.dataframe(sessions_df, use_container_width=True, hide_index=True)

    # Drill-down selector
    st.markdown("---")
    session_ids = [s.get("id", "") for s in data]
    labels = [
        f"{s.get('id', '?')[:8]}… — {s.get('broker_name') or 'Unknown'} — {s.get('status')}"
        for s in data
    ]
    selected_idx = st.selectbox("Select session to inspect", range(len(labels)),
                                format_func=lambda i: labels[i])

    if st.button("Load Session Details", type="primary"):
        sid = session_ids[selected_idx]
        _render_history_detail(sid)


def _render_history_detail(session_id: str):
    """Fetch and display detail for a historical session."""
    detail = _get(f"/api/status/sessions/{session_id}/results")
    if not detail:
        st.error("Could not load session results.")
        return

    st.markdown(f"### Session: `{session_id[:12]}…`")

    # KPIs
    cols = st.columns(5)
    cols[0].metric("Broker", detail.get("broker_name") or "Unknown")
    cols[1].metric("Matched", detail.get("matched_count", 0))
    cols[2].metric("Mismatched", detail.get("mismatched_count", 0))
    cols[3].metric("New", detail.get("new_trades_count", 0))
    cols[4].metric("Missing", detail.get("missing_trades_count", 0))

    # Trades
    trades = detail.get("extracted_trades", [])
    results = detail.get("reconciliation_results", [])

    if trades:
        with st.expander(f"📊 Extracted Trades ({len(trades)})", expanded=False):
            st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)

    if results:
        with st.expander(f"📋 Reconciliation Results ({len(results)})", expanded=True):
            results_df = pd.DataFrame(results)
            # Inline MS snapshot data for display
            if "ms_trade_snapshot" in results_df.columns:
                for field in ["trade_id", "instrument", "quantity", "price"]:
                    results_df[f"ms_{field}"] = results_df["ms_trade_snapshot"].apply(
                        lambda x: x.get(field) if isinstance(x, dict) else None
                    )
            display_cols = [c for c in [
                "status", "mismatch_reason", "confidence_score",
                "ms_trade_id", "ms_instrument", "ms_quantity", "ms_price",
                "differences",
            ] if c in results_df.columns]
            st.dataframe(
                results_df[display_cols] if display_cols else results_df,
                use_container_width=True, hide_index=True,
            )

    # Download link
    output_file = detail.get("output_file")
    if output_file:
        try:
            r = httpx.get(f"{API_BASE}/api/download/{output_file}", timeout=30)
            r.raise_for_status()
            st.download_button(
                label="⬇️ Download Report",
                data=r.content,
                file_name=output_file,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            st.caption(f"Report file: {output_file}")


# ── Page: MS Data ────────────────────────────────────────────────────────────
def page_ms_data():
    st.header("📂 MS Internal Data")

    # Flow type selector
    flow_label = st.radio(
        "Flow Type",
        ["Receivable", "Payable"],
        horizontal=True,
        key="ms_data_flow_type",
    )
    flow_type = flow_label.lower()

    # Stats
    stats = _get(f"/api/status/ms-data?flow_type={flow_type}")
    if stats:
        cols = st.columns(4)
        cols[0].metric("Flow Type", flow_type.capitalize())
        cols[1].metric("Total Rows", stats.get("total_rows", 0))
        cols[2].metric("Trade ID Index", stats.get("trade_id_count", 0))
        cols[3].metric("Composite Index", stats.get("composite_count", 0))

        columns = stats.get("columns", [])
        if columns:
            st.markdown("**Columns detected:**")
            st.code(", ".join(columns))

    st.markdown("---")

    # Preview table
    st.subheader("Data Preview")
    limit = st.slider("Rows to show", min_value=10, max_value=200, value=50, step=10)
    preview = _get(f"/api/status/ms-data/preview?limit={limit}&flow_type={flow_type}")
    if preview and preview.get("rows"):
        df = pd.DataFrame(preview["rows"])
        st.dataframe(df, use_container_width=True, hide_index=True,
                     height=min(600, 35 * len(df) + 38))
        st.caption(f"Showing {len(df)} of {preview.get('total', '?')} {flow_type} rows")
    elif preview:
        st.info(f"No MS {flow_type} data loaded. Check the config file path.")


# ── Page: Prompt Cache ───────────────────────────────────────────────────────
def page_prompt_cache():
    st.header("🧠 SIPDO Prompt Cache")
    st.markdown("Cached SIPDO-optimized extraction prompts per broker.")

    data = _get("/api/status/sipdo/prompts")
    if not data or not isinstance(data, list) or not data:
        st.info("No optimized prompts cached yet. "
                "Upload a new broker and choose **Optimize First** to generate one.")
        return

    df = pd.DataFrame(data)
    rename_map = {
        "broker_name": "Broker",
        "accuracy_score": "Accuracy",
        "source_session_id": "Source Session",
        "created_at": "Created",
        "updated_at": "Updated",
    }
    display_cols = [c for c in rename_map.keys() if c in df.columns]
    display_df = df[display_cols].rename(columns=rename_map)

    if "Accuracy" in display_df.columns:
        display_df["Accuracy"] = display_df["Accuracy"].apply(lambda x: f"{x:.0%}" if x else "—")

    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.caption(f"{len(data)} optimized prompt(s) cached")


# ── Router ───────────────────────────────────────────────────────────────────
nav()
page = st.session_state.page
if page == "Upload":
    page_upload()
elif page == "Review":
    page_review()
elif page == "Results":
    page_results()
elif page == "History":
    page_history()
elif page == "MS Data":
    page_ms_data()
elif page == "Prompt Cache":
    page_prompt_cache()
