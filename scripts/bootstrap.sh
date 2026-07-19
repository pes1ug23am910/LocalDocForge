#!/usr/bin/env bash
# LocalDocForge development bootstrap (macOS/Linux)
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || { echo "Python 3.12+ required"; exit 1; }

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python -m pytest tests -q
.venv/bin/ldf doctor
echo
echo "Bootstrap complete. Try: .venv/bin/ldf --help"
