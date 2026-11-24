<!-- docs/STAGE_03A_JOB_DELIVERY.md -->
# Stage 03A. Доставка и восстановление заданий 8083

Миграция Alembic `0002` добавляет транспортные поля в `extraction_jobs`. Создание задания, `job_queued` и pending-доставка фиксируются вместе. Ручной retry повышает `revision`, очищает время начала и создаёт pending в той же транзакции. Лимит активных заданий применяется к созданию и retry под одним PostgreSQL advisory lock. Хранилище, PDF, события и снимки старых заданий сохраняются.

| Состояние доставки | Значение |
|---|---|
| `legacy_unknown` | В старой схеме не было подтверждений. Queued исключён из фоновой публикации до получения старого UUID или явного восстановления. |
| `pending` | Публикация ожидает срока `delivery_due`. |
| `claimed` | Одну строку зарезервировал publisher с `FOR UPDATE SKIP LOCKED`. Резерв действует 30 секунд. |
| `published` | Получен RabbitMQ ACK публикации. Фоновый dispatcher пропускает эту строку. |
| `received` | Consumer захватил lease задания. При его истечении возможна одна подтверждённая recovery-публикация на потерянный lease. |
| `closed` | Терминальное задание. Публикация прекращена. |

Публикуется persistent текстовый UUID с целочисленным AMQP-заголовком `revision`. Заголовок проверяется под блокировкой задания вместе с lease. Сообщения только с UUID принимаются для legacy заданий, пока не был запрошен ручной retry в новой версии. Старые ревизии, терминальные и неизвестные UUID, а также дубли при живом lease безопасно ACK. При ранней redelivery погибшего worker запись `received` остаётся в БД. Истечение lease даёт новый резерв, даже если ранняя redelivery уже ACK. После захвата восстанавливаются только неподтверждённые страницы. Погибший или отставший worker теряет право продлить просроченный lease или зафиксировать checkpoint.

Одна итерация резервирует по одной записи и пытается опубликовать до четырёх. Сетевой вызов ограничен восемью секундами. Ошибка маршрута, overflow, разрыв, timeout и отсутствие положительного confirm оставляют повтор. Задержка растёт от 2 секунд с jitter до предела 60 секунд. Бизнес-бюджет задания ограничивает суммарное время повторов. При исчерпании задание завершается с `job_timeout`, либо `cancelled` при уже принятой отмене. Приложение сохраняет at-least-once. Потеря confirm или падение до его записи в PostgreSQL допускает повторное сообщение. `message_id` служит корреляции и не включает дедупликацию RabbitMQ. Две стороны используют существующие ограничения очереди и prefetch.

## Переход, контроль и rollback

1. Владелец приостанавливает новые отправки 8083 и связанные отправки Mapper на окно обновления. Дождитесь завершения активных работ либо согласуйте их восстановление после остановки worker. Запишите число уникальных заданий вместе с TTL и lease и отдельно считайте сообщения только своего vhost.
2. Выполните штатные тесты после `git pull --ff-only`. В shell ниже `dc` — существующий Compose проекта. Перед заменой образа зафиксируйте текущий image ID контейнера API под локальным тегом rollback и сделайте закрытую резервную копию собственной базы. Для полного восстановления дополнительно нужен согласованный snapshot project volume `documents`.

```bash
dc() { docker compose --env-file .env.example --env-file .env "$@"; }
dc ps
docker exec shared-rabbitmq-1 rabbitmqctl list_queues -p specification name messages_ready messages_unacknowledged consumers
dc exec -T postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT state, count(*) FROM extraction_jobs
WHERE state IN ('queued', 'running') AND expires_at > now() GROUP BY state;
SELECT id, COALESCE(payload->>'job_type', 'specification') AS profile, state, lease_until, expires_at
FROM extraction_jobs WHERE state IN ('queued', 'running') AND expires_at > now();
SQL
old_api=$(dc ps -q api)
test -n "$old_api" && docker tag "$(docker inspect --format '{{.Image}}' "$old_api")" spec-extracting-ai:before-stage03a
```

3. Остановите только собственные API и worker после паузы отправок. Примените миграцию через `up.sh`, который сначала собирает образ, затем выполняет `alembic upgrade head` и запускает API/worker. Старая очередь остаётся на месте. Если стоп или backup не удался, устраните проблему до продолжения.

```bash
dc stop api worker && (
  umask 077
  mkdir -p data/backups &&
  dc exec -T postgres sh -c 'exec pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    > "data/backups/stage03a-$(date -u +%Y%m%dT%H%M%SZ).dump"
) && bash scripts/up.sh
```

