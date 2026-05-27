#!/usr/bin/env bash
# Igual ao dev.sh mas com logs persistidos em logs/backend-YYYYmmdd-HHMMSS.log.
# Use quando estiver indo pro primeiro trade real e quiser tail -f num
# segundo terminal.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

FRONTEND_PORT="${FRONTEND_PORT:-3001}"

mkdir -p logs
LOG_FILE="logs/backend-$(date +%Y%m%d-%H%M%S).log"
LOG_LATEST="logs/backend-latest.log"
ln -sf "$(basename "$LOG_FILE")" "$LOG_LATEST"
echo "Backend logs → $LOG_FILE"
echo "Em outro terminal:"
echo "  tail -f $LOG_LATEST"
echo

# Redirect direto pro arquivo (sem tee). Evita o problema de SIGPIPE matar
# o uvicorn quando o terminal de log fecha.
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000 >> "$LOG_FILE" 2>&1 &
BACKEND_PID=$!
echo "Backend started (pid=$BACKEND_PID) — logs vão pro arquivo, não pra cá."

cleanup() {
  echo
  echo "Stopping dev servers..."
  kill -TERM "$BACKEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(cd frontend && npm run dev -- --port "$FRONTEND_PORT")
