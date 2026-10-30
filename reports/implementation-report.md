<!-- reports/implementation-report.md -->
# Отчёт реализации — 29.10.2026

Код подготовлен без Git add/commit/push со стороны агента. Полная производственная приёмка остаётся открытой до серверного прогона. 30.10.2026 владелец разрешил автоматическое создание app user/vhost через существующий RabbitMQ; эта подготовка включена в `scripts/up.sh`. Конфигурация shared stack не редактируется.

Актуальное исправление серверного PermissionError, Docker-регрессия 600/700 под UID 10001, AMQP-проверка worker и резервный registry описаны в [reports/deployment-repair.md](deployment-repair.md). Итог: Windows 151 passed (82%), Linux 150 passed; серверное развёртывание остаётся неподтверждённым из-за отказа SSH-аутентификации. Числа и ограничения ниже относятся к прежним прогонам.

## Что реализовано

- FastAPI REST `/api/v1`, типизированный OpenAPI и локальный Swagger `/docs`; независимый от UI запуск заданий, owner-scoped API credentials, идемпотентность.
- Собственная PostgreSQL/Alembic, durable DB-outbox и существующий RabbitMQ adapter, bounded worker, fencing lease/heartbeat, checkpoint/restart, cancel и page retry зависимого суффикса.
- Реальный PyMuPDF subprocess с Linux CPU/RAM/timeout limits, PNG previews, image-only PDF, adaptive crops, ограниченные загрузка/страницы/pixels/stream/events/items и TTL cleanup.
- Shared-vlm image + text + geometry, настоящий streaming, полная внутренняя JSON-валидация, retry ошибок, структурный carryover только соседних физических листов. Неоднозначность не превращается в выдуманные поля. Коллизия model row_id отклоняет страницу для повтора вместо удаления потенциально реальной строки.
- Backend собирает Caption из отдельных граф, задаёт подтверждённый UserComment1 и физический Pages. JSON — ровно items и пять строковых полей. Excel из того же набора, formula injection заблокирован без изменения значений.
- UI по двум renders: настоящее изображение слева, raw/validated JSON справа, работающие controls/exports/journal/метрики/warnings, ручной просмотр до следующего нового page_started, refresh/reconnect, серверный session prompt с restore/TTL/CSRF. Вёрстка проверена на 1672, 800, 375 px.
- Логи JSON в stdout с bounded Docker rotation; timing публичных операций, отдельные измерения текста/рендера/VLM/валидации/согласования/экспорта. Чужие библиотечные сообщения не раскрывают credentials URL.

## Фактические проверки

| Проверка | Результат |
|---|---|
| Windows `ops/check-quality.ps1 -Browser` | Ruff check, format check, 4 JS tests, 80 Python tests (78 offline + 2 Chromium) — пройдены |
| Linux, финальный production backend в отдельном контейнере | 78 offline tests, включая все четыре examples и окончательный RAM limit PDF-процесса — пройдены |
| Chromium UI/E2E | 2 сценария пройдены: upload, prompt, stream, JSON/Excel, preview/navigation/zoom/fit/pan/fullscreen, retry, reset, responsive, Swagger и manual view/reconnect |
| Chromium + реальный PDF + настоящий shared-vlm | 1 сценарий пройден: первый лист ТМ, 17 позиций, 8 различных предупреждений, равенство JSON/Excel; только транспорт очереди заменён локальным dispatcher, БД — SQLite |
| Реальная PostgreSQL | Миграции, отдельная test schema, concurrent idempotency, FOR UPDATE/fencing и атомарный replay — пройдены |
| Реальный shared-vlm | Alias, vision, streaming и JSON schema — пройдены; без inference API key |
| Все реальные сохранённые ответы VLM | 51/51 page responses проходят актуальную внутреннюю схему, включая уникальность row_id |
| Собственный Docker image | Собран успешно; runtime без root/read-only проверен |
| Собственный Compose | `config --quiet` с безопасными тестовыми значениями — пройден |
| Live API внутри Linux | `/health/live` 200, `/docs`/локальные Swagger assets/session 200, OpenAPI 19 paths с типизированным статусом |
| Readiness | Правильно 503: database=true, shared_vlm=true, rabbitmq=false |
| Настоящий RabbitMQ/worker вместе с PG/VLM | Серверный прогон не подтверждён; 30.10 добавлена автоподготовка app user/vhost. Offline doubles явно отделены от live tests |

