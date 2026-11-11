#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[GuWen] .venv not found. Run ./setup.sh first." >&2
  exit 1
fi

exec .venv/bin/python run.py
