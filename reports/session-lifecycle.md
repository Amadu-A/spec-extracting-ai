<!-- reports/session-lifecycle.md -->
# Анонимные сессии и срок хранения — 30.10.2026

Работа выполнена поверх локального и проверенного GitHub main `e6e951934676a87ee0cdb36868b66653eae37176`. Начальный рабочий каталог был чистым. Правила актуального main shared_infrasktructure прочитаны начиная с LLM_CONTEXT, включая Engineering и Frontend Guidelines. Общая инфраструктура, PDRD и contract-diff-search не изменялись. Commit/push агентом не выполнялись.

## Поведение по эталонному проекту

Изучен [contract-diff-search main](https://github.com/Amadu-A/contract-diff-search/tree/606e4680dba634885ffc658e95af3046dfd10bf7), в частности app/web/api.py, app/application/service.py, app/settings.py и .env.example. Перенесена модель случайной HttpOnly/SameSite=Strict capability-cookie без пользовательского входа; срок cookie равен JOB_TIMEOUT_SECONDS + RETENTION_SECONDS + 3600. По умолчанию это 3 часа. Время обработки ограничено часом, результат и оригиналы хранятся час после завершения задания; фоновая очистка имеет интервал 15 секунд.

## Изменения

- Полностью удалены HTML формы/окна входа, поле пароля, кнопка выхода и соответствующий JavaScript/CSS. Удалены SessionService.login, LoginInput и endpoint /api/v1/session/login. Внешние API_KEYS сохраняют прежнюю проверку и приоритет над cookie.
- GET / автоматически создаёт анонимную сессию. GET /api/v1/session возвращает csrf и expires_at; authenticated больше не возвращается. Сессия хранится по SHA256 capability, с фиксированным сроком без продления при чтении. Старый authenticated из DB JSON отбрасывается адаптером, SQL-миграция не нужна.
- Документы, результаты и временный промпт изолированы по cookie. Сохраняются CSRF/Origin, безопасные cookies, ограничения HTTP, API ownership и 404 для чужого UUID. Endpoint /session/logout сохранён для явного отзыва сессии программным клиентом; интерфейс входа/выхода отсутствует.
- После истечения сессии браузер автоматически получает новый capability, очищает прошлый просмотр и предлагает повторную загрузку. Запись до получения CSRF безопасно ждёт bootstrap. Интерфейс показывает фактический expires_at результата.
- RETENTION_HOURS и SESSION_TTL_MINUTES заменены на RETENTION_SECONDS=3600, JOB_TIMEOUT_SECONDS=3600, CLEANUP_INTERVAL_SECONDS=15 в Settings/.env.example. Пароль UI_PASSWORD удалён из модели и каталога. normalize-env удаляет его из существующего .env с backup, сохраняя DATABASE_PASSWORD, RABBITMQ_PASSWORD и API_KEYS.
- Исходный PDF без задания хранится час с загрузки. Queued/running job резервирует время обработки и хранения. В terminal state, включая failed/cancelled, срок меняется на час с завершения; retry получает новый бюджет. Лимит worker учитывает оставшийся бюджет после восстановления. Просроченные задания после простоя worker завершаются с job_timeout.
- Изменение срока job и максимального срока общего PDF выполняется одной транзакцией репозитория. Истечение одного результата не удаляет оригинал другого живого задания. API закрывает доступ сразу по TTL (410), даже до физической очистки. Отдельный цикл worker удаляет истёкшие jobs/events/sessions/PDF каждые 15 секунд и повторяет очистку после ошибок.
- Слои Ports & Adapters/DI сохранены; добавлены порт overdue_jobs и явный контракт cleanup_orphans. Схема PostgreSQL, очередь, VLM, Dockerfile и Compose не изменены.

## Проверки окончательного кода

| Проверка | Результат |
|---|---|
| Windows .\ops\check-quality.ps1 -Browser -Docker | 164 passed, 2 skipped, 4 deselected; 152,54 с; coverage 82% |
| Ruff check/format | Успех, 60 Python-файлов |
| JavaScript | 4 passed, Node из Playwright |
| Chromium E2E | 3 passed: PDF/экспорты/промпт; просмотр/reconnect/refresh; новая сессия после потери cookie |
| Настоящая Docker-регрессия | passed в составе gate: контекст с правами 600/700, UID 10001, import/config без UI_PASSWORD |
| Linux offline, network=none | 162 passed, 8 deselected; 122,27 с; coverage 81% |
| Настоящий PostgreSQL 16 | 1 passed, 3,03 с: миграции, concurrent idempotency, fencing, сроки общего PDF, JSON-предикаты и FK-очистка |
| Git diff --check | Успех; index не изменялся |

13 новых тестов tests/test_session_lifecycle.py проверяют отсутствие парольного endpoint/формы, cookie-флаги, изоляцию двух клиентов, фиксированный TTL и перевыпуск сессии, чтение старых DB snapshots, отсчёт хранения от terminal state, 410 до cleanup, общий PDF, job_timeout и физическое удаление без браузера. Часы заменены в тестах для проверки часовых границ; SQLite, PDF и файлы настоящие. Browser-модель и AMQP transport — явно test doubles; новая точность извлечения модели не измерялась. PostgreSQL запускался только как собственный временный тестовый контейнер без опубликованных портов и без доступа к ai-shared; контейнеры, его анонимный volume и изолированная сеть удалены. Общие RabbitMQ/VLM не поднимались и не менялись.

Два Windows skip — POSIX-проверки, выполненные на Linux. Четыре live-теста в полном offline gate исключены; PostgreSQL выполнен отдельно. Серверный лог владельца подтвердил развёртывание предыдущего e6e9519: UID 10001, PostgreSQL/RabbitMQ/VLM OK, API/worker healthy, durable queue consumers=1. Новая версия без входа на aistation агентом не развёртывалась. Внутренние подробные прогоны находятся в игнорируемых session-*.log; пользовательские scripts логи показывают. Снимок test-results/analysis-page.png просмотрен: форма входа отсутствует, PDF/JSON и exports доступны.

## Обновление

Существующие записи сохраняют уже записанный expires_at. Новые сроки действуют для новых сессий и новых/повторных заданий; старые snapshots читаются без SQL-миграции. Автоматическая очистка выполняется работающим worker; API применяет TTL независимо от этого процесса.

На Windows перед проверками можно очистить прежний .env штатной командой, если файл существует:

```powershell
uv run --frozen python -m app.operations normalize-env
.\ops\check-quality.ps1 -Browser -Docker
git diff --check
```

Сервер после пользовательского commit/push:

```bash
cd ~/projects/spec-extracting-ai
git pull --ff-only origin main
uv run --frozen python -m app.operations normalize-env
bash scripts/test.sh --browser --docker
bash scripts/up.sh
docker compose --env-file .env.example --env-file .env exec -T api python -m app.operations broker-check --require-consumer
curl --fail --show-error http://127.0.0.1:8083/api/v1/health/ready
```

Не генерируйте новые пароли БД/RabbitMQ и не пересоздавайте volumes. Нормализатор сохраняет имеющиеся значения и удаляет UI_PASSWORD; в .env остаются три секретных поля. Ручные проверки: открыть http://192.168.55.3:8083 и сразу загрузить PDF, проверить промпт/обработку/оба экспорта, затем refresh. В приватном окне создаётся независимая сессия, старый UUID не должен раскрывать документы. Очистка cookie и следующее действие должны автоматически обновить сессию без формы входа; новый PDF должен загружаться. После завершения status.expires_at должен быть примерно через час. После этого срока preview/SSE/exports недоступны, а worker удаляет оригинал на очередном цикле.

Полные тексты всех изменённых файлов с относительными путями: локальный игнорируемый reports/live/anonymous-session-changes.txt. Реальные .env/examples/outputs туда не входят.