Coverage последнего Windows-набора с Chromium: 83,83% statements, 66,42% branches, 80,71% объединённого показателя coverage.py. Это диагностическая метрика, не заявление о полном покрытии каждого deployment-пути. Native PDF subprocess реально вызывается тестами, но его строки не включаются в покрытие родительского процесса. Реальные AMQP restart/redelivery и Linux Chromium E2E на сервере требуют отдельного прогона; CI Linux browser workflow подготовлен.

В ограниченном Windows-окружении pytest сообщил одно предупреждение о правах записи своего cache; все 80 тестов прошли. В Linux предупреждений нет. Повторяющиеся текстовые замечания модели внутри одной страницы показываются один раз; raw-поток сохраняется полностью. Предупреждения по разным товарным строкам не объединяются.

## Реальное качество на документах

Полный проход всех 51 страниц выполнен после разделения наименования/марки/изготовителя; затем отдельно повторены 11 страниц ЭОМ после уточнения mapping длины кабеля. Ответы модели не заменялись эталонами. Baseline не изменялся.

| Комплект | Baseline | Подтверждено | Точных совпадений пяти полей | Exact F1 | Warnings | Время финального прохода, с |
|---|---:|---:|---:|---:|---:|---:|
| ТМ | 488 | 486 | 436 | 89,53% | 55 | 580,9 |
| ЭОМ | 197 | 197 | 170 | 86,29% | 19 | 215,4 |
| АТМД | 85 | 85 | 54 | 63,53% | 14 | 105,3 |
| УУТЭ | 32 | 32 | 9 | 28,12% | 12 | 40,3 |

Итого: 800 подтверждённых позиций, 802 baseline, 669 точных совпадений, 100 предупреждений. Exact F1 — строковое совпадение с предоставленным baseline, не абсолютная OCR accuracy. Особенно на сканах различаются орфография/пунктуация/марки и способ сборки Caption; похожие строки в отчёте только кандидаты для ручной сверки. JSON и Excel всех четырёх реальных результатов проверены на равенство каждой ячейки.

Матрица по всем 51 PDF-листам: `quality.md`; исходные различия/outputs/diagnostics/Excel: игнорируемый `reports/live/`. Схема входных комплектов: `corpus-matrix.md`. Восемь вручную сверенных по изображениям строк прошли проверку опорных фрагментов/изготовителя/единицы/количества — см. `golden-review.md`. Это малая выборка, полная golden-разметка всех позиций не подготовлена.

## Оставшиеся ограничения

1. Нельзя считать production deployment и сквозной RabbitMQ recovery принятыми до успешного серверного прогона `up.sh`/`--live`. С 30.10 выделенный app user/vhost создаются автоматически; недоступный broker не подменяется.
2. Две позиции ТМ на границе PDF 18–19 модель ошибочно сочла переносом; конфликт количеств и незакрытый carryover видны в warnings. Они исключены из подтверждённого экспорта. Нужны ручная сверка/новый анализ с уточнёнными инструкциями, а не автоматическая подстановка baseline.
3. Полный шифр ЭОМ не читается в штампе страницы 8: виден только `.СО`. Тестовый клиент использовал предоставленный baseline-код `267-РД-ИТП.ЭОМ.СО`; перед импортом его подтверждает владелец. АТМД имеет шифр АТМ.СО, отличающийся от имени файла.
4. Schema validation не гарантирует полноту распознавания. Все четыре результата имеют completed_with_warnings, сверка с оригиналом остаётся обязательной для спорных участков.
5. Вход первого релиза — PDF. Word-файлы не предоставлены и не объявляются поддержанными. Извлечение страниц задаёт пользователь; автоматического поиска спецификаций нет.
6. Отдельный Tesseract не установлен: для сканов реально используется vision shared-vlm. GPU/runtime shared stack не менялся, нагрузочное SLA и смена физической модели не проверялись.

## Передача пользователю

Список файлов: `files.md`. Настройки и запуск: `../README.md`. API-примеры: `../docs/API.md`. Проверки руками и pull/rebuild: `../docs/operations.md`.

Рабочие команды: Windows `.\ops\check-quality.ps1 -Browser`; Linux `bash scripts/test.sh --browser`; deployment `bash scripts/up.sh`. Все пользовательские команды показывают логи. Git commit/push выполняет владелец после просмотра diff и проверок.

## Публикация по адресу сервера — 29.10.2026

После push владельца сверены удалённый `main` приложения `86cb30a162698c9c8cf0ad87cb6e10711b768e35` и чистое локальное состояние на старте проверки. Текущий `main` shared infrastructure остался `b0d955acaeb9e54c24a4b44c787883808d467330`; правила прочитаны начиная с `docs/LLM_CONTEXT.md`.

