#!/bin/bash
# ============================================================
# Brokerage Reconciliation System v2 — Stop Script
# ============================================================
# Gracefully stops the background API + UI processes started
# by start.sh. Reads PIDs from .pids/ and falls back to pkill
# if PID files are missing.
#
# Usage:
#   ./stop.sh            # stop both
#   ./stop.sh api        # stop API only
#   ./stop.sh ui         # stop UI only
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pids"

API_PID_FILE="$PID_DIR/api.pid"
UI_PID_FILE="$PID_DIR/ui.pid"

MODE="${1:-both}"

stop_pid_file() {
    local pid_file="$1"
    local label="$2"

    if [ ! -f "$pid_file" ]; then
        echo "ℹ️  No PID file for $label — using pkill fallback"
        return 1
    fi

    local pid
    pid=$(cat "$pid_file")
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "ℹ️  $label (PID $pid) is not running — cleaning stale PID file"
        rm -f "$pid_file"
        return 0
    fi

    echo "■ Stopping $label (PID $pid)..."
    kill "$pid" 2>/dev/null || true

    # Wait up to 8s for graceful shutdown
    for i in {1..8}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 1
    done

    # Force kill if still alive
    if kill -0 "$pid" 2>/dev/null; then
        echo "⚠️  $label did not stop gracefully — sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$pid_file"
    echo "✅ $label stopped"
    return 0
}

stop_api() {
    if ! stop_pid_file "$API_PID_FILE" "API"; then
        # Fallback: pkill any uvicorn running our app
        if pgrep -f "broker_recon_flow.backend.main:app" >/dev/null; then
            echo "■ Killing stray uvicorn processes..."
            pkill -f "broker_recon_flow.backend.main:app" || true
            sleep 1
            echo "✅ API processes killed"
        fi
    fi
}

stop_ui() {
    if ! stop_pid_file "$UI_PID_FILE" "UI"; then
        # Fallback: pkill any streamlit running our app
        if pgrep -f "streamlit run.*ui/app.py" >/dev/null; then
            echo "■ Killing stray streamlit processes..."
            pkill -f "streamlit run.*ui/app.py" || true
            sleep 1
            echo "✅ UI processes killed"
        fi
    fi
}

case "$MODE" in
    api)  stop_api ;;
    ui)   stop_ui ;;
    both)
        stop_ui
        stop_api
        ;;
    *)
        echo "Usage: $0 [api|ui|both]"
        exit 1
        ;;
esac

echo ""
echo "Done. To restart: ./start.sh"
