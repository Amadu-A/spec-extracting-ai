<!-- docs/operations.md -->
# Эксплуатация

## Предпосылки

На сервере должны работать Docker/Compose, сеть `ai-shared`, shared-vlm и существующий RabbitMQ. Нужны GNU timeout и flock (coreutils/util-linux). По разрешению владельца `scripts/up.sh` создаёт отсутствующие app user/vhost `specification` через CLI существующего `shared-rabbitmq-1`. Пароли PostgreSQL/RabbitMQ минимум 12 символов, ключи интеграций минимум 24. Веб-интерфейс не требует пароля. Конфигурация shared stack не редактируется, общие сервисы не перезапускаются.

Settings и Compose имеют одинаковое наслоение `.env.example`, затем `.env`, затем process environment. Команды Compose ниже обязательно используют оба файла. Не выводите полный `docker compose config`: там раскрываются секреты; `config --quiet` достаточен.

## Первое создание .env

Рабочий репозиторий — `Amadu-A/spec-extracting-ai`. Если серверный clone сделан из `neo-term-it`, измените origin и получите рабочую ветку. Команда pull остановится при несовместимой истории или локальных изменениях; не применяйте reset для обхода отказа.

```bash
cd ~/projects/spec-extracting-ai
git remote set-url origin git@github.com:Amadu-A/spec-extracting-ai.git
git pull --ff-only origin main
```

Следующий блок создаёт `.env` один раз, с правами 600 и случайными секретами. При существующем файле он остановится, сохранив пароли. Для первичной настройки нужны ровно три переменные: `DATABASE_PASSWORD`, `RABBITMQ_PASSWORD`, `API_KEYS`. Имена пользователя/vhost, адреса и порты остаются в `.env.example`.

```bash
(
  set +x
  set -euo pipefail
  umask 077
  [[ ! -e .env ]] || { echo '.env уже существует: сохраните его секреты.' >&2; exit 1; }
  database_secret=$(openssl rand -hex 32)
  rabbitmq_secret=$(openssl rand -hex 32)
  onec_key=$(openssl rand -hex 32)
  telegram_key=$(openssl rand -hex 32)
  evaluation_key=$(openssl rand -hex 32)
  set -C
  printf 'DATABASE_PASSWORD=%s\nRABBITMQ_PASSWORD=%s\nAPI_KEYS={"onec":"%s","telegram":"%s","evaluation":"%s"}\n' \
    "$database_secret" "$rabbitmq_secret" "$onec_key" "$telegram_key" "$evaluation_key" > .env
  printf '.env создан; секреты не выведены в терминал.\n'
)
```

Браузер создаёт анонимную изолированную сессию автоматически; пароль и окно входа удалены. При последующих обновлениях `.env` не пересоздаётся. Если пользователь `specification` уже существует, в `RABBITMQ_PASSWORD` должен быть именно его действующий пароль; автоматической смены пароля нет. Не запускайте `broker-config` вручную: его stdout — закрытый протокол передачи секрета родительскому `up.sh`.

Подготовка RabbitMQ использует Settings в одноразовом контейнере приложения, затем `docker exec --user rabbitmq` в уже существующем контейнере. Пароль передаётся через stdin в приватный файл `/dev/shm` с удалением после команды; в argv/логи и файлы других проектов он не попадает. Проверяются теги пользователя, отсутствие прав в чужих vhost и права в собственном vhost. Ошибки чтения/аутентификации не считаются отсутствием записи. CLI и Docker-клиент ограничены timeout; flock запрещает два одновременных запуска.

## Очистка существующего .env

Если `.env` уже создан, повторная генерация исходным блоком правильно остановится. Для удаления лишних полей и дополнения недостающих секретов используйте существующий операторский CLI:

```bash
uv run --frozen python -m app.operations normalize-env
```

Команда оставляет `DATABASE_PASSWORD`, `RABBITMQ_PASSWORD`, `API_KEYS` и удаляет устаревший `UI_PASSWORD`. Действующие пароли и API-ключи сохраняются вместе с исходными кавычками и экранированием для Compose; только отсутствующие/пустые поля заполняются случайными значениями. Исходный файл сохраняется в `data/env-backups/`, новый получает права 600 и заменяется атомарно после проверки через Pydantic. При неверном синтаксисе/JSON или слишком коротком действующем секрете операция завершится отказом без смены пароля. Значения не печатаются. Повторная очистка не создаёт новые ключи. После обновления выполните её перед запуском проверок на хосте: Settings больше не принимает удалённое поле UI_PASSWORD.

В показанном первоначальном `.env` поля `RABBITMQ_USER=spec-extraction` и `RABBITMQ_VHOST=spec-extraction` были несекретными overrides. После очистки действуют `RABBITMQ_USER=specification` и `RABBITMQ_VHOST=specification` из `.env.example`. Автоподготовка намеренно отклоняет другие имена и очередь, отличную от `specification.extractions`. В предоставленном неудачном запуске сборка остановилась до подготовки RabbitMQ.