4. После health и подписки worker наблюдайте drain старой очереди. Старые копии постепенно ACK. Сопоставляйте Ready/Unacked с PostgreSQL в течение нескольких циклов, учитывая скорость VLM. Команда `delivery-status` выдаёт только UUID, профиль, ревизию, состояние, lease/deadline/TTL и транспортные счётчики. В журнале ищите `delivery_attempt`, `delivery_confirm`, `delivery_retry`, `delivery_ack`, `delivery_ack_noop`, `delivery_processing_retry` и `broker_consumer_subscribed`. Признаки `recovering` и `redelivered` различают recovery и redelivery.

```bash
dc exec -T api python -m app.operations broker-check --require-consumer
dc exec -T api python -m app.operations delivery-status
docker exec shared-rabbitmq-1 rabbitmqctl list_queues -p specification name messages_ready messages_unacknowledged consumers
dc logs --since 15m worker
```

5. Если после drain и подтверждённой работы consumer остались актуальные `legacy_unknown` или есть подтверждённая оператором потеря `published` сообщений, выберите UUID из отчёта. Команда принимает до десяти `--job-id`, ставит только подходящие записи в pending и показывает `scheduled`/`unchanged`. Разрешены queued и running с истёкшим lease, без принятой отмены и с оставшимся бюджетом. Для `published` выдерживается пять минут с confirm. После вызова дождитесь результата, прежде чем решать, нужны ли следующие UUID. Команда сознательно не выдаёт отсутствие записи в RabbitMQ за установленный факт. Пакетное восстановление всей очереди и очистка RabbitMQ здесь не применяются.

```bash
dc exec -T api python -m app.operations delivery-reconcile --job-id '<UUID_проверенного_задания>'
```

6. Проверьте HTTP status, snapshot/reconnect, отмену и полный экспорт обоих профилей. Сохраните измерения отдельно от результатов QA. Возобновление нагрузки Mapper требует приёмки Stage 03B либо согласованного владельцем ограниченного режима.

Для отката остановите только `api worker`, верните сохранённый образ как `spec-extracting-ai:local` и запустите `dc up -d --no-build api worker`. Аддитивную миграцию можно оставить, формат JSON снимков совместим. Подтверждённые сообщения имеют прежнее тело UUID. В старом образе возвращается прежняя периодическая публикация и отсутствует проверка заголовка ревизии, поэтому контролируйте backlog и ограничьте окно отката. Перед повторным переходом инвентаризируйте ожидающие записи ещё раз. Для задач, изменённых старым worker или созданных в старом API в этом окне, `delivery-status` может показывать более раннее транспортное состояние. После drain восстанавливайте выбранные потерянные UUID по описанной процедуре. Не запускайте старый и новый dispatcher одновременно.

## Контракт для Stage 03B

Базовые пути — `/api/v1/extractions` и `/api/v1/invoice-extractions`. На обоих путях `GET /{job_id}` и `POST /{job_id}/cancel` возвращают один и тот же DTO соответствующего профиля. `GET /{job_id}/snapshot`, `/events`, `/result.json` работают с тем же owner. Контракт доступен в `/openapi.json` и Swagger `/docs`. Mapper использует собственный integration key через `X-API-Key` или Bearer. Веб-сессия использует cookie и CSRF для мутаций. Чужой или неизвестный UUID даёт 404, истёкшая ещё сохранённая запись — 410, после cleanup — 404. Неготовый финал возвращает 409 `result_not_ready`. Неверный SSE cursor даёт 409 `invalid_event_cursor`. Конфликт ключа создания возвращает 409 `idempotency_conflict`, переполнение admission — 429 `queue_full`.

| Поля | Тип и смысл |
|---|---|
| `job_id`, `status` | UUID-строка. Возможные статусы `queued`, `running`, `completed`, `completed_with_warnings`, `failed`, `cancelled`. Последние четыре терминальные. |
| `revision`, `last_event_id` | Целые числа. `revision` начинается с 0 и растёт при ручном retry. Cursor монотонен на всём задании, в том числе через retry. |
| `created_at`, `updated_at`, `expires_at`, `started_at` | ISO 8601. `started_at` — первый фактический захват lease текущей ревизии, сохраняется при recovery. До захвата и для исторической записи без сохранённого времени возможен `null`. |
| `cancel_requested`, `error_code` | Булев флаг и строка или `null`. После running cancel может остаться `running` с флагом `true` до остановки; queued сразу `cancelled`. Повтор запроса безопасен. HTTP-вызов VLM мог уже начаться на удалённой стороне. |
| `current_pdf_page`, `processed_pages`, `total_pages`, `progress` | Физический лист с 1 или `null`, целые счётчики committed/выбранных листов, численный процент 0–100. Процент не служит ETA, 100% страниц ещё допускают финализацию. |
| `item_count`, `warning_count`, `prompt_version`, `links` | Целые счётчики, хэш версии промпта, относительные URL. Экспортные ссылки `json`/`xlsx` заполнены только при успешном терминальном состоянии. Общие ссылки `status`, `events`, `warnings`, у счетов также `snapshot`. |
| Только спецификации | `filename: str`, `document_id: UUID`, `document_pages: int`, `project_code: str`, `selected_pdf_pages: int[]`, `pages[]` с `pdf_page/state/attempts/item_count/warning_count/stages`. |
| Только счета | `current_file_index: int|null` с 0, `file_count/document_count: int`, `files[]` с `file_index/source_file/page_count/selected_pages/pages[]`. Страница содержит `pdf_page/state/attempts/stages`. |
| `stages[]` | `name/state: str`, `started_at` ISO 8601, `duration_ms: number|null`. |

