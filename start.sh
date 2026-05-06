#!/bin/bash
# ============================================================
# Brokerage Reconciliation System v2 — Start (Daemon Mode)
# ============================================================
# Runs FastAPI backend + Streamlit UI as background processes.
# All output is redirected to logs/. Terminal returns immediately.
#
# Usage:
#   ./start.sh           # both API + UI
#   ./start.sh api       # API only
#   ./start.sh ui        # UI only
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"  # ms_payables/

LOG_DIR="$SCRIPT_DIR/logs"
PID_DIR="$SCRIPT_DIR/.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

API_LOG="$LOG_DIR/api.log"
UI_LOG="$LOG_DIR/ui.log"
API_PID_FILE="$PID_DIR/api.pid"
UI_PID_FILE="$PID_DIR/ui.pid"

# ── Load .env if present ───────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Activate venv ──────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/venv"
[ -d "$VENV_DIR" ] || VENV_DIR="$ROOT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
fi

export PYTHONPATH="$ROOT_DIR:$PYTHONPATH"

MODE="${1:-both}"

is_running() {
    local pid_file="$1"
    [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null
}

start_api() {
    if is_running "$API_PID_FILE"; then
        echo "⚠️  API already running (PID $(cat "$API_PID_FILE"))"
        return
    fi
    echo "▶ Starting FastAPI backend (logs → $API_LOG)..."
    nohup uvicorn broker_recon_flow.backend.main:app \
        --host 0.0.0.0 \
        --port 8021 \
        --app-dir "$ROOT_DIR" \
        >> "$API_LOG" 2>&1 &
    echo $! > "$API_PID_FILE"
    sleep 1
    if is_running "$API_PID_FILE"; then
        echo "✅ API started (PID $(cat "$API_PID_FILE"))"
    else
        echo "❌ API failed to start. Check $API_LOG"
        rm -f "$API_PID_FILE"
        exit 1
    fi
}

start_ui() {
    if is_running "$UI_PID_FILE"; then
        echo "⚠️  UI already running (PID $(cat "$UI_PID_FILE"))"
        return
    fi
    echo "▶ Starting Streamlit UI (logs → $UI_LOG)..."
    nohup streamlit run "$SCRIPT_DIR/ui/app.py" \
        --server.port 8503 \
        --server.address 0.0.0.0 \
        --server.headless true \
        --browser.gatherUsageStats false \
        >> "$UI_LOG" 2>&1 &
    echo $! > "$UI_PID_FILE"
    sleep 1
    if is_running "$UI_PID_FILE"; then
        echo "✅ UI started (PID $(cat "$UI_PID_FILE"))"
    else
        echo "❌ UI failed to start. Check $UI_LOG"
        rm -f "$UI_PID_FILE"
        exit 1
    fi
}

case "$MODE" in
    api)  start_api ;;
    ui)   start_ui ;;
    both)
        start_api
        sleep 2
        start_ui
        ;;
    *)
        echo "Usage: $0 [api|ui|both]"
        exit 1
        ;;
esac

echo ""
echo "============================================"
echo "  API:  http://localhost:8021"
echo "  UI:   http://localhost:8503"
echo "  Docs: http://localhost:8021/docs"
echo ""
echo "  Logs:    tail -f $LOG_DIR/api.log"
echo "           tail -f $LOG_DIR/ui.log"
echo "  Stop:    ./stop.sh"
echo "============================================"
