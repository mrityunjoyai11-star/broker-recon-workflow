"""Upload endpoint — accepts PDF(s) + Excel(s) pair and saves to disk."""

from __future__ import annotations

import uuid
from typing import List

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
    pdf_file: List[UploadFile] = File(...),
    excel_file: List[UploadFile] = File(...),
    broker_hint: str = Form(default=""),
    flow_type: str = Form(default="receivable"),
):
    """Accept one or more PDF + Excel uploads and return a session_id with saved paths.

    flow_type: "receivable" (default) or "payable".
    Multiple PDFs/Excels are supported — the first of each is the 'primary' pair.
    """
    if flow_type not in ("receivable", "payable"):
        raise HTTPException(status_code=400, detail="flow_type must be 'receivable' or 'payable'")

    # Validate file extensions
    for pf in pdf_file:
        if not pf.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"File '{pf.filename}' must be a PDF")
    for ef in excel_file:
        if not ef.filename.lower().endswith((".xlsx", ".xls", ".csv")):
            raise HTTPException(status_code=400, detail=f"File '{ef.filename}' must be an Excel/CSV")

    session_id = str(uuid.uuid4())

    # Save all files, track paths
    pdf_paths: list[str] = []
    excel_paths: list[str] = []

    for pf in pdf_file:
        data = await pf.read()
        path = save_uploaded_file(data, pf.filename)
        pdf_paths.append(str(path))

    for ef in excel_file:
        data = await ef.read()
        path = save_uploaded_file(data, ef.filename)
        excel_paths.append(str(path))

    # Primary pair is the first of each
    primary_pdf = pdf_paths[0]
    primary_excel = excel_paths[0]

    # Create a DB session row so in-progress pipelines are trackable
    factory = get_session_factory()
    db = factory()
    try:
        db.add(ReconciliationSession(
            id=session_id,
            pdf_filename=pdf_file[0].filename,
            excel_filename=excel_file[0].filename,
            broker_name=broker_hint or None,
            status="uploaded",
            flow_type=flow_type,
        ))
        db.commit()
    except Exception as exc:
        logger.warning("Failed to create DB session row: %s", exc)
        db.rollback()
    finally:
        db.close()

    logger.info(
        "Upload: session=%s flow=%s PDFs=%d Excels=%d",
        session_id, flow_type, len(pdf_paths), len(excel_paths),
    )

    return JSONResponse({
        "session_id": session_id,
        "flow_type": flow_type,
        "pdf_path": primary_pdf,
        "excel_path": primary_excel,
        "pdf_paths": pdf_paths,
        "excel_paths": excel_paths,
        "pdf_filename": pdf_file[0].filename,
        "excel_filename": excel_file[0].filename,
        "broker_hint": broker_hint or None,
        "message": "Files uploaded. Call /api/pipeline/start to begin.",
    })