Snapshot спецификаций возвращает `status`, `last_event_id`, `partial` (`{file_name, items}`), `raw_text`. Snapshot счетов возвращает `status`, `last_event_id`, `diagnostic_only: true`, `partial_pages` (`file_index`, `pdf_page`, `extraction`), `raw_text` и `result` (полная `invoice_extraction.v1` только при успехе). Промежуточные позиции и текст модели могут содержать данные документа и доступны только владельцу. Mapper должен выводить allowlist метаданных и обрабатывать текст как недоверенный. Lease token, полный промпт и учётные данные не входят в status. Финальный импорт читает `/result.json` после успеха.

SSE `Last-Event-ID` имеет приоритет над `?after=`, значения исключают сам cursor. Запрос сначала проверяет владельца и TTL. Браузерный disconnect не меняет job. Сохраняйте `last_event_id` snapshot, затем подключайтесь к потоку. После исчерпания терминального журнала сервер закрывает соединение. Клиент может повторно запросить snapshot при reconnect. Сервер отправляет heartbeat-комментарии и `access_expired` без ID при отзыве доступа во время стрима.

| События | Дополнительные данные к `timestamp` ISO 8601 и `revision: int` |
|---|---|
| `job_queued`, `job_started` | Спецификации передают `selected_pdf_pages` при очереди. `job_started` передаёт `recovered: bool`. |
| `page_started`, `stream_reset`, `text_delta` | `pdf_page`, при начале `attempt`, для дельты `text`, иногда `operation` у нового сегмента. |
| `stage_started`, `stage_completed` | `pdf_page`, `stage`; окончание содержит `state` и `duration_ms`. |
| `item_validated`, `warning`, `retry_scheduled` | Координаты и исходный item/source_pages либо диагностика/код/попытка. |
| `page_completed`, `document_validated` | Счётчики страницы. У счетов также событие итоговой проверки с `validation`, `document_index` и `item_count`. |
| `result_invalidated`, `cancellation_requested` | Retry сообщает `from_pdf_page` новой ревизии. Отмена сообщает о принятии запроса. |
| `job_completed`, `job_failed`, `job_cancelled` | Итоговый `status` и `error_code` или `null`, если переданы. Queued cancel может не передать эти два поля, проверяйте snapshot. |

События счетов добавляют `file_index`, `pdf_page`, `document_index` со значениями `int|null`. События дедуплицируются по `job_id` и `id`. Для нового результата ориентируйтесь на `revision`. Пример сокращённых status, running cancel, snapshot и кадра replay:

```text
GET /api/v1/invoice-extractions/00000000-0000-4000-8000-000000000003
{"status":"queued","revision":0,"started_at":null,"cancel_requested":false,"processed_pages":0,"total_pages":3,"progress":0,"last_event_id":1}

POST /api/v1/invoice-extractions/00000000-0000-4000-8000-000000000003/cancel
{"status":"running","revision":0,"started_at":"2026-11-24T11:00:05Z","cancel_requested":true,"processed_pages":1,"total_pages":3,"progress":33.33,"last_event_id":12}

GET /api/v1/invoice-extractions/00000000-0000-4000-8000-000000000003/snapshot
{"status":{"status":"cancelled","revision":0,"started_at":"2026-11-24T11:00:05Z","cancel_requested":true},"diagnostic_only":true,"result":null,"last_event_id":13}

GET /api/v1/invoice-extractions/00000000-0000-4000-8000-000000000003/events?after=12
id: 13
event: job_cancelled
data: {"revision":0,"file_index":0,"pdf_page":2,"document_index":null,"status":"cancelled","error_code":null,"timestamp":"2026-11-24T11:01:00Z"}
```
