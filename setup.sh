#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[GuWen] Python 3 not found. Install Python 3.11-3.12 first." >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)'; then
  echo "[GuWen] Python 3.11 or 3.12 is required." >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "[GuWen] Done. Run ./start.sh, then open http://127.0.0.1:8770/"
