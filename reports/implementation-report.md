<!-- reports/implementation-report.md -->
# Отчёт реализации — 29.10.2026

Код подготовлен в рабочем каталоге без Git add/commit/push и без изменения shared stack. Полная производственная приёмка остаётся открытой: владелец подтвердил, что администратор ещё должен выделить app user/vhost RabbitMQ.

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
| Настоящий RabbitMQ/worker вместе с PG/VLM | Не проверен: app account/vhost пока не выделены; offline doubles явно отделены от live tests |

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

1. Нельзя считать production deployment и сквозной RabbitMQ recovery принятыми до выделения app credentials и прогона `--live`. Скрипт up не подменяет недоступный broker: dependency preflight останавливает запуск.
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

Изменения подготовлены локально без commit/push. Для полного запуска остаётся выделить app user/vhost RabbitMQ и заполнить настройки по README; shared stack не изменялся.
