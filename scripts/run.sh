#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Starting Concierge at http://localhost:8000"
echo "Model directory: $(pwd)/models"
ls models/*.gguf 2>/dev/null && echo "Model found." || echo "No model yet — run scripts/fetch_model.sh"
echo ""

cd backend
.venv/bin/python3.12 -m uvicorn server:app --reload --host 0.0.0.0 --port 8000