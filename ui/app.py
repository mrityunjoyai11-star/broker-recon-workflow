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
from datetime import datetime
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
    "page": "Batch",
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

    # Show lifecycle timeline below the tracker
    logs = state.get("logs", [])
    if logs:
        st.markdown("---")
        st.markdown("**📋 Session Activity**")
        _render_lifecycle_timeline(logs)


def _render_lifecycle_timeline(logs: list[str]):
    """Parse log entries and display with contextual icons."""
    _ICON_MAP = [
        ("upload",      "📤"),
        ("verif",       "✅"),
        ("classif",     "🏷️"),
        ("extract",     "📊"),
        ("cached",      "⚡"),
        ("sipdo",       "🎯"),
        ("optimi",      "🎯"),
        ("reconcil",    "🔍"),
        ("match",       "✅"),
        ("affirm",      "⏳"),
        ("gate",        "🚪"),
        ("approv",      "✅"),
        ("reject",      "❌"),
        ("break",       "⚠️"),
        ("resolv",      "🔧"),
        ("evidence",    "📋"),
        ("escalat",     "⬆️"),
        ("generat",     "📝"),
        ("persist",     "💾"),
        ("complet",     "🏁"),
        ("fail",        "❌"),
        ("error",       "❌"),
        ("case",        "📋"),
    ]
    for log in logs[-15:]:
        icon = "▸"
        log_lower = log.lower()
        for keyword, emoji in _ICON_MAP:
            if keyword in log_lower:
                icon = emoji
                break
        # Try to extract timestamp prefix (e.g. "13:42:05" or "[13:42:05]")
        display = log.strip()
        st.caption(f"{icon} {display}")


def _render_session_header():
    """Show a persistent session context bar when a session is active."""
    state = st.session_state.pipeline_state
    if not state or not st.session_state.session_id:
        return
    sid = st.session_state.session_id[:8]
    broker = state.get("broker_name") or "Unknown"
    flow = (state.get("flow_type") or "receivable").capitalize()
    status = (state.get("status") or "—").replace("_", " ").title()
    st.markdown(
        f'<div style="background:#f0f2f6;padding:6px 14px;border-radius:6px;margin-bottom:10px;'
        f'font-size:0.85rem;color:#333;">'
        f'<b>Session:</b> {sid} &nbsp;|&nbsp; <b>Broker:</b> {broker} &nbsp;|&nbsp; '
        f'<b>Flow:</b> {flow} &nbsp;|&nbsp; <b>Status:</b> {status}</div>',
        unsafe_allow_html=True,
    )


def nav():
    pages = ["Batch", "Upload", "Review", "Results", "History", "MS Data", "Prompt Cache"]
    with st.sidebar:
        st.title("📊 Brokerage Recon")
        st.markdown("---")
        for p in pages:
            if st.button(p, key=f"nav_{p}", width="stretch"):
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


# ── Page: Batch Processing ───────────────────────────────────────────────────
_BATCH_STATUS_ICONS = {
    "starting":    "⏳",
    "verifying":   "🔍",
    "classifying": "🏷️",
    "sipdo_choice": "🔀",
    "optimizing":  "🎯",
    "extracting":  "📊",
    "hitl_review": "⏸️",
    "reconciling": "🔄",
    "generating":  "📝",
    "persisting":  "💾",
    "completed":   "✅",
    "processed":   "✅",
    "error":       "❌",
    "failed":      "❌",
}

# Pipeline node sequence for the batch flow (no manual gates)
_BATCH_NODES = [
    ("verify",         "🔍 Verify Documents"),
    ("classify",       "🏷️ Classify Broker"),
    ("sipdo_optimize", "🎯 SIPDO Optimize Prompt"),
    ("extract",        "📊 Extract Trades"),
    ("reconcile",      "🔄 Reconcile vs MS Data"),
    ("generate",       "📝 Generate Report"),
    ("persist",        "💾 Persist Results"),
]


_NODE_KEYWORDS = [
    ("verify",         ["verify", "verification"]),
    ("classify",       ["classif", "broker name", "template"]),
    ("sipdo_optimize", ["sipdo", "optimization", "iteration", "accuracy"]),
    ("extract",        ["extract", "tier ", "page ", "concurrent pdf"]),
    ("reconcile",      ["reconcil", "matched=", "mismatched=", "ghost"]),
    ("generate",       ["generated report", "template_agent"]),
    ("persist",        ["persist", "saved output"]),
]


def _classify_log_line(line: str) -> str:
    """Return the node key a log line most likely belongs to."""
    lower = line.lower()
    for key, kws in _NODE_KEYWORDS:
        if any(kw in lower for kw in kws):
            return key
    return ""


