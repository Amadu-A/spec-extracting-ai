#!/usr/bin/env bash
# scripts/test.sh
# Linux quality gate с видимыми логами; --browser/--live включают внешние зависимости.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
uv run --frozen ruff check app tests scripts migrations
uv run --frozen ruff format --check app tests scripts migrations
node --test tests/frontend/state.test.js
marker='not live and not browser'
if [[ " ${*} " == *' --browser '* ]]; then
  uv run --frozen playwright install --with-deps chromium
  marker='not live'
fi
if [[ " ${*} " == *' --live '* ]]; then
  if [[ " ${*} " == *' --browser '* ]]; then marker=''; else marker='not browser'; fi
fi
args=(--cov=app --cov-report=term-missing --cov-report=json:reports/coverage.json)
if [[ " ${*} " == *' --live '* ]]; then args+=(--postgres --broker --vlm); fi
[[ -z "$marker" ]] || args+=(-m "$marker")
uv run --frozen pytest "${args[@]}"
git diff --check
