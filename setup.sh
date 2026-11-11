#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[GuWen] Python 3 not found. Install Python 3.11-3.12 first." >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "[GuWen] Done. Run ./start.sh, then open http://127.0.0.1:8770/"
