"""Batch processor — watches NAS_PATH/{payables,receivable}/ for new files
and runs them through the pipeline automatically.

Default flow:
  - Drop file in NAS_PATH/payables/ or NAS_PATH/receivable/
  - Watcher picks it up, creates a session, runs:
      verify → classify → SIPDO optimize → extract → reconcile → generate → persist
  - HITL is auto-approved (no human gate)
  - If extraction confidence >= MIN_CONFIDENCE: save email draft, move file to Processed/
  - Otherwise: move file to Error/ with .error.txt sidecar
  - Max BATCH_CONCURRENCY parallel jobs
"""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.db.models import ReconciliationSession
from broker_recon_flow.graph.state import GraphState
from broker_recon_flow.graph.workflow import get_graph
from broker_recon_flow.services.storage_service import save_uploaded_file
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
NAS_ROOT = BASE_DIR / "data" / "NAS_PATH"
POLL_INTERVAL_SEC = 5
BATCH_CONCURRENCY = 4
MIN_CONFIDENCE = 0.80
SUPPORTED_EXTS = (".pdf",)

# ── State ────────────────────────────────────────────────────────────────────
_executor: Optional[ThreadPoolExecutor] = None
_seen_files: set[str] = set()
_active_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_watcher_thread: Optional[threading.Thread] = None
_stop_flag = threading.Event()


def _flow_dirs() -> dict[str, dict[str, Path]]:
    return {
        "payable": {
            "watch": NAS_ROOT / "payables",
            "processed": NAS_ROOT / "payables" / "Processed",
            "error": NAS_ROOT / "payables" / "Error",
        },
        "receivable": {
            "watch": NAS_ROOT / "receivable",
            "processed": NAS_ROOT / "receivable" / "Processed",
            "error": NAS_ROOT / "receivable" / "Error",
        },
    }


def _ensure_dirs():
    for dirs in _flow_dirs().values():
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)


def _record_job(session_id: str, **kwargs):
    with _jobs_lock:
        if session_id not in _active_jobs:
            _active_jobs[session_id] = {}
        _active_jobs[session_id].update(kwargs)


def get_active_jobs() -> list[dict]:
    """Return a snapshot of active/recent jobs (most recent first, max 50)."""
    with _jobs_lock:
        items = [{"session_id": sid, **info} for sid, info in _active_jobs.items()]
    items.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    return items[:50]


