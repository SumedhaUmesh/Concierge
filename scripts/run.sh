#!/usr/bin/env bash
# Start the Concierge server with hot-reload.
set -euo pipefail

cd "$(dirname "$0")/.."
source backend/.venv/bin/activate

echo "Starting Concierge at http://localhost:8000"
echo "Model directory: $(pwd)/models"
ls models/*.gguf 2>/dev/null && echo "Model found." || echo "No model yet — run scripts/fetch_model.sh"
echo ""

cd backend
python3.13 -m uvicorn server:app --reload --host 0.0.0.0 --port 8000