Причина отсутствия доступа с другого компьютера — публикация порта только на `127.0.0.1`. Значение `APP_BIND` изменено на `0.0.0.0` в `.env.example`, Settings и fallback Compose. Стандартный адрес клиента — `http://<IP_сервера>:8083`; `0.0.0.0` в браузере не используется. Привязку к одному IP можно переопределить. Обновлены `README.md` и `docs/operations.md`. Новые секреты и миграции не требуются; применение требует пересоздания API через существующий `scripts/up.sh`.

Фактические проверки этого изменения:

- Windows `ops/check-quality.ps1 -Browser`: Ruff/format, 4 JS и 80 Python/Chromium тестов пройдены; одно прежнее предупреждение о pytest cache ACL.
- Реальный Compose config: стандартная и пустая привязка дают `0.0.0.0:8083`; overrides `127.0.0.1` и конкретного IP сохраняются. PostgreSQL и worker не получили host ports.
- Production image пересобран. Во временном собственном контейнере подтверждён Docker binding `0.0.0.0:8083`; по адресу Windows-хоста вне loopback получены HTTP 200 для `/`, `/docs`, `/openapi.json`, JS из HTML и `/api/v1/health/live`. Контейнер удалён после проверки. БД, RabbitMQ и модель этим сетевым тестом не подменялись и не проверялись.
- Серверная проверка только на чтение: `192.168.55.3:8000/health` вернул 200; TCP `192.168.55.3:8083` с рабочего компьютера недоступен. Docker context здесь — `desktop-linux`, доступа к shell сервера нет: серверное развёртывание, firewall/NAT и сквозная готовность не подтверждены.

Изменения публикации подготовлены без commit/push агента. Последующее разрешение владельца на автоматическую подготовку app user/vhost реализовано ниже.

## Автоподготовка RabbitMQ — 30.10.2026

Изучены текущие `scripts/up.sh`, `app/provisioning.py` и functional tests локального `contract-diff-search`. По прямому требованию владельца подготовка перенесена в существующий `scripts/up.sh` сервиса спецификаций. Основной репозиторий подтверждён владельцем: `Amadu-A/spec-extracting-ai`; серверный clone `neo-term-it` должен сменить origin перед pull.

Добавлены открытые Settings `RABBITMQ_CONTAINER=shared-rabbitmq-1` и `RABBITMQ_CLI_TIMEOUT_SECONDS=30`. Служебная операция `python -m app.operations broker-config` валидирует имена и секреты Pydantic и возвращает закрытый протокол родительскому скрипту. Его stdout содержит пароль и не предназначен для ручного вывода. Новый entrypoint-скрипт, зависимость, модель или брокер не добавлены.

Скрипт проверяет работающий контейнер и сеть ai-shared, создаёт отсутствующего пользователя `specification` без тегов, проверяет пароль, создаёт vhost `specification` и выдаёт права только в нём. Повторный запуск не пересоздаёт ресурсы и не меняет пароль. Отказ аутентификации, служебные теги, права в других vhost и ошибки CLI останавливают запуск. Есть flock и конечные timeout Docker/CLI.