## Ошибки инструментов и загрузки образов

`scripts/test.sh` и Windows quality gate используют Node из установленного Playwright. `uv sync --frozen` устанавливает его вместе с dev-зависимостями; установка системного `node` не нужна. Linux `--browser` дополнительно устанавливает Chromium и его системные библиотеки.

Ошибка `failed to fetch anonymous token` / `TLS handshake timeout` на `auth.docker.io` означает отказ сетевого обращения к registry, а не ошибку Python-кода. Dockerfile приложения использует официальный образ `ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim` с закреплённым digest. Он содержит Python 3.12 на Debian 13; его загрузка не требует Docker Hub. Для PostgreSQL сначала используется локальный `postgres:16-alpine`. Если его нет, выполняется pull из Docker Hub с общим пределом 180 секунд; при отказе — pull `mirror.gcr.io/library/postgres:16-alpine` с таким же пределом и локальное присвоение тега `postgres:16-alpine`. Ошибка обоих источников останавливает запуск до изменений RabbitMQ. Конфигурация Compose, общего Docker daemon, proxy и mirrors не меняется.

После pull повторите `bash scripts/up.sh`: Docker использует уже скачанные слои. Если недоступен сам GHCR, сохраните текст ошибки сетевого шага; изменение секретов `.env` такую ошибку не исправляет.

## Права файлов образа и регрессия Docker

`COPY` сохраняет права файлов Linux build context. После `umask 077` новые Python-файлы могут получить 600, каталоги — 700; скопированные с владельцем root, они недоступны runtime-пользователю `app` (UID/GID 10001). Dockerfile явно задаёт исходникам, prompt и миграциям 644/755, корню `/app` — 755. Владельцем кода остаётся root; `/app/data` принадлежит app с правами 700. Установка зависимостей выполняется с umask 022. USER, read_only, cap_drop и no-new-privileges сохраняются.

`up.sh` до bootstrap исполняет import/config под штатным USER. Отдельный pytest `tests/test_docker_image.py` с `--docker` собирает реальный Dockerfile из tar-контекста с 600/700, проверяет чтение всех модулей и ресурсов, UID 10001 и `python -m app.operations config` с read-only, network=none, cap_drop=ALL. Используются искусственные секреты; `.env` и документы в контекст не входят. Без флага тест пропускается явно; с флагом недоступный Docker означает ошибку. CI запускает его автоматически.

```powershell
.\ops\check-quality.ps1 -Browser -Docker
uv run --frozen pytest tests/test_docker_image.py --docker -s
```

```bash
bash scripts/test.sh --browser --docker
```

## Обновление и тесты

```bash
cd ~/projects/spec-extracting-ai
git pull --ff-only origin main
bash scripts/test.sh --browser
bash scripts/up.sh
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs -f --tail=100 api worker
```

Если репозиторий расположен иначе, используйте его фактический путь. `up.sh` включает пересборку, миграции и реальные проверки зависимостей/vision. Нет необходимости в down или удалении volumes.

Проверка действующего соединения и повторного bootstrap без ручного ввода паролей:

```bash
docker compose --env-file .env.example --env-file .env exec -T api python -m app.operations broker-check --require-consumer
curl --fail --show-error http://127.0.0.1:8083/api/v1/health/ready
bash scripts/up.sh
```

На повторном запуске ожидается `RabbitMQ bootstrap: user=specification vhost=specification mutations=false`. Итог AMQP: `user=specification, vhost=specification, queue=specification.extractions, durable=OK, consumers=N`, где N больше нуля. Диагностика использует настоящий адаптер приложения: соединение аутентифицируется указанным паролем, совместимый durable declare проверяет свойства очереди, passive declare показывает consumers. За 15 секунд worker должен подписаться; отсутствие подписки даёт ненулевой exit code. Сообщения не публикуются и не извлекаются. После этого проверяются фактические задания PDF через UI/API.

## Доступ по адресу сервера

В каталоге `.env.example` установлены `APP_BIND=0.0.0.0` и `APP_PORT=8083`. После успешного `bash scripts/up.sh` UI открывается с другого компьютера по `http://<IP_сервера>:8083`, например `http://192.168.55.3:8083`; документация API — `/docs`. `0.0.0.0` означает публикацию на IPv4-интерфейсах, а не адрес для браузера. При необходимости привязки к одному интерфейсу укажите его IP в `APP_BIND` и повторите `up.sh`.

На сервере проверьте публикацию и HTTP:

```bash
docker compose --env-file .env.example --env-file .env port api 8083
curl --fail --show-error http://127.0.0.1:8083/api/v1/health/live
curl --fail --show-error http://127.0.0.1:8083/api/v1/health/ready
```

