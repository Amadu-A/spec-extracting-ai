<!-- README.md -->
# Извлечение спецификаций

FastAPI-сервис извлекает товары и материалы с выбранных физических страниц PDF, включая сканы, через существующий `shared-vlm`. Web UI показывает оригинал слева и настоящий JSON-stream справа. Независимый REST API подходит для 1С, Telegram и других клиентов. JSON содержит только `items` с пятью строковыми полями; Excel строится из тех же данных.

## Структура

`app/domain` — правила и схема; `application` — use-case и порты; `infrastructure` — PostgreSQL/PDF/VLM/RabbitMQ/Excel; `presentation/http` — API; `web` — интерфейс. `migrations` — Alembic, `prompts` — стандартные инструкции, `tests` — проверки, `reports` — фактические измерения. Локальные `examples/`, `.env`, документы и результаты распознавания исключены из Git.

## Настройки

Все параметры читает Pydantic: `.env.example` → `.env` → окружение процесса. Открытые настройки меняются в каталоге `.env.example`. В `.env` запишите только секреты:

```dotenv
DATABASE_PASSWORD=длинный_случайный_пароль_собственной_БД
RABBITMQ_PASSWORD=пароль_выделенного_app_пользователя
UI_PASSWORD=длинный_пароль_веб_интерфейса
API_KEYS='{"onec":"отдельный_ключ_не_короче_24_символов","evaluation":"другой_длинный_ключ"}'
```

До запуска администратор должен выделить app user/vhost существующего RabbitMQ. Их открытые имена задаются в `RABBITMQ_USER`/`RABBITMQ_VHOST` каталога настроек. Скрипт ничего не создаёт и не меняет в shared stack. VLM не требует API key. Нужны Docker Compose и уже существующая сеть `ai-shared`.

## Развёртывание

```bash
bash scripts/up.sh
```

Команда собирает собственный образ, поднимает свою PostgreSQL, применяет миграции, проверяет реальный app-vhost и vision/streaming/JSON schema VLM, запускает API/worker с healthchecks. Логи видны. Общие сервисы и их volumes не затрагиваются.

По умолчанию UI и `/docs` доступны на `http://127.0.0.1:8083`. Для прямого доступа в LAN задайте разрешённый IP сервера в `APP_BIND` файла `.env.example`; порт 8083 выбран по предоставленному списку сервисов. Для HTTPS установите `COOKIE_SECURE=true` и явный список IP доверенного reverse-proxy в `TRUSTED_PROXY_HOSTS` (например, `["172.30.0.2"]`) в каталоге настроек. CSRF остаётся включён.

## Проверки

Windows (Python 3.12, uv, Node 22+):

```powershell
.\ops\check-quality.ps1
.\ops\check-quality.ps1 -Browser
```

Linux с теми же инструментами:

```bash
bash scripts/test.sh
bash scripts/test.sh --browser
```

Проверки не прячут логи. `--live` / `-Live` дополнительно проверяют реальные PostgreSQL (в отдельной временной schema), app-vhost и VLM по Settings. Для запуска live-проверок с host вместо Docker DNS нужны соответствующие адреса в окружении команды. Offline-модель существует только в tests и явно обозначена как double. Сквозной production RabbitMQ в этой поставке не проверен: учётную запись ещё выделяет администратор.

Реальная оценка всех четырёх комплектов после запуска сервиса:

```bash
uv run --frozen python -m scripts.evaluate --api-url http://127.0.0.1:8083 --owner evaluation
```

Нужны все PDF/JSON в `examples/` и ключ `evaluation` в `.env`. Manifest входных шифров описан в `tests/fixtures/README.md`. Матрица: [reports/quality.md](reports/quality.md); детали с наименованиями — только в игнорируемом `reports/live/`.

Контракты, примеры API и эксплуатация: [docs/API.md](docs/API.md), [docs/architecture.md](docs/architecture.md), [docs/operations.md](docs/operations.md). Ограничения и результаты приёмки: [reports/implementation-report.md](reports/implementation-report.md).
