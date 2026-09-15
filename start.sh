#!/usr/bin/env bash
# Start the mock mail server then launch langgraph dev.
# Run from the sales_assistant directory: ./start.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Kill any leftover mail server from a previous run ----
# Detect OS and choose the right command.
if command -v lsof >/dev/null 2>&1; then
    # Linux / macOS / Git Bash com lsof
    OLD_PID=$(lsof -ti :5002 2>/dev/null || true)
elif command -v netstat >/dev/null 2>&1; then
    # Windows (Git Bash / MSYS) — pega o PID pela porta
    OLD_PID=$(netstat -ano 2>/dev/null | grep ":5002" | grep LISTENING | awk '{print $5}' | head -n1)
fi

if [ -n "${OLD_PID:-}" ]; then
    echo "Port 5002 already in use (PID $OLD_PID) — killing it ..."
    kill "$OLD_PID" 2>/dev/null || taskkill //PID "$OLD_PID" //F 2>/dev/null || true
    sleep 1
fi

# ---- Start mock mail server ----
echo "Starting mock mail server on http://127.0.0.1:5002 ..."
poetry run python "$SCRIPT_DIR/mcp/mail_server.py" &
MAIL_PID=$!

cleanup() {
    echo "Shutting down mail server (PID $MAIL_PID)..."
    kill "$MAIL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- Wait until the server accepts connections (up to 10s) ----
for i in $(seq 1 10); do
    if curl -s --max-time 1 http://127.0.0.1:5002/ping >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "Mail server up (PID $MAIL_PID). Starting langgraph dev ..."
poetry run langgraph dev "$@"