Пароль передаётся через stdin в приватный временный файл `/dev/shm` внутри существующего брокера; файл удаляется при штатном завершении/обрабатываемых сигналах. RPC читает файл без секрета в argv. Пароли/файлы других проектов не читаются. Контракты проверены по исходникам RabbitMQ 4.1: [eval с позиционными bindings](https://github.com/rabbitmq/rabbitmq-server/blob/v4.1.0/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/eval_command.ex), [add_user](https://github.com/rabbitmq/rabbitmq-server/blob/v4.1.0/deps/rabbit/src/rabbit_auth_backend_internal.erl), [check_user_pass_login](https://github.com/rabbitmq/rabbitmq-server/blob/v4.1.0/deps/rabbit/src/rabbit_access_control.erl).

Фактические результаты:

- Windows quality с Chromium: 122 passed, 1 skipped (POSIX timeout проверяется на Linux), 4 deselected live; Ruff/format и 4 JS tests пройдены. Одно прежнее предупреждение pytest cache ACL.
- Linux offline: 121 passed, 6 deselected browser/live; проверен настоящий GNU timeout зависшего Docker-клиента. Docker и состояние RabbitMQ в functional тестах — изолированная подмена, не реальные AMQP вызовы.
- Команды создания `.env` из документации реально исполнены в пустом временном каталоге на Windows/Linux: ровно четыре секретные переменные, шесть независимых значений, три API-owner, права 600 на Linux, повтор не изменяет файл.
- Production image пересобран. Серверный RabbitMQ не изменялся из этой среды; подтверждение реального bootstrap и app-vhost ожидается после запуска оператором на aistation.

Изменённые файлы: `.env.example`, `AGENTS.md`, `app/core/settings.py`, `app/operations.py`, `scripts/up.sh`, `README.md`, `docs/operations.md`, `reports/files.md`, этот отчёт. Добавлены `tests/test_bootstrap_settings.py` и `tests/test_deployment.py`. Миграции не требуются. Commit/push выполняет владелец.

## Ошибки серверного запуска и очистка .env — 30.10.2026

Изучен приложенный лог владельца: Windows quality завершился 122 passed и успешным push `314a694` в оба origin URL. На сервере `scripts/test.sh` остановился на `node: command not found`; сборка остановилась на TLS handshake timeout обращения к `auth.docker.io` до подготовки RabbitMQ. Отказ блока создания существующего `.env` был предусмотренной защитой от перезаписи.

Linux/Windows quality теперь запускают Node, поставляемый закреплённым Playwright 1.64.1. Системный Node не требуется; лишний шаг setup-node удалён из CI. В Windows исправлено декодирование UTF-8 native stdout/stderr во время проверки с восстановлением прежней console encoding после команды.

Dockerfile использует официальный [derived image Astral](https://docs.astral.sh/uv/guides/integration/docker/) `ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim`, закреплённый OCI digest `sha256:28d570c06d150303b39adfa731ada20956c63fc86b1277d7ff2ad63185974092`. Его существование и linux/amd64 manifest проверены перед изменением. Python 3.12.15 / Debian 13 применяются в builder и runtime; загрузка Python не обращается к Docker Hub. Общий Docker daemon, registry mirrors, proxy и shared stack не изменялись. Образ PostgreSQL по-прежнему поступает из Docker Hub, если его нет локально.

Добавлена операция `uv run --frozen python -m app.operations normalize-env`. Она читает собственный `.env` через источник Pydantic, оставляет только DATABASE_PASSWORD/RABBITMQ_PASSWORD/UI_PASSWORD/API_KEYS, сохраняет действующие значения и их исходное quoting, генерирует только отсутствующие/пустые. Сохранение quoting важно для литеральных `$VARIABLE` и обратных слешей в Compose. Ошибочный синтаксис, неверный JSON и короткий имеющийся секрет вызывают отказ без перезаписи. Перед атомарной заменой проверяется точное чтение секретов обратно; исходник копируется в игнорируемый `data/env-backups/` с mode 600. Секреты не печатаются. Временные `.env.*` исключены из Git/Docker context, `.env.example` остаётся разрешённым каталогом. `python-dotenv` явно объявлен как прямая зависимость; версии пакетов в lock не обновлялись.

В показанном серверном файле лишние RABBITMQ_USER/RABBITMQ_VHOST со значением spec-extraction после очистки уступят место specification из каталога настроек. Новые секреты для уже заполненных четырёх полей не создаются.

Проверки окончательного кода:

- Windows quality с Chromium: 131 passed, 2 skipped, 4 deselected live; Ruff check/format и 4 JS-теста пройдены. Два skip относятся к Linux shell/GNU timeout и выполнены на Linux. Прежнее предупреждение о правах pytest cache сохраняется.
- Linux на новом production base: 131 passed, 6 deselected browser/live. Включены настоящий Node без системной установки, idempotency/backup/права 600, ошибочный/неполный `.env`, сложные пароли и отказ атомарной замены без потери исходника.
- Production build через GHCR успешен. Native smoke под UID 10001 с read-only filesystem и network=none подтвердил PyMuPDF, psycopg/openpyxl imports и три PNG большого листа. Новые AMQP/VLM прогоны не заявляются.
- В трёх формах искусственных паролей фактический hash значения внутри временного Compose-контейнера до/после очистки совпал: проверены доллары, кавычки и обратные слеши. Эти контейнеры запускались с network=none и удалены.
- Текущий серверный `.env` не читался и не изменялся агентом; оператор применяет команду после pull. Реальная серверная сборка/готовность подтверждаются следующим запуском `up.sh`.

Обновлены Dockerfile, CI, Windows/Linux quality scripts, защита env-артефактов, README и operations. Добавлены `app/infrastructure/environment.py`, `tests/test_environment.py`, regression для Linux Node в `tests/test_deployment.py`. Commit/push не выполнялись агентом.
