#!/usr/bin/env bash
# Sobe backend (FastAPI) + frontend (Next.js) em paralelo.
# Sucessor de dev.sh — usa backend.app (não backend.main).
set -euo pipefail

cd "$(dirname "$0")"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cleanup() {
  echo
  echo "Stopping dev servers..."
  kill -TERM "$BACKEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(cd frontend && npm run dev)
