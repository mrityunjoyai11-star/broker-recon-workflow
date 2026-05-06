"""Batch processing endpoints — file watcher status, jobs, folder contents."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from broker_recon_flow.services import batch_processor as bp
from broker_recon_flow.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/jobs")
def list_jobs():
    """Return active and recent batch jobs (last 50)."""
    return JSONResponse(bp.get_active_jobs())


@router.get("/folders")
def list_folders():
    """Return file listings for watch/Processed/Error folders per flow."""
    return JSONResponse(bp.list_folder_files())


@router.get("/status")
def watcher_status():
    """Return watcher runtime configuration + status."""
    return JSONResponse({
        "running": bp.watcher_running(),
        "concurrency": bp.BATCH_CONCURRENCY,
        "min_confidence": bp.MIN_CONFIDENCE,
        "poll_interval_sec": bp.POLL_INTERVAL_SEC,
        "nas_root": str(bp.NAS_ROOT),
    })


@router.post("/start")
def start_watcher():
    """Idempotently start the file watcher."""
    bp.start_watcher()
    return JSONResponse({"status": "started", "running": bp.watcher_running()})


@router.post("/stop")
def stop_watcher():
    """Request the watcher to stop (best-effort)."""
    bp.stop_watcher()
    return JSONResponse({"status": "stopping"})


@router.post("/upload")
async def upload_to_watch_dir(
    pdf_file: UploadFile = File(...),
    flow_type: str = Form(...),
):
    """Drop a PDF directly into the watch dir for the given flow.

    The watcher will pick it up on its next poll cycle (~5s).
    """
    if flow_type not in ("payable", "receivable"):
        raise HTTPException(status_code=400, detail="flow_type must be 'payable' or 'receivable'")
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    dirs = bp._flow_dirs()[flow_type]
    watch_dir = dirs["watch"]
    watch_dir.mkdir(parents=True, exist_ok=True)
    target = watch_dir / pdf_file.filename
    data = await pdf_file.read()
    target.write_bytes(data)
    logger.info("[batch] File dropped into %s: %s (%d bytes)", flow_type, pdf_file.filename, len(data))
    return JSONResponse({
        "status": "queued",
        "flow_type": flow_type,
        "path": str(target),
        "filename": pdf_file.filename,
    })