Для стандартной конфигурации первая команда должна показать `0.0.0.0:8083`. Liveness подтверждает работу API; readiness дополнительно проверяет PostgreSQL, RabbitMQ и VLM. Отсутствующие app user/vhost создаются до preflight; неверный пароль, служебные теги или права в чужом vhost останавливают запуск с явным сообщением.

С Windows-клиента для сервера `192.168.55.3`:

```powershell
Test-NetConnection 192.168.55.3 -Port 8083
```

После `TcpTestSucceeded: True` откройте `http://192.168.55.3:8083`: загрузка PDF доступна сразу, без авторизации. Если HTTP работает на самом сервере, а TCP с клиента недоступен, проверьте фактический IP, маршрутизацию и правило доступа TCP 8083 в используемом firewall. Изменение `APP_BIND` публикует порт Docker; оно не настраивает firewall, NAT или публичный DNS.

## Миграции и live-проверки

Локальная миграция отдельно:

```bash
docker compose --env-file .env.example --env-file .env run --rm --no-deps migrate
```

Live-проверки запускаются из окружения с доступом к endpoints. PostgreSQL проверяется в собственной случайной schema, которая удаляется после успешного теста; нужны права CREATE SCHEMA в собственной БД приложения. Broker test использует отдельную краткоживущую очередь того же app-vhost и не читает production queue. Отсутствующая dependency не подменяется double и означает failure при явном флаге.

```bash
bash scripts/test.sh --browser --live
```

## Ручная приёмка

1. Открыть UI: поля загрузки доступны сразу, формы входа нет. Загрузить каждый из четырёх PDF. Проверить настоящее изображение, количество страниц и шифр; для АТМД имя файла отличается от штампа. Для ЭОМ полный шифр подтвердить вручную.
2. Ввести `1-2`, запустить. Слева должны открываться листы, реально передаваемые VLM, справа — raw tokens. Метрики берутся из committed pages/items/warnings. Полный JSON доступен отдельным режимом в меню `…`.
3. На обработке листа 1 вручную открыть лист 3. Токены не должны вернуть просмотр к 1. Когда VLM начнёт лист 2, viewer должен открыть 2. Повторить ручной переход; refresh сохраняет просмотр и подхватывает job без нового запуска.
4. Проверить zoom/fit/pan/fullscreen, миниатюры/стрелки/номер листа, сворачивание журнала и копирование JSON. В журнале — реальные timestamps/durations, не секундомер прогресса.
5. Изменить prompt, сохранить/открыть повторно. «Восстановить» сразу меняет действующий server prompt. «Отмена»/Esc/× не сохраняют правки; при dirty close появляется подтверждение. Уже running job использует свой прежний snapshot.
6. Скачать JSON и Excel: одинаковые пять колонок и значения, физические Pages с 1. Открыть предупреждения и перейти по ним к оригиналу. Уточнить непроверенные строки; статус completed_with_warnings не означает полную ручную проверку.
7. Отменить длинный job: состояние cancelled, полный экспорт заблокирован, partial доступен в diagnostics. После завершения повторить выбранный лист: зависимые результаты должны замениться, дубликаты не появиться.
8. Закрыть UI и проверить завершение через API. Открыть `/docs`, авторизовать отдельный ключ и повторить create/status/events/result. Другой API-key не должен видеть job.
9. На тестовом job перезапустить только собственный worker (`docker compose ... restart worker`): обработка возобновляется с checkpoint. Shared сервисы не перезапускать.

## Хранение и backup

По умолчанию metadata/PDF/events доступны час после завершения задания, включая failed/cancelled. Загруженный PDF без задания живёт час. Для queued/running резервируется JOB_TIMEOUT_SECONDS + RETENTION_SECONDS, повторный анализ получает новый бюджет; обработка ограничена JOB_TIMEOUT_SECONDS=3600. PDF с несколькими заданиями хранится до последнего живого результата. API закрывает доступ по TTL при каждом обращении; отдельный цикл worker каждые CLEANUP_INTERVAL_SECONDS=15 удаляет события, jobs, sessions и originals. Случайная HttpOnly/SameSite=Strict cookie и серверная сессия имеют фиксированный срок JOB_TIMEOUT_SECONDS + RETENTION_SECONDS + 3600 (3 часа по умолчанию); чтение его не продлевает. После истечения UI автоматически создаёт новую сессию и предлагает загрузить PDF повторно, без модального окна. Прежние записи до обновления сохраняют уже записанные сроки; новые сроки применяются к новым/повторным заданиям и вновь созданным сессиям. Runtime logs ограничены Docker driver. Для backup используйте pg_dump собственной базы и согласованный snapshot project volume `documents`; для восстановления нужны оба. Не удаляйте volumes общей инфраструктуры.

Rollback кода: вернуть выбранную версию файлов и пересобрать только этот проект. При миграциях с удалением данных сначала сделать backup; автоматического downgrade с удалением данных скрипты не выполняют.
