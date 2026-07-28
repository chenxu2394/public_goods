#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  printf "[INFO] Created .env from .env.example\n"
fi

exec uv run dotenv run --no-override -- uvicorn app:app --reload
