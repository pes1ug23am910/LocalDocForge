#!/usr/bin/env bash
# LocalDocForge development/profile bootstrap (macOS/Linux)
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE=${1:-dev}
case "$PROFILE" in
    lite|standard|full|dev) ;;
    *) echo "usage: $0 [lite|standard|full|dev]" >&2; exit 2 ;;
esac

PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info < (3, 15) else 1)' \
    || { echo "CPython 3.12, 3.13, or 3.14 is required"; exit 1; }

if [ "$PROFILE" = dev ]; then VENV=.venv; else VENV=".venv-$PROFILE"; fi
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
VENV_PYTHON="$VENV/bin/python"
"$VENV_PYTHON" -m pip install --require-hashes -r "requirements/locks/$PROFILE.txt"
"$VENV_PYTHON" -m pip install -e ".[${PROFILE}]" --no-deps
if [ "$PROFILE" = dev ]; then
    "$VENV_PYTHON" -m pytest tests -q
    "$VENV_PYTHON" -m ruff check src tests scripts
    "$VENV_PYTHON" -m mypy
else
    "$VENV_PYTHON" scripts/profile_smoke.py --profile "$PROFILE"
fi
"$VENV/bin/ldf" --json doctor
echo
echo "Bootstrap complete. Try: $VENV/bin/ldf --help"
