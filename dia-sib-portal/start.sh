#!/usr/bin/env bash
# DIA SI B Portal - start script
# Run: bash start.sh

set -a
source "$(dirname "$0")/.env"
set +a

exec /c/Users/z00514bu/AppData/Local/Programs/Python/Python314/python.exe \
  -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
