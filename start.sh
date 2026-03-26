#!/bin/bash
# ============================================================
# Brokerage Reconciliation System v2 — Start Script
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"  # ms_payables/

# Use venv if it exists — check project dir first, then parent
VENV_DIR="$SCRIPT_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    VENV_DIR="$ROOT_DIR/venv"
fi
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
fi

export PYTHONPATH="$ROOT_DIR:$PYTHONPATH"

# ── Parse mode ──────────────────────────────────────────────
MODE="${1:-both}"   # api | ui | both

start_api() {
    echo "Starting FastAPI backend..."
    uvicorn broker_recon_flow.backend.main:app \
        --host 0.0.0.0 \
        --port 8001 \
        --reload \
        --app-dir "$ROOT_DIR"
}

start_ui() {
    echo "Starting Streamlit frontend..."
    streamlit run "$SCRIPT_DIR/ui/app.py" \
        --server.port 8501 \
        --server.address 0.0.0.0 \
        --server.headless true
}

case "$MODE" in
    api)
        start_api
        ;;
    ui)
        start_ui
        ;;
    both)
        start_api &
        API_PID=$!
        sleep 3
        start_ui &
        UI_PID=$!
        echo ""
        echo "============================================"
        echo "  API:  http://localhost:8001"
        echo "  UI:   http://localhost:8501"
        echo "  Docs: http://localhost:8001/docs"
        echo "============================================"
        echo "Press Ctrl+C to stop both services"
        wait $API_PID $UI_PID
        ;;
    *)
        echo "Usage: $0 [api|ui|both]"
        exit 1
        ;;
esac
