"""FastAPI application — thin trigger layer over the LangGraph pipeline."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from broker_recon_flow.config import get_server_config
from broker_recon_flow.db.database import init_db
from broker_recon_flow.services.ms_data_service import load_ms_data
from broker_recon_flow.utils.logger import get_logger
from broker_recon_flow.backend.api.routes import upload, pipeline, download, status

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle hooks."""
    logger.info("Starting up — initialising DB and MS data")
    init_db()
    load_ms_data()
    logger.info("Ready.")
    yield
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Brokerage Reconciliation API",
    version="2.0.0",
    description="LangGraph-powered broker vs MS data reconciliation pipeline",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(upload.router, prefix="/api", tags=["upload"])
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(download.router, prefix="/api/download", tags=["download"])
app.include_router(status.router, prefix="/api/status", tags=["status"])


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}
