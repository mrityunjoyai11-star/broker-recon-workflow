"""Download endpoint — serve the generated Excel report."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from broker_recon_flow.services.storage_service import get_output_path
from broker_recon_flow.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/{filename}")
def download_file(filename: str):
    """Serve a generated output file by name."""
    # Sanitise: only filenames, no path traversal
    safe_name = Path(filename).name
    file_path = get_output_path(safe_name)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{safe_name}' not found")

    logger.info("Download: %s", safe_name)
    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=safe_name,
    )
