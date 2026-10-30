<!-- docs/operations.md -->
# Эксплуатация

## Предпосылки

На сервере должны работать Docker/Compose, сеть `ai-shared`, shared-vlm и существующий RabbitMQ. Нужны GNU timeout и flock (coreutils/util-linux). По разрешению владельца `scripts/up.sh` создаёт отсутствующие app user/vhost `specification` через CLI существующего `shared-rabbitmq-1`. Пароли PostgreSQL/UI/RabbitMQ минимум 12 символов, ключи интеграций минимум 24. Конфигурация shared stack не редактируется, общие сервисы не перезапускаются.

Settings и Compose имеют одинаковое наслоение `.env.example`, затем `.env`, затем process environment. Команды Compose ниже обязательно используют оба файла. Не выводите полный `docker compose config`: там раскрываются секреты; `config --quiet` достаточен.

## Первое создание .env

Рабочий репозиторий — `Amadu-A/spec-extracting-ai`. Если серверный clone сделан из `neo-term-it`, измените origin и получите рабочую ветку. Команда pull остановится при несовместимой истории или локальных изменениях; не применяйте reset для обхода отказа.

```bash
cd ~/projects/spec-extracting-ai
git remote set-url origin git@github.com:Amadu-A/spec-extracting-ai.git
git pull --ff-only origin main
```

Следующий блок создаёт `.env` один раз, с правами 600 и случайными секретами. При существующем файле он остановится, сохранив пароли. Для первичной настройки нужны ровно четыре переменные: `DATABASE_PASSWORD`, `RABBITMQ_PASSWORD`, `UI_PASSWORD`, `API_KEYS`. Имена пользователя/vhost, адреса и порты остаются в `.env.example`.

```bash
(
  set +x
  set -euo pipefail
  umask 077
  [[ ! -e .env ]] || { echo '.env уже существует: сохраните его секреты.' >&2; exit 1; }
  database_secret=$(openssl rand -hex 32)
  rabbitmq_secret=$(openssl rand -hex 32)
  ui_secret=$(openssl rand -hex 32)
  onec_key=$(openssl rand -hex 32)
  telegram_key=$(openssl rand -hex 32)
  evaluation_key=$(openssl rand -hex 32)
  set -C
  printf 'DATABASE_PASSWORD=%s\nRABBITMQ_PASSWORD=%s\nUI_PASSWORD=%s\nAPI_KEYS={"onec":"%s","telegram":"%s","evaluation":"%s"}\n' \
    "$database_secret" "$rabbitmq_secret" "$ui_secret" "$onec_key" "$telegram_key" "$evaluation_key" > .env
  printf 'Пароль входа в Web UI: %s\n' "$ui_secret"
)
```

Пароль UI появится только в вашем терминале. При последующих обновлениях `.env` не пересоздаётся. Если пользователь `specification` уже существует, в `RABBITMQ_PASSWORD` должен быть именно его действующий пароль; автоматической смены пароля нет.

Подготовка RabbitMQ использует Settings в одноразовом контейнере приложения, затем `docker exec --user rabbitmq` в уже существующем контейнере. Пароль передаётся через stdin в приватный файл `/dev/shm` с удалением после команды; в argv/логи и файлы других проектов он не попадает. Проверяются теги пользователя, отсутствие прав в чужих vhost и права в собственном vhost. Ошибки чтения/аутентификации не считаются отсутствием записи. CLI и Docker-клиент ограничены timeout; flock запрещает два одновременных запуска.

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

После `TcpTestSucceeded: True` откройте `http://192.168.55.3:8083` и войдите с `UI_PASSWORD`. Если HTTP работает на самом сервере, а TCP с клиента недоступен, проверьте фактический IP, маршрутизацию и правило доступа TCP 8083 в используемом firewall. Изменение `APP_BIND` публикует порт Docker; оно не настраивает firewall, NAT или публичный DNS.

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

1. Открыть UI, войти с UI_PASSWORD. Загрузить каждый из четырёх PDF. Проверить настоящее изображение, количество страниц и шифр; для АТМД имя файла отличается от штампа. Для ЭОМ полный шифр подтвердить вручную.
2. Ввести `1-2`, запустить. Слева должны открываться листы, реально передаваемые VLM, справа — raw tokens. Метрики берутся из committed pages/items/warnings. Полный JSON доступен отдельным режимом в меню `…`.
3. На обработке листа 1 вручную открыть лист 3. Токены не должны вернуть просмотр к 1. Когда VLM начнёт лист 2, viewer должен открыть 2. Повторить ручной переход; refresh сохраняет просмотр и подхватывает job без нового запуска.
4. Проверить zoom/fit/pan/fullscreen, миниатюры/стрелки/номер листа, сворачивание журнала и копирование JSON. В журнале — реальные timestamps/durations, не секундомер прогресса.
5. Изменить prompt, сохранить/открыть повторно. «Восстановить» сразу меняет действующий server prompt. «Отмена»/Esc/× не сохраняют правки; при dirty close появляется подтверждение. Уже running job использует свой прежний snapshot.
6. Скачать JSON и Excel: одинаковые пять колонок и значения, физические Pages с 1. Открыть предупреждения и перейти по ним к оригиналу. Уточнить непроверенные строки; статус completed_with_warnings не означает полную ручную проверку.
7. Отменить длинный job: состояние cancelled, полный экспорт заблокирован, partial доступен в diagnostics. После завершения повторить выбранный лист: зависимые результаты должны замениться, дубликаты не появиться.
8. Закрыть UI и проверить завершение через API. Открыть `/docs`, авторизовать отдельный ключ и повторить create/status/events/result. Другой API-key не должен видеть job.
9. На тестовом job перезапустить только собственный worker (`docker compose ... restart worker`): обработка возобновляется с checkpoint. Shared сервисы не перезапускать.

## Хранение и backup

По умолчанию metadata/PDF/events доступны 24 часа; TTL возвращается status. Worker удаляет истёкшие события, jobs, sessions и originals. Runtime logs ограничены Docker driver. Для backup используйте pg_dump собственной базы и согласованный snapshot project volume `documents`; для восстановления нужны оба. Не удаляйте volumes общей инфраструктуры.

Rollback кода: вернуть выбранную версию файлов и пересобрать только этот проект. При миграциях с удалением данных сначала сделать backup; автоматического downgrade с удалением данных скрипты не выполняют.
