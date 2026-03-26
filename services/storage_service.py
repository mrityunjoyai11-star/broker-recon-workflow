"""Storage service — file I/O for uploads, parsed files, and output."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from broker_recon_flow.config import get_storage_config
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# Anchor to the broker_recon_flow/ directory (one level up from services/)
BASE_DIR = Path(__file__).parent.parent


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_storage_path(subdir: str) -> Path:
    cfg = get_storage_config()
    return _ensure(BASE_DIR / cfg.get(subdir, f"data/{subdir}"))


def save_uploaded_file(file_bytes: bytes, original_name: str, subdir: str = "raw_files") -> Path:
    """Save raw uploaded bytes; return the saved path."""
    storage = get_storage_path(subdir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = original_name.replace(" ", "_")
    dest = storage / f"{timestamp}_{safe_name}"
    dest.write_bytes(file_bytes)
    logger.info("Saved uploaded file: %s", dest)
    return dest


def save_output_file(file_bytes: bytes, filename: str) -> Path:
    storage = get_storage_path("normalized_output")
    dest = storage / filename
    dest.write_bytes(file_bytes)
    logger.info("Saved output file: %s", dest)
    return dest


def get_output_path(filename: str) -> Path:
    return get_storage_path("normalized_output") / filename


def list_files(subdir: str = "raw_files") -> list[Path]:
    return sorted(get_storage_path(subdir).iterdir())