def _render_batch_job_activity(job: dict):
    """Render live activity for a single batch job: pipeline tracker + logs."""
    sid = job.get("session_id", "")
    status = job.get("status", "")
    is_running = status not in ("processed", "error", "completed", "failed")

    # Quick KPI strip
    kcols = st.columns(5)
    kcols[0].metric("Status", status or "—")
    conf = job.get("confidence")
    kcols[1].metric("Confidence", f"{conf:.0%}" if isinstance(conf, (int, float)) else "—")
    sipdo = job.get("sipdo_accuracy")
    kcols[2].metric("SIPDO Acc", f"{sipdo:.0%}" if isinstance(sipdo, (int, float)) else "—")
    kcols[3].metric("Trades", job.get("trade_count", "—"))
    kcols[4].metric("Flow", job.get("flow_type", "—"))

    # Pipeline node tracker
    state = _get(f"/api/pipeline/state/{sid}") if sid else {}
    current_step = (state or {}).get("current_step", "")
    pipeline_status = (state or {}).get("status", status)
    logs = (state or {}).get("logs", [])

    # Determine furthest-progressed node from current_step + status
    completed_idx = -1
    for i, (key, _label) in enumerate(_BATCH_NODES):
        if key == current_step:
            completed_idx = i
            break
    # If status is "completed" / "processed", everything is done
    if pipeline_status in ("completed", "processed"):
        completed_idx = len(_BATCH_NODES) - 1
        active_idx = -1
    elif pipeline_status in ("error", "failed"):
        active_idx = completed_idx  # stalled here
    else:
        active_idx = completed_idx
        completed_idx = active_idx - 1 if active_idx > 0 else -1

    # Render the tracker as compact horizontal pills
    tracker_lines = []
    for i, (_key, label) in enumerate(_BATCH_NODES):
        if i <= completed_idx:
            tracker_lines.append(f'<span class="step-done">✓ {label}</span>')
        elif i == active_idx and is_running:
            tracker_lines.append(f'<span class="step-active">⏳ {label}</span>')
        elif i == active_idx and pipeline_status in ("error", "failed"):
            tracker_lines.append(f'<span class="step-failed">✗ {label}</span>')
        else:
            tracker_lines.append(f'<span class="step-pending">○ {label}</span>')
    st.markdown(" &nbsp;·&nbsp; ".join(tracker_lines), unsafe_allow_html=True)

    # Group logs by inferred node
    if logs:
        st.markdown("**📜 Live Pipeline Logs**")
        # Group last 30 logs by node
        recent = logs[-30:]
        groups: dict[str, list[str]] = {key: [] for key, _ in _BATCH_NODES}
        groups["other"] = []
        for line in recent:
            key = _classify_log_line(line)
            if key in groups:
                groups[key].append(line)
            else:
                groups["other"].append(line)

        # Render in pipeline order, one collapsible per node that has logs
        active_key = _BATCH_NODES[active_idx][0] if 0 <= active_idx < len(_BATCH_NODES) else None
        done_keys = {_BATCH_NODES[i][0] for i in range(min(completed_idx + 1, len(_BATCH_NODES)))}
        for key, label in _BATCH_NODES:
            entries = groups.get(key, [])
            if not entries:
                continue
            if key in done_keys:
                done_marker = "✅"
            elif key == active_key and is_running:
                done_marker = "⏳"
            else:
                done_marker = "•"
            with st.expander(f"{done_marker} {label} — {len(entries)} log line(s)",
                             expanded=(key == active_key)):
                for line in entries:
                    st.caption(line)

        if groups["other"]:
            with st.expander(f"📋 Other ({len(groups['other'])} line(s))", expanded=False):
                for line in groups["other"]:
                    st.caption(line)
    else:
        st.caption("_(no logs yet)_")

    # Result links
    if job.get("moved_to"):
        st.caption(f"📂 File moved to: `{job['moved_to']}`")
    if job.get("error"):
        st.error(f"❌ {job['error']}")

    # ── Download + email draft (only after completion) ───────────────────
    if status in ("processed", "completed"):
        out_fn = job.get("output_file")
        col_dl, col_email, _ = st.columns([1, 1, 2])
        with col_dl:
            if out_fn:
                try:
                    r = httpx.get(f"{API_BASE}/api/download/{out_fn}", timeout=20)
                    r.raise_for_status()
                    st.download_button(
                        label="📥 Download Report",
                        data=r.content,
                        file_name=out_fn,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_{sid}",
                        width="stretch",
                    )
                except Exception:
                    st.caption(f"📎 Report: `{out_fn}` (download unavailable)")
            else:
                st.caption("_(no report generated)_")
        with col_email:
            draft = _get(f"/api/pipeline/email-draft/{sid}") if sid else {}
            if draft and draft.get("found"):
                st.caption(f"📧 Draft saved: `{draft.get('draft_file', '')}`")
            else:
                st.caption("_(no email draft saved)_")

        # Inline email draft preview (collapsed)
        if draft and draft.get("found"):
            with st.expander("📧 View Email Draft", expanded=False):
                st.markdown(f"**Subject:** {draft.get('subject', '')}")
                if draft.get("attachment"):
                    st.caption(f"📎 Attachment: **{draft['attachment']}**")
                st.text_area(
                    "Body",
                    value=draft.get("body", ""),
                    height=300,
                    key=f"batch_email_body_{sid}",
                    disabled=True,
                )
                bcols = st.columns([1, 1, 2])
                with bcols[0]:
                    if st.button("✏️ Edit in History", key=f"goto_history_{sid}"):
                        st.session_state.history_detail_id = sid
                        st.session_state.page = "History"
                        st.rerun()
                with bcols[1]:
                    st.button("📧 Send Email", disabled=True, key=f"send_email_batch_{sid}",
                              help="Email dispatch will be enabled in a future release.")


