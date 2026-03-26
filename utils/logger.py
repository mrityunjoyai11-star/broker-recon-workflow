"""Logging utility."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

_configured: set[str] = set()


def _load_log_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "dev.yaml"
    try:
        with open(cfg_path) as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. Safe to call repeatedly."""
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    cfg = _load_log_config()
    app_cfg = cfg.get("app", {})
    log_cfg = cfg.get("logging", {})

    log_level = getattr(logging, app_cfg.get("log_level", "INFO").upper(), logging.INFO)
    logger.setLevel(log_level)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt=log_cfg.get("format", "%(asctime)s | %(name)-20s | %(levelname)s | %(message)s"),
        datefmt=log_cfg.get("date_format", "%Y-%m-%d %H:%M:%S"),
    )

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(log_level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    log_file = log_cfg.get("file", "logs/app.log")
    log_dir = Path(__file__).parent.parent / Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    full_log_path = Path(__file__).parent.parent / log_file

    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        fh = RotatingFileHandler(
            full_log_path,
            maxBytes=log_cfg.get("max_bytes", 10_485_760),
            backupCount=log_cfg.get("backup_count", 5),
        )
        fh.setLevel(log_level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    _configured.add(name)
    return logger
