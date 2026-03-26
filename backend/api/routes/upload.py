"""Upload endpoint — accepts PDF + Excel pair and saves to disk."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse

from broker_recon_flow.db.database import get_session_factory
from broker_recon_flow.db.models import ReconciliationSession
from broker_recon_flow.services.storage_service import save_uploaded_file
from broker_recon_flow.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/upload")
async def upload_files(
    pdf_file: UploadFile = File(...),
    excel_file: UploadFile = File(...),
    broker_hint: str = Form(default=""),
):
    """Accept a PDF + Excel upload pair and return a session_id with saved paths."""
    if not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="First file must be a PDF")
    if not excel_file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Second file must be an Excel/CSV")

    session_id = str(uuid.uuid4())

    pdf_bytes = await pdf_file.read()
    excel_bytes = await excel_file.read()

    pdf_path = save_uploaded_file(pdf_bytes, pdf_file.filename)
    excel_path = save_uploaded_file(excel_bytes, excel_file.filename)

    # Create a DB session row so in-progress pipelines are trackable
    factory = get_session_factory()
    db = factory()
    try:
        db.add(ReconciliationSession(
            id=session_id,
            pdf_filename=pdf_file.filename,
            excel_filename=excel_file.filename,
            broker_name=broker_hint or None,
            status="uploaded",
        ))
        db.commit()
    except Exception as exc:
        logger.warning("Failed to create DB session row: %s", exc)
        db.rollback()
    finally:
        db.close()

    logger.info("Upload: session=%s PDF=%s Excel=%s", session_id, pdf_path, excel_path)

    return JSONResponse({
        "session_id": session_id,
        "pdf_path": str(pdf_path),
        "excel_path": str(excel_path),
        "pdf_filename": pdf_file.filename,
        "excel_filename": excel_file.filename,
        "broker_hint": broker_hint or None,
        "message": "Files uploaded. Call /api/pipeline/start to begin.",
    })
