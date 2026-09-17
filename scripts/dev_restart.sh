#!/usr/bin/env bash
# Restart the local dev server in the background. Usage: bash scripts/dev_restart.sh [port] [logfile]
PORT="${1:-8000}"; LOG="${2:-/tmp/clipforge_dev.log}"
pkill -f "clipforge serve --port ${PORT}" 2>/dev/null; sleep 1
cd "$(dirname "$0")/.."
setsid nohup python3 -m clipforge serve --port "${PORT}" > "${LOG}" 2>&1 &
sleep 3
curl -s -o /dev/null -w "server: %{http_code}\n" "http://127.0.0.1:${PORT}/login"
