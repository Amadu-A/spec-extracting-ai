#!/usr/bin/env bash
# scripts/up.sh
# Одна команда: своё приложение и выделенная учётная запись существующего RabbitMQ.
set +x
set -Eeuo pipefail
cd "$(dirname "$0")/.."
current_stage='проверка инструментов'
finish() {
  # Сохраняет код отказа и удаляет секретные значения из памяти родительского shell.
  local code=$?
  unset broker_config broker_values broker_password
  if [[ $code -ne 0 ]]; then echo "Ошибка: $current_stage (код $code)." >&2; fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
stage() {
  # Логи видны; секретный протокол Settings никогда не печатается.
  current_stage=$1
  printf '[%s; +%ss] %s\n' "$(date +%T)" "$SECONDS" "$current_stage"
}
for tool in docker timeout flock; do
  command -v "$tool" >/dev/null || { echo "Не найдена команда $tool (нужны Docker, coreutils и util-linux)." >&2; exit 1; }
done
mkdir -p data
exec 9>data/.deploy.lock
flock -n 9 || { echo 'Уже выполняется другой scripts/up.sh.' >&2; exit 1; }
[[ -f .env ]] || { echo 'Создайте .env с секретами по README.md'; exit 1; }
dc=(docker compose --env-file .env.example --env-file .env)
stage 'Проверка Docker, Compose и существующей сети ai-shared'
docker info >/dev/null
"${dc[@]}" config --quiet
docker network inspect ai-shared >/dev/null
stage 'Сборка приложения'
"${dc[@]}" build api worker
stage 'Проверка настроек через Pydantic'
if ! broker_config="$("${dc[@]}" run --rm --no-deps -T api python -m app.operations broker-config)"; then
  echo 'Подготовка RabbitMQ остановлена: проверьте настройки и секреты.' >&2
  exit 1
fi
mapfile -t broker_values <<< "$broker_config"
if [[ ${#broker_values[@]} -ne 5 || ! ${broker_values[4]} =~ ^[0-9]+$ ]]; then
  echo 'Неверный ответ процесса настроек RabbitMQ.' >&2
  exit 1
fi
broker_container=${broker_values[0]}
broker_user=${broker_values[1]}
broker_vhost=${broker_values[2]}
broker_password=${broker_values[3]}
broker_timeout=${broker_values[4]}
bounded_docker() {
  # Ограничивает и RPC, и зависание самого Docker-клиента.
  timeout --signal=TERM --kill-after=5s "$((broker_timeout + 15))s" bash -c 'docker "$@"' -- "$@"
}
stage 'Проверка существующего контейнера RabbitMQ'
if [[ "$(bounded_docker inspect --format '{{.State.Running}}' "$broker_container")" != true ]]; then
  echo 'Контейнер RabbitMQ не запущен. Проверьте существующий shared-infrastructure.' >&2
  exit 1
fi
members="$(bounded_docker network inspect ai-shared --format '{{range .Containers}}{{println .Name}}{{end}}')"
if ! awk -v name="$broker_container" '$0 == name { found=1 } END { exit !found }' <<< "$members"; then
  echo 'RabbitMQ не подключён к сети ai-shared.' >&2
  exit 1
fi
rabbitmq_ctl() {
  # CLI использует cookie существующего узла; пароль администратора не нужен.
  bounded_docker exec --user rabbitmq "$broker_container" timeout --signal=TERM --kill-after=3s "${broker_timeout}s" rabbitmqctl --quiet --timeout "$broker_timeout" "$@"
}
rabbitmq_secret() {
  # Пароль идёт через stdin в приватный файл RAM; argv содержит только имена/пути.
  local expression
  case "$1" in
    add_user) expression='begin case file:read_file(_2) of {ok, P} -> case catch rabbit_auth_backend_internal:add_user(_1, P, <<"specification-bootstrap">>) of ok -> ok; _ -> throw(user_creation_failed) end; _ -> throw(secret_read_failed) end end.' ;;
    authenticate_user) expression='begin case file:read_file(_2) of {ok, P} -> case catch rabbit_access_control:check_user_pass_login(_1, P) of {ok, _} -> ok; _ -> throw(authentication_failed) end; _ -> throw(secret_read_failed) end end.' ;;
    *) return 2 ;;
  esac
  printf '%s' "$broker_password" | bounded_docker exec -i --user rabbitmq "$broker_container" \
    timeout --signal=TERM --kill-after=3s "$((broker_timeout + 8))s" sh -c '
      set -eu
      umask 077
      secret_file=$(mktemp /dev/shm/specification-password.XXXXXX)
      trap '\''rm -f "$secret_file"'\'' EXIT
      trap '\''exit 143'\'' TERM
      trap '\''exit 130'\'' INT
      cat > "$secret_file"
      timeout --signal=TERM --kill-after=3s "${1}s" rabbitmqctl --quiet --timeout "$1" eval "$2" "$3" "$secret_file"
    ' -- "$broker_timeout" "$expression" "$broker_user"
}
stage "Проверка пользователя RabbitMQ (лимит CLI: ${broker_timeout}с)"
users="$(rabbitmq_ctl list_users --no-table-headers)"
user_record="$(awk -v name="$broker_user" '$1 == name { print; exit }' <<< "$users")"
if [[ -n "$user_record" ]]; then
  if ! awk '$2 == "[]" { valid=1 } END { exit !valid }' <<< "$user_record"; then
    echo 'Выбранный пользователь имеет служебные теги. Нужна отдельная учётная запись приложения.' >&2
    exit 1
  fi
else
  stage 'Создание пользователя приложения RabbitMQ'
  rabbitmq_secret add_user
fi
stage 'Проверка пароля RabbitMQ'
if rabbitmq_secret authenticate_user; then :; else
  code=$?
  if [[ $code == 124 || $code == 137 ]]; then
    echo 'Превышено время ожидания RabbitMQ; CLI ограничен timeout.' >&2
  else
    echo 'Пароль RabbitMQ не подтверждён. Проверьте RABBITMQ_PASSWORD в .env; существующий пароль не меняется.' >&2
  fi
  exit "$code"
fi
stage 'Проверка выделенного vhost и прав RabbitMQ'
user_permissions="$(rabbitmq_ctl list_user_permissions "$broker_user" --no-table-headers)"
if ! awk -v name="$broker_vhost" 'NF && $1 != name { foreign=1 } END { exit foreign }' <<< "$user_permissions"; then
  echo 'У пользователя есть права в других vhost. Нужна отдельная учётная запись приложения.' >&2
  exit 1
fi
vhosts="$(rabbitmq_ctl list_vhosts name --no-table-headers)"
if ! awk -v name="$broker_vhost" '$0 == name { found=1 } END { exit !found }' <<< "$vhosts"; then
  stage 'Создание выделенного vhost RabbitMQ'
  rabbitmq_ctl add_vhost "$broker_vhost"
fi
permissions="$(rabbitmq_ctl list_permissions -p "$broker_vhost" --no-table-headers)"
if ! awk -v name="$broker_user" '$1 == name && $2 == ".*" && $3 == ".*" && $4 == ".*" { valid=1 } END { exit !valid }' <<< "$permissions"; then
  stage 'Настройка прав внутри выделенного vhost'
  rabbitmq_ctl set_permissions -p "$broker_vhost" "$broker_user" '.*' '.*' '.*'
fi
unset broker_config broker_values broker_password
stage 'Запуск собственной PostgreSQL и миграций'
"${dc[@]}" up -d --wait postgres
"${dc[@]}" run --rm --no-deps migrate
stage 'Проверка настоящих app-vhost, vision и JSON schema'
"${dc[@]}" run --rm --no-deps api python -m app.operations vision
stage 'Запуск API и worker'
"${dc[@]}" up -d --wait --wait-timeout 180 api worker
"${dc[@]}" ps
"${dc[@]}" logs --tail=40 api worker
echo 'Запуск завершён. Проверьте UI и /docs на настроенном APP_BIND:APP_PORT.'