def page_batch():
    st.header("📦 Batch Processing")
    st.markdown(
        "Drop PDF files into the watch folders. The pipeline runs automatically — "
        "no human gates. Files are moved to **Processed/** on success or **Error/** on failure."
    )

    # Watcher status
    bstatus = _get("/api/batch/status") or {}
    cols = st.columns(4)
    cols[0].metric("Watcher", "🟢 Running" if bstatus.get("running") else "🔴 Stopped")
    cols[1].metric("Concurrency", bstatus.get("concurrency", "—"))
    cols[2].metric("Min Confidence", f"{bstatus.get('min_confidence', 0):.0%}")
    cols[3].metric("Poll Interval", f"{bstatus.get('poll_interval_sec', '—')}s")
    nas_root = bstatus.get("nas_root", "")
    if nas_root:
        st.caption(f"📂 Watch root: `{nas_root}`")

    # Drop zones
    st.markdown("---")
    st.markdown("### ⬇️ Drop Files")
    col_pay, col_recv = st.columns(2)
    for col, flow_type, label in [
        (col_pay, "payable", "💰 Payables"),
        (col_recv, "receivable", "💵 Receivables"),
    ]:
        with col:
            st.markdown(f"#### {label}")
            uploaded = st.file_uploader(
                f"Drop PDF(s) — {flow_type}",
                type=["pdf"],
                accept_multiple_files=True,
                key=f"batch_upload_{flow_type}",
            )
            if uploaded:
                if st.button(f"📤 Queue {len(uploaded)} file(s)", key=f"queue_{flow_type}", type="primary"):
                    queued = 0
                    for f in uploaded:
                        try:
                            files = {"pdf_file": (f.name, f.getvalue(), "application/pdf")}
                            data = {"flow_type": flow_type}
                            r = httpx.post(f"{API_BASE}/api/batch/upload", files=files, data=data, timeout=30)
                            r.raise_for_status()
                            queued += 1
                        except Exception as exc:
                            st.error(f"Failed to queue {f.name}: {exc}")
                    if queued:
                        st.success(f"✅ Queued {queued} file(s) in {flow_type}/. Watcher will pick up within ~{bstatus.get('poll_interval_sec', 5)}s.")
                        time.sleep(1)
                        st.rerun()

    # Active/recent jobs
    st.markdown("---")
    st.markdown("### 🔧 Active & Recent Jobs")
    jobs = _get("/api/batch/jobs") or []
    if not jobs:
        st.info("No batch jobs yet. Drop a file above to start.")
    else:
        rows = []
        running_count = 0
        for j in jobs:
            status = j.get("status", "")
            icon = _BATCH_STATUS_ICONS.get(status, "•")
            if status not in ("processed", "error", "completed", "failed"):
                running_count += 1
            conf = j.get("confidence")
            sipdo = j.get("sipdo_accuracy")
            tcount = j.get("trade_count")
            rows.append({
                "Status": f"{icon} {status}",
                "Started": j.get("started_at", "")[-8:] if j.get("started_at") else "",
                "File": str(j.get("file", "")),
                "Flow": str(j.get("flow_type", "")),
                "Broker": str(j.get("broker_name") or "—"),
                "Confidence": f"{conf:.0%}" if isinstance(conf, (int, float)) else "—",
                "SIPDO Acc": f"{sipdo:.0%}" if isinstance(sipdo, (int, float)) else "—",
                "Trades": str(tcount) if isinstance(tcount, (int, float)) else "—",
                "Result": str(j.get("error") or j.get("moved_to") or ""),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     height=min(400, 35 * len(rows) + 38))

        if running_count > 0:
            st.caption(f"⏳ {running_count} job(s) running — auto-refreshing every 3s…")

        # ── Per-job live activity ───────────────────────────────────────
        st.markdown("---")
        st.markdown("### 🛰️ Live Activity")
        st.caption("Click a job below to see real-time agent/node activity, SIPDO iterations, and per-page extraction progress.")
        # Show running jobs expanded; finished jobs collapsed
        for j in jobs[:8]:
            sid = j.get("session_id", "")
            status = j.get("status", "")
            icon = _BATCH_STATUS_ICONS.get(status, "•")
            broker = j.get("broker_name") or "Unknown"
            file_name = j.get("file", "")
            is_running = status not in ("processed", "error", "completed", "failed")
            label = f"{icon} `{sid[:8]}` — {file_name} → {broker} ({status})"
            with st.expander(label, expanded=is_running):
                _render_batch_job_activity(j)

    # Folder contents
    st.markdown("---")
    st.markdown("### 📁 Folder Contents")
    folders = _get("/api/batch/folders") or {}
    for flow_type, dirs in folders.items():
        with st.expander(f"📂 {flow_type}", expanded=False):
            sub_cols = st.columns(3)
            labels = [("watch", "📥 Inbox"), ("processed", "✅ Processed"), ("error", "❌ Error")]
            for col, (key, lbl) in zip(sub_cols, labels):
                files = dirs.get(key, [])
                with col:
                    st.markdown(f"**{lbl} ({len(files)})**")
                    if files:
                        from datetime import datetime as _dt
                        for f in files[:15]:
                            mtime = _dt.fromtimestamp(f["mtime"]).strftime("%H:%M:%S")
                            kb = f["size"] // 1024
                            st.caption(f"`{mtime}` {f['name'][:40]} ({kb} KB)")
                    else:
                        st.caption("_(empty)_")

    # Auto-refresh while jobs are running
    if jobs:
        running = sum(1 for j in jobs if j.get("status") not in ("processed", "error", "completed", "failed"))
        if running > 0:
            time.sleep(3)
            st.rerun()


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
            # Show cache hit notification if applicable
            method = state.get("extraction_method", "") or ""
            broker = state.get("broker_name") or "Unknown"
            if "cached" in method.lower() or "sipdo_cached" in method.lower():
                st.success(f"✅ Used cached extraction template for **{broker}** "
                           f"— extracted **{state.get('trade_count', 0)}** trades")
            elif not state.get("is_unknown_broker", True):
                st.info(f"ℹ️ Known broker format detected — extracted "
                        f"**{state.get('trade_count', 0)}** trades directly")
            else:
                st.success(f"Extracted **{state.get('trade_count', 0)}** trades "
                           f"via `{method}`")
            st.info("Proceed to the **Review** tab to inspect and approve/reject.")
        st.session_state.page = "Review"
        st.rerun()


# ── Page: Review (HITL) ──────────────────────────────────────────────────────
def page_review():
    st.header("🔍 Review Extracted Trades")
    _render_session_header()
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
    _gate_statuses = {"hitl_review", "completed", "failed"}
    if status not in _gate_statuses and not hitl_pending:
        page_count = state.get("page_count")
        if status == "extracting" and page_count:
            st.info(f"⚡ Extracting trades from **{page_count}** pages…")
        else:
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
                max_wait=300,  # up to 5 minutes for large PDFs
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
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Broker", state.get("broker_name") or "Unknown")
    col2.metric("Method", state.get("extraction_method") or "—")
    col3.metric("Confidence", f"{(state.get('extraction_confidence') or 0):.0%}")
    sipdo_acc = state.get("sipdo_accuracy_score")
    col4.metric("SIPDO Accuracy", f"{sipdo_acc:.0%}" if sipdo_acc else "—")
    col5.metric("Trades", state.get("trade_count", 0))

    # Column mapping used (useful for reviewers to validate mapping quality)
    mapping = state.get("last_column_mapping")
    if mapping:
        with st.expander("📋 Column Mapping Used", expanded=False):
            mapping_df = pd.DataFrame(
                [{"Source Column": k, "Mapped To": v} for k, v in mapping.items()]
            )
            st.dataframe(mapping_df, width="stretch", hide_index=True)

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
            width="stretch",
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
            if st.button("✅ Approve & Reconcile", type="primary", width="stretch"):
                progress = st.progress(0, text="Phase 2: Reconciling…")
                resumed = _post(
                    "/api/pipeline/resume",
                    json={
                        "session_id": state["session_id"],
                        "approved": True,
                        "feedback": feedback,
                    },
                )
                progress.progress(100, text="Phase 2 done!")
                if resumed:
                    st.session_state.pipeline_state = resumed
                    next_status = resumed.get("status", "")
                    if next_status == "completed":
                        st.success("Pipeline complete! See the **Results** tab.")
                        st.session_state.page = "Results"
                        st.rerun()
                    else:
                        st.info(f"Pipeline status: **{next_status}**")
                        st.rerun()

        with col_reject:
            if st.button("❌ Reject & Re-extract", width="stretch"):
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
            if graph_state and graph_state.get("status") == "extracting":
                # Show extraction progress instead of rerunning
                trade_count = graph_state.get("trade_count", 0)
                # Parse page count from logs
                gs_logs = graph_state.get("logs", [])
                page_info = ""
                for log in reversed(gs_logs):
                    if "pages" in log.lower() and ("concurrent" in log.lower() or "extraction" in log.lower()):
                        page_info = log
                        break
                progress_bar.progress(98, text="SIPDO: 5/5 stages complete")
                stages_container.markdown("##### Optimization Stages\n" + "\n\n".join(f"✅ {l}" for _, l in sipdo_stages))
                if page_info:
                    status_text.info(f"⚡ SIPDO complete! Now extracting trades… {page_info.strip()}")
                elif trade_count:
                    status_text.info(f"⚡ SIPDO complete! Extracting trades… ({trade_count} found so far)")
                else:
                    status_text.info("⚡ SIPDO complete! Extracting trades from PDF pages…")
                # Show live extraction log lines
                extract_logs = [l for l in gs_logs if "page" in l.lower() or "trade" in l.lower() or "tier" in l.lower()]
                if extract_logs:
                    iter_container.markdown("##### Extraction Progress\n" + "\n\n".join(f"- {m}" for m in extract_logs[-8:]))
                time.sleep(3)
                continue  # keep polling until hitl_review/completed/failed
            elif graph_state and graph_state.get("status") in ("hitl_review", "completed", "failed"):
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
        if st.button("⚡ Quick Extract", type="secondary", width="stretch", key="sipdo_quick"):
            with st.spinner("Starting extraction…"):
                result = _post(
                    "/api/pipeline/sipdo-choice",
                    json={"session_id": state["session_id"], "strategy": "quick"},
                    timeout=15,
                )
            if result:
                st.session_state.pipeline_state = result
                st.rerun()
            else:
                # Even if the POST timed out, the backend may still be running.
                # Poll to check actual status.
                refreshed = _get(f"/api/pipeline/state/{state['session_id']}")
                if refreshed:
                    st.session_state.pipeline_state = refreshed
                    st.rerun()

    with col_optimize:
        st.markdown("#### 🎯 Optimize First")
        st.markdown(
            "- Generates a broker-specific extraction prompt (~2-5 min)\n"
            "- Higher accuracy extraction\n"
            "- You'll see **live progress** (iteration-by-iteration)\n"
            "- Future uploads from this broker will be instant"
        )
        if st.button("🎯 Optimize First", type="primary", width="stretch", key="sipdo_optimize"):
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


# ── Gate 2: Affirmation Screen ───────────────────────────────────────────────

def _render_affirmation_gate(state: dict):
    """Render reconciliation results for ops affirmation."""
    st.header("✅ Reconciliation Affirmation (Gate 2)")
    st.markdown("Review the reconciliation results below. For each category, choose an action.")

    cases = state.get("cases", [])
    if not cases:
        st.warning("No cases found.")
        return

    matched = [c for c in cases if c["case_type"] == "matched"]
    breaks = [c for c in cases if c["case_type"] == "break"]
    ghosts = [c for c in cases if c["case_type"] == "ghost"]
    missing = [c for c in cases if c["case_type"] == "missing"]

    # KPI row
    cols = st.columns(4)
    cols[0].metric("Matched ✅", len(matched))
    cols[1].metric("Breaks ⚠️", len(breaks))
    cols[2].metric("Ghost 👻", len(ghosts))
    cols[3].metric("Missing ❓", len(missing))

    decisions = {}

    # Matched — bulk affirm
    if matched:
        with st.expander(f"✅ Matched Trades ({len(matched)})", expanded=False):
            matched_df = pd.DataFrame([
                {
                    "Trade ID": c.get("broker_trade", {}).get("trade_id", ""),
                    "Instrument": c.get("broker_trade", {}).get("instrument", ""),
                    "Qty": c.get("broker_trade", {}).get("quantity", ""),
                    "Price": c.get("broker_trade", {}).get("price", ""),
                    "Confidence": c.get("confidence_score", 0),
                } for c in matched
            ])
            st.dataframe(matched_df, width="stretch", hide_index=True)
            for c in matched:
                decisions[c["case_id"]] = "affirm"

    # Breaks — per-break decision
    if breaks:
        st.markdown(f"### ⚠️ Breaks ({len(breaks)})")
        for c in breaks:
            bt = c.get("broker_trade", {})
            mt = c.get("ms_trade", {})
            diffs = c.get("differences", {})
            with st.expander(f"Break: {bt.get('trade_id', 'unknown')} — {c.get('mismatch_reason', '')}", expanded=True):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**Broker Values**")
                    for field, detail in diffs.items():
                        if isinstance(detail, dict):
                            st.text(f"  {field}: {detail.get('broker', 'N/A')}")
                        else:
                            st.text(f"  {field}: {detail}")
                with col2:
                    st.markdown("**MS Values**")
                    for field, detail in diffs.items():
                        if isinstance(detail, dict):
                            st.text(f"  {field}: {detail.get('ms', 'N/A')}")

                action = st.radio(
                    f"Action for {bt.get('trade_id', c['case_id'][:8])}",
                    ["Request Resolution", "Accept as-is"],
                    key=f"break_{c['case_id']}",
                    horizontal=True,
                )
                decisions[c["case_id"]] = "request_resolution" if action == "Request Resolution" else "affirm"

    # Ghost trades
    if ghosts:
        st.markdown(f"### 👻 Ghost Trades — Broker only ({len(ghosts)})")
        for c in ghosts:
            bt = c.get("broker_trade", {})
            with st.expander(f"Ghost: {bt.get('trade_id', 'unknown')} — {bt.get('instrument', '')}"):
                st.json(bt)
                action = st.radio(
                    f"Action for {bt.get('trade_id', c['case_id'][:8])}",
                    ["Escalate to TSG", "Reject", "Flag for Booking"],
                    key=f"ghost_{c['case_id']}",
                    horizontal=True,
                )
                action_map = {"Escalate to TSG": "escalate_tsg", "Reject": "reject", "Flag for Booking": "flag_booking"}
                decisions[c["case_id"]] = action_map[action]

    # Missing
    if missing:
        with st.expander(f"❓ Missing — MS only ({len(missing)})", expanded=False):
            missing_df = pd.DataFrame([
                {
                    "Trade ID": c.get("ms_trade", {}).get("trade_id", ""),
                    "Instrument": c.get("ms_trade", {}).get("instrument", ""),
                    "Qty": c.get("ms_trade", {}).get("quantity", ""),
                } for c in missing
            ])
            st.dataframe(missing_df, width="stretch", hide_index=True)
            for c in missing:
                decisions[c["case_id"]] = "affirm"  # acknowledged

    # Submit
    st.markdown("---")
    if st.button("✅ Submit Affirmation", type="primary", width="stretch"):
        with st.spinner("Processing affirmation…"):
            result = _post(
                "/api/pipeline/affirm",
                json={"session_id": state["session_id"], "affirmation_decisions": decisions},
            )
        if result:
            st.session_state.pipeline_state = result
            if result.get("status") == "completed":
                st.session_state.page = "Results"
            st.rerun()


# ── Gate 3: Break Review Screen ──────────────────────────────────────────────

def _render_break_review_gate(state: dict):
    """Render resolution analysis for ops review."""
    st.header("🔍 Break Resolution Review (Gate 3)")
    st.markdown("Review the AI-generated root cause analysis and broker email drafts.")

    resolutions = state.get("resolution_results", [])
    if not resolutions:
        st.info("No resolutions to review.")
        return

    decisions = {}

    for res in resolutions:
        case_id = res["case_id"]
        severity_colors = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        sev = res.get("severity", "medium")
        icon = severity_colors.get(sev, "⚪")

        with st.expander(f"{icon} {res.get('break_type', 'unknown')} — Severity: {sev.upper()}", expanded=True):
            st.markdown(f"**Root Cause:** {res.get('root_cause', 'N/A')}")
            st.markdown(f"**Break Type:** {res.get('break_type', 'unknown')}")

            edited_email = st.text_area(
                "Draft Broker Email (editable)",
                value=res.get("draft_broker_email", ""),
                height=200,
                key=f"email_{case_id}",
            )
            reviewer_notes = st.text_input("Reviewer Notes", key=f"notes_{case_id}")
            approved = st.checkbox("Approve this resolution", value=True, key=f"approve_{case_id}")

            decisions[case_id] = {
                "approved": approved,
                "reviewer_notes": reviewer_notes,
                "edited_email": edited_email,
            }

    st.markdown("---")
    if st.button("✅ Submit Break Reviews", type="primary", width="stretch"):
        with st.spinner("Processing break reviews…"):
            result = _post(
                "/api/pipeline/approve-resolution",
                json={"session_id": state["session_id"], "break_review_decisions": decisions},
            )
        if result:
            st.session_state.pipeline_state = result
            if result.get("status") == "completed":
                st.session_state.page = "Results"
            st.rerun()


# ── Gate 4: Escalation Review Screen ─────────────────────────────────────────

def _render_escalation_gate(state: dict):
    """Render escalation drafts for ops approval."""
    st.header("⬆️ Escalation Review (Gate 4)")
    st.markdown("Review the escalation emails to Trade Support Group. Emails will be saved but **not sent** until email integration is enabled.")

    # Show evidence packages
    evidence = state.get("evidence_packages", [])
    if evidence:
        with st.expander(f"📋 Evidence Packages ({len(evidence)})", expanded=False):
            for ep in evidence:
                st.markdown(f"**Case:** {ep['case_id'][:8]}")
                st.markdown(f"**Summary:** {ep.get('corroboration_summary', '')}")
                for src in ep.get("sources", []):
                    corr = "✅" if src.get("corroborates_firm") is True else ("❌" if src.get("corroborates_firm") is False else "⏳")
                    st.caption(f"  {corr} {src['source_name']} ({src['source_type']})")
                st.markdown("---")

    escalations = state.get("escalation_drafts", [])
    if not escalations:
        st.info("No escalation drafts to review.")
        return

    decisions = {}
    for esc in escalations:
        case_id = esc["case_id"]
        urgency_colors = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        urg = esc.get("urgency", "medium")
        icon = urgency_colors.get(urg, "⚪")

        with st.expander(f"{icon} Escalation — Urgency: {urg.upper()}", expanded=True):
            st.text_area(
                "Escalation Email (read-only preview)",
                value=esc.get("draft_email", ""),
                height=250,
                key=f"esc_email_{case_id}",
                disabled=True,
            )
            reviewer_notes = st.text_input("Reviewer Notes", key=f"esc_notes_{case_id}")
            approved = st.checkbox("Approve for dispatch", value=True, key=f"esc_approve_{case_id}")

            decisions[case_id] = {
                "approved": approved,
                "reviewer_notes": reviewer_notes,
            }

    st.markdown("---")
    st.caption("📧 Emails will be saved for dispatch once email integration is enabled (Phase 7).")
    if st.button("✅ Submit Escalation Approvals", type="primary", width="stretch"):
        with st.spinner("Processing escalation approvals…"):
            result = _post(
                "/api/pipeline/approve-escalation",
                json={"session_id": state["session_id"], "escalation_decisions": decisions},
            )
        if result:
            st.session_state.pipeline_state = result
            if result.get("status") == "completed":
                st.session_state.page = "Results"
            st.rerun()


# ── Page: Results ────────────────────────────────────────────────────────────
def page_results():
    st.header("📊 Reconciliation Results")
    _render_session_header()
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

    # ── Action buttons ───────────────────────────────────────────────────
    btn_col1, btn_col2, _ = st.columns([1, 1, 2])
    with btn_col1:
        if st.button("🔄 Start New Session", width="stretch"):
            for k, v in _DEFAULTS.items():
                st.session_state[k] = v
            st.session_state.page = "Upload"
            st.rerun()
    with btn_col2:
        output_fn = state.get("output_filename")
        if output_fn:
            try:
                r = httpx.get(f"{API_BASE}/api/download/{output_fn}", timeout=30)
                r.raise_for_status()
                st.download_button(
                    label="📥 Download Report",
                    data=r.content,
                    file_name=output_fn,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    width="stretch",
                )
            except Exception as e:
                st.error(f"Download failed: {e}")

    st.markdown("---")

    # ── Tabbed reconciliation detail ─────────────────────────────────────
    tab_matched, tab_mismatched, tab_new, tab_missing, tab_all_trades, tab_email = st.tabs([
        f"Matched ({summary.get('matched_count', 0)})",
        f"Mismatched ({summary.get('mismatched_count', 0)})",
        f"New / Broker Only ({summary.get('new_trades_count', 0)})",
        f"Missing / MS Only ({summary.get('missing_trades_count', 0)})",
        "All Extracted Trades",
        "📧 Email Draft",
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
                         width="stretch", hide_index=True)
        else:
            st.info("No trade data in state.")

    with tab_email:
        _render_email_draft(state, summary)


def _render_email_draft(state: dict, summary: dict):
    """Render a template-based reconciliation summary email for the broker."""
    broker = state.get("broker_name") or "Unknown"
    invoice_id = state.get("invoice_id") or "N/A"
    invoice_date = state.get("invoice_date") or "N/A"
    trade_date_range = state.get("trade_date_range") or "N/A"
    currency = summary.get("currency") or "USD"
    today = datetime.now().strftime("%d %B %Y")

    matched_count = summary.get("matched_count", 0)
    mismatched_count = summary.get("mismatched_count", 0)
    new_count = summary.get("new_trades_count", 0)
    missing_count = summary.get("missing_trades_count", 0)
    broker_trade_count = summary.get("broker_trade_count", 0)
    matched_brokerage = summary.get("matched_brokerage", summary.get("broker_total_brokerage", 0))

    has_breaks = mismatched_count > 0 or new_count > 0

    if not has_breaks:
        # Clean match email
        subject = f"Trade Reconciliation Confirmation — {broker} — Invoice {invoice_id}"
        body = (
            f"Dear {broker} Operations,\n\n"
            f"We have completed reconciliation of your invoice {invoice_id} dated {invoice_date}\n"
            f"for the period {trade_date_range}.\n\n"
            f"Reconciliation Summary:\n"
            f"{'━' * 35}\n"
            f"  Total broker trades reviewed:  {broker_trade_count}\n"
            f"  Matched trades:                {matched_count} ✅\n"
            f"  Total brokerage confirmed:     {currency} {matched_brokerage:,.2f}\n\n"
            f"All trades have been verified against our internal records and confirmed as accurate.\n"
            f"This invoice is approved for settlement processing.\n\n"
            f"No further action is required from your side.\n\n"
            f"Best regards,\n"
            f"MS Trade Operations\n"
            f"{today}"
        )
    else:
        # Break email
        subject = f"Trade Reconciliation — Discrepancies Found — {broker} — Invoice {invoice_id}"

        # Build break details table
        break_lines = []
        mismatched = state.get("recon_mismatched", [])
        for m in mismatched:
            bt = m.get("broker_trade", {})
            mt = m.get("ms_trade", {})
            diffs = m.get("differences", {})
            tid = bt.get("trade_id") or "—"
            instr = bt.get("instrument") or "—"
            for field, detail in diffs.items():
                if isinstance(detail, dict):
                    break_lines.append(
                        f"  {tid:<12} {instr:<18} {field:<12} {str(detail.get('broker', 'N/A')):<10} {str(detail.get('ms', 'N/A')):<10}"
                    )

        break_table = ""
        if break_lines:
            header = f"  {'Trade ID':<12} {'Instrument':<18} {'Field':<12} {'Broker':<10} {'Our Records':<10}"
            sep = "  " + "-" * 62
            break_table = f"\nBreak Details:\n{sep}\n{header}\n{sep}\n" + "\n".join(break_lines) + f"\n{sep}\n"

        # Ghost trades
        ghost_lines = []
        new_trades = state.get("recon_new", [])
        for m in new_trades:
            bt = m.get("broker_trade", {})
            tid = bt.get("trade_id") or "—"
            instr = bt.get("instrument") or "—"
            qty = bt.get("quantity") or "—"
            price = bt.get("price") or "—"
            tdate = bt.get("trade_date") or "—"
            ghost_lines.append(f"  • Trade {tid}: {instr}, {qty} @ {price} on {tdate}")

        ghost_section = ""
        if ghost_lines:
            ghost_section = "\nGhost Trades (broker-only — no corresponding record in our system):\n" + "\n".join(ghost_lines) + "\n"

        body = (
            f"Dear {broker} Operations,\n\n"
            f"We have completed reconciliation of your invoice {invoice_id} dated {invoice_date}.\n"
            f"Please note the following discrepancies that require your attention:\n\n"
            f"Reconciliation Summary:\n"
            f"{'━' * 35}\n"
            f"  Total broker trades reviewed:  {broker_trade_count}\n"
            f"  Matched trades:                {matched_count} ✅\n"
            f"  Mismatched trades:             {mismatched_count} ⚠️\n"
            f"  Unmatched (broker-only):       {new_count} ❌\n"
            f"  Missing (our records only):    {missing_count} ❓\n"
            f"{break_table}"
            f"{ghost_section}\n"
            f"Please review these discrepancies and provide an amended recap at your earliest\n"
            f"convenience. For matched trades totaling {currency} {matched_brokerage:,.2f}, we will\n"
            f"proceed with settlement processing.\n\n"
            f"For questions, please contact MS Trade Operations.\n\n"
            f"Best regards,\n"
            f"MS Trade Operations\n"
            f"{today}"
        )

    # Check if there's a previously saved draft for this session
    session_id = state.get("session_id", "")
    saved_draft = _get(f"/api/pipeline/email-draft/{session_id}") if session_id else {}
    if saved_draft and saved_draft.get("found"):
        st.info(f"📄 Loaded saved draft: **{saved_draft.get('draft_file', '')}**")
        subject = saved_draft.get("subject", subject)
        body = saved_draft.get("body", body)

    subject_input = st.text_input("Subject", value=subject, key="email_draft_subject")
    email_text = st.text_area(
        "Email Body (editable)",
        value=body,
        height=450,
        key="email_draft_body",
    )

    # Attachment info
    output_fn = state.get("output_filename")
    if output_fn:
        st.caption(f"📎 Attachment: **{output_fn}**")
    elif saved_draft and saved_draft.get("attachment"):
        output_fn = saved_draft["attachment"]
        st.caption(f"📎 Attachment: **{output_fn}**")

    col_save, col_send, _ = st.columns([1, 1, 2])
    with col_save:
        if st.button("💾 Save Draft", type="primary", key="save_email_draft"):
            result = _post(
                "/api/pipeline/save-email-draft",
                json={
                    "session_id": session_id,
                    "broker_name": state.get("broker_name", ""),
                    "subject": subject_input,
                    "body": email_text,
                },
            )
            if result and result.get("status") == "saved":
                st.success(f"✅ Draft saved: **{result.get('draft_file', '')}**")
                if result.get("attachment"):
                    st.caption(f"📎 Recon report attached: {result['attachment']}")
            else:
                st.error("Failed to save draft.")
    with col_send:
        st.button(
            "📧 Send Email",
            disabled=True,
            key="send_email_btn",
            help="Email dispatch will be enabled in a future release.",
        )


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
    st.dataframe(df, width="stretch", hide_index=True,
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
    st.dataframe(df, width="stretch", hide_index=True,
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

    st.dataframe(sessions_df, width="stretch", hide_index=True)

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
            st.dataframe(pd.DataFrame(trades), width="stretch", hide_index=True)

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
                width="stretch", hide_index=True,
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

    # ── Email Draft ──────────────────────────────────────────────────────
    draft = _get(f"/api/pipeline/email-draft/{session_id}")
    if draft and draft.get("found"):
        with st.expander("📧 Email Draft", expanded=False):
            st.markdown(f"**Subject:** {draft.get('subject', '')}")
            draft_body = st.text_area(
                "Email Body (editable)",
                value=draft.get("body", ""),
                height=350,
                key=f"history_email_body_{session_id[:8]}",
            )
            if draft.get("attachment"):
                st.caption(f"📎 Attachment: **{draft['attachment']}**")
            st.caption(f"📄 Draft file: {draft.get('draft_file', '')}")

            col_resave, col_send, _ = st.columns([1, 1, 2])
            with col_resave:
                if st.button("💾 Save Draft", key=f"resave_draft_{session_id[:8]}"):
                    result = _post(
                        "/api/pipeline/save-email-draft",
                        json={
                            "session_id": session_id,
                            "broker_name": detail.get("broker_name", ""),
                            "subject": draft.get("subject", ""),
                            "body": draft_body,
                        },
                    )
                    if result and result.get("status") == "saved":
                        st.success(f"✅ Draft re-saved: {result.get('draft_file', '')}")
                    else:
                        st.error("Failed to save draft.")
            with col_send:
                st.button(
                    "📧 Send Email",
                    disabled=True,
                    key=f"send_email_history_{session_id[:8]}",
                    help="Email dispatch will be enabled in a future release.",
                )


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
        st.dataframe(df, width="stretch", hide_index=True,
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

    st.dataframe(display_df, width="stretch", hide_index=True)
    st.caption(f"{len(data)} optimized prompt(s) cached")


# ── Router ───────────────────────────────────────────────────────────────────
nav()
page = st.session_state.page
if page == "Batch":
    page_batch()
elif page == "Upload":
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
