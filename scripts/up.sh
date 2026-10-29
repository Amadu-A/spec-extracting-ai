#!/usr/bin/env bash
# scripts/up.sh
# Одна команда развёртывания только проекта: build, migrations, dependency/vision smoke, health.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo 'Создайте .env с секретами по README.md'; exit 1; }
dc=(docker compose --env-file .env.example --env-file .env)
"${dc[@]}" config --quiet
docker network inspect ai-shared >/dev/null
"${dc[@]}" build api worker
"${dc[@]}" up -d --wait postgres
"${dc[@]}" run --rm --no-deps migrate
"${dc[@]}" run --rm --no-deps api python -m app.operations vision
"${dc[@]}" up -d --wait --wait-timeout 180 api worker
"${dc[@]}" ps
"${dc[@]}" logs --tail=40 api worker
echo 'Запуск завершён. Проверьте UI и /docs на настроенном APP_BIND:APP_PORT.'
