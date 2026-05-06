#!/bin/sh
set -e
# Railway/Render set PORT; default for local Docker runs.
export PORT="${PORT:-8000}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
