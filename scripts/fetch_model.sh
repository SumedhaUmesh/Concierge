#!/usr/bin/env bash
# Download LFM2.5-1.2B-Instruct Q4_K_M GGUF from Hugging Face.
# Requires: huggingface-cli (installed in the venv).
set -euo pipefail

REPO="LiquidAI/LFM2.5-1.2B-Instruct-GGUF"
# Try to find the right filename; fall back to a known pattern.
FILE="LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
MODELS_DIR="$(cd "$(dirname "$0")/.." && pwd)/models"

cd "$(dirname "$0")/.."
PYTHON="$(pwd)/backend/.venv/bin/python3.13"

echo "Downloading $FILE from $REPO into models/…"
mkdir -p "$MODELS_DIR"

"$PYTHON" - <<EOF
from huggingface_hub import hf_hub_download
import shutil, os

path = hf_hub_download(
    repo_id="$REPO",
    filename="$FILE",
    local_dir="$MODELS_DIR",
    local_dir_use_symlinks=False,
)
print(f"Saved to: {path}")
EOF

echo "Done — model saved to models/$FILE"