def _move_file(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = dst_dir / f"{timestamp}_{src.name}"
    src.rename(dst)
    return dst


def _process_file(file_path: Path, flow_type: str):
    """Run the full pipeline on one file. Move to Processed/ or Error/ on completion."""
    session_id = str(uuid.uuid4())
    started = datetime.now().isoformat(timespec="seconds")
    _record_job(
        session_id,
        file=file_path.name,
        flow_type=flow_type,
        status="starting",
        started_at=started,
        broker_name=None,
    )
    logger.info("[batch] Processing: %s (flow=%s, session=%s)", file_path.name, flow_type, session_id[:8])

    try:
        # ── Copy to raw_files/ via storage helper ────────────────────────
        file_bytes = file_path.read_bytes()
        saved_path = save_uploaded_file(file_bytes, file_path.name)

        # ── Insert DB session row ────────────────────────────────────────
        factory = get_session_factory()
        db = factory()
        try:
            db.add(ReconciliationSession(
                id=session_id,
                pdf_filename=file_path.name,
                broker_name=None,
                status="uploaded",
                flow_type=flow_type,
            ))
            db.commit()
        finally:
            db.close()

        # ── Phase 1: verify → classify → [interrupt at sipdo_choice or hitl] ──
        graph, _ = get_graph()
        config = {"configurable": {"thread_id": session_id}}
        initial_state = GraphState(
            session_id=session_id,
            flow_type=flow_type,
            pdf_path=str(saved_path),
            pdf_paths=[str(saved_path)],
        )
        _record_job(session_id, status="verifying")
        for _ in graph.stream(initial_state.model_dump(), config=config):
            pass

        current = graph.get_state(config)
        state = GraphState(**current.values) if current else initial_state
        _record_job(session_id, broker_name=state.broker_name, status=state.status)

        # ── Phase 2: SIPDO choice (default = optimize) ───────────────────
        if state.sipdo_choice_pending or state.status == "sipdo_choice":
            _record_job(session_id, status="optimizing")
            graph.update_state(config, {
                "sipdo_strategy": "optimize",
                "sipdo_choice_pending": False,
            })
            for _ in graph.stream(None, config=config):
                pass
            current = graph.get_state(config)
            state = GraphState(**current.values) if current else state
            _record_job(
                session_id,
                broker_name=state.broker_name,
                status=state.status,
                sipdo_accuracy=state.sipdo_accuracy_score,
            )

        # ── Phase 3: HITL — auto-approve if confidence sufficient ────────
        confidence = state.extraction.confidence if state.extraction else 0.0
        trade_count = state.extraction.trade_count if state.extraction else 0
        _record_job(
            session_id,
            confidence=confidence,
            trade_count=trade_count,
            sipdo_accuracy=state.sipdo_accuracy_score,
        )

        if state.status not in ("hitl_review",) and not state.hitl_pending:
            raise RuntimeError(f"Pipeline did not reach HITL stage (status={state.status})")

        if confidence < MIN_CONFIDENCE or trade_count == 0:
            raise RuntimeError(
                f"Extraction quality below threshold: confidence={confidence:.0%} "
                f"(min={MIN_CONFIDENCE:.0%}), trades={trade_count}. Manual review required."
            )

        # Auto-approve and run remainder
        _record_job(session_id, status="reconciling")
        graph.update_state(config, {
            "hitl_approved": True,
            "hitl_feedback": "auto-approved by batch processor",
            "hitl_pending": False,
        })
        for _ in graph.stream(None, config=config):
            pass
        current = graph.get_state(config)
        state = GraphState(**current.values) if current else state
        _record_job(session_id, status=state.status)

        if state.status != "completed":
            raise RuntimeError(f"Pipeline did not complete (status={state.status}, error={state.error})")

        # ── Phase 4: Save email draft ────────────────────────────────────
        try:
            _autosave_email_draft(state)
        except Exception as exc:
            logger.warning("[batch] Email draft autosave failed: %s", exc)

        # ── Move file to Processed/ ──────────────────────────────────────
        dirs = _flow_dirs()[flow_type]
        moved = _move_file(file_path, dirs["processed"])
        _record_job(
            session_id,
            status="processed",
            confidence=confidence,
            trade_count=trade_count,
            sipdo_accuracy=state.sipdo_accuracy_score,
            output_file=state.output_filename,
            moved_to=str(moved.relative_to(NAS_ROOT)),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        logger.info("[batch] ✅ Processed: %s → %s", file_path.name, moved.name)

    except Exception as exc:
        logger.exception("[batch] ❌ Failed: %s", file_path.name)
        # Mark the DB session as failed so it shows up in History with the error
        try:
            factory = get_session_factory()
            db = factory()
            try:
                row = db.query(ReconciliationSession).filter_by(id=session_id).first()
                if row:
                    row.status = "failed"
                    row.error_message = str(exc)[:1000]
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("[batch] Could not update DB session status to failed")

        try:
            dirs = _flow_dirs()[flow_type]
            if file_path.exists():
                moved = _move_file(file_path, dirs["error"])
                # Sidecar with traceback
                sidecar = moved.with_suffix(moved.suffix + ".error.txt")
                sidecar.write_text(
                    f"Session: {session_id}\nFlow: {flow_type}\nError: {exc}\n\n"
                    f"Traceback:\n{traceback.format_exc()}",
                    encoding="utf-8",
                )
                _record_job(
                    session_id,
                    status="error",
                    error=str(exc),
                    moved_to=str(moved.relative_to(NAS_ROOT)),
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            else:
                _record_job(
                    session_id,
                    status="error",
                    error=str(exc),
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
        except Exception:
            logger.exception("[batch] Failed to move file to Error/")


def _autosave_email_draft(state: GraphState):
    """Build a template email draft and save it to email_drafts_saved/."""
    from broker_recon_flow.db.models import AuditEvent
    from broker_recon_flow.schemas.canonical_trade import AuditEventType

    broker = state.broker_name or "Unknown"
    summary = state.reconciliation.summary if state.reconciliation else {}
    matched_count = summary.get("matched_count", 0)
    mismatched_count = summary.get("mismatched_count", 0)
    new_count = summary.get("new_trades_count", 0)
    has_breaks = mismatched_count > 0 or new_count > 0
    today = datetime.now().strftime("%d %B %Y")

    if not has_breaks:
        subject = f"Trade Reconciliation Confirmation — {broker}"
        body = (
            f"Dear {broker} Operations,\n\n"
            f"We have completed reconciliation of your invoice.\n\n"
            f"All {matched_count} trades have been verified against our internal records.\n"
            f"This invoice is approved for settlement processing.\n\n"
            f"Best regards,\nMS Trade Operations\n{today}"
        )
    else:
        subject = f"Trade Reconciliation — Discrepancies Found — {broker}"
        body = (
            f"Dear {broker} Operations,\n\n"
            f"We have completed reconciliation of your invoice. Please review:\n"
            f"  Matched: {matched_count}\n"
            f"  Mismatched: {mismatched_count}\n"
            f"  Unmatched (broker-only): {new_count}\n\n"
            f"Please review these discrepancies and respond at your earliest convenience.\n\n"
            f"Best regards,\nMS Trade Operations\n{today}"
        )

    safe_broker = broker.replace(" ", "_")
    for ch in r'/\:*?"<>|':
        safe_broker = safe_broker.replace(ch, "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    drafts_dir = BASE_DIR / "data" / "email_drafts_saved"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    filename = f"email_draft_{safe_broker}_{state.session_id[:8]}_{timestamp}.txt"
    content = (
        f"Subject: {subject}\n"
        f"Session: {state.session_id}\n"
        f"Broker: {broker}\n"
        f"Saved: {datetime.now().isoformat()}\n"
        f"Attachment: {state.output_filename or 'none'}\n"
        f"{'=' * 60}\n\n{body}"
    )
    (drafts_dir / filename).write_text(content, encoding="utf-8")

    # Audit
    factory = get_session_factory()
    db = factory()
    try:
        db.add(AuditEvent(
            session_id=state.session_id,
            event_type=AuditEventType.EMAIL_DRAFT_SAVED.value,
            details={
                "subject": subject,
                "draft_file": filename,
                "attachment": state.output_filename,
                "auto": True,
            },
            timestamp=datetime.utcnow(),
        ))
        db.commit()
    finally:
        db.close()


def _scan_and_dispatch():
    """Scan watch dirs and dispatch new files to the executor."""
    if _executor is None:
        return
    for flow_type, dirs in _flow_dirs().items():
        watch_dir = dirs["watch"]
        if not watch_dir.exists():
            continue
        for f in watch_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXTS:
                continue
            abs_path = str(f.resolve())
            if abs_path in _seen_files:
                continue
            _seen_files.add(abs_path)
            logger.info("[batch] Detected new file: %s (flow=%s)", f.name, flow_type)
            _executor.submit(_process_file, f, flow_type)


def _watch_loop():
    logger.info("[batch] Watcher loop started (poll every %ds)", POLL_INTERVAL_SEC)
    while not _stop_flag.is_set():
        try:
            _scan_and_dispatch()
        except Exception:
            logger.exception("[batch] Watcher iteration error")
        _stop_flag.wait(POLL_INTERVAL_SEC)
    logger.info("[batch] Watcher loop stopped")


def start_watcher():
    """Idempotently start the file watcher + executor."""
    global _executor, _watcher_thread
    _ensure_dirs()
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=BATCH_CONCURRENCY, thread_name_prefix="batch")
    if _watcher_thread is None or not _watcher_thread.is_alive():
        _stop_flag.clear()
        _watcher_thread = threading.Thread(target=_watch_loop, daemon=True, name="batch-watcher")
        _watcher_thread.start()
        logger.info("[batch] Watcher started (concurrency=%d, root=%s)", BATCH_CONCURRENCY, NAS_ROOT)


def stop_watcher():
    _stop_flag.set()
    logger.info("[batch] Watcher stop requested")


def watcher_running() -> bool:
    return _watcher_thread is not None and _watcher_thread.is_alive()


def list_folder_files() -> dict:
    """Return file listings for each watch/Processed/Error folder per flow."""
    out: dict = {}
    for flow_type, dirs in _flow_dirs().items():
        out[flow_type] = {}
        for label, d in dirs.items():
            if d.exists():
                files = sorted(
                    [
                        {"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
                        for f in d.iterdir() if f.is_file()
                    ],
                    key=lambda x: x["mtime"], reverse=True,
                )
            else:
                files = []
            out[flow_type][label] = files[:50]
    return out
