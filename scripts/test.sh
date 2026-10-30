#!/usr/bin/env bash
# scripts/test.sh
# Linux quality gate с видимыми логами; --browser/--live/--docker включают нужные зависимости.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
uv run --frozen ruff check app tests scripts migrations
uv run --frozen ruff format --check app tests scripts migrations
# Playwright уже содержит Node; отдельная установка node на сервер не требуется.
node_binary="$(uv run --frozen python -c 'from playwright._impl._driver import compute_driver_executable; print(compute_driver_executable()[0])')"
"$node_binary" --test tests/frontend/state.test.js
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
if [[ " ${*} " == *' --docker '* ]]; then args+=(--docker); fi
uv run --frozen pytest "${args[@]}"
git diff --check
