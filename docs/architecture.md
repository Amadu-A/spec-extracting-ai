<!-- docs/architecture.md -->
# Архитектура и границы

Правила сверены 29.10.2026 с актуальным `main` [shared_infrasktructure](https://github.com/Amadu-A/shared_infrasktructure/tree/b0d955acaeb9e54c24a4b44c787883808d467330/docs), SHA `b0d955acaeb9e54c24a4b44c787883808d467330`. Прочитаны LLM_CONTEXT, ENGINEERING_GUIDELINES, FRONTEND_GUIDELINES, INFRASTRUCTURE_INSTRUCTIONS, services.yaml и ARCHITECTURE_DECISIONS. На старте локально/в публичном репозитории был только .gitignore; существующие API отсутствовали. README.txt/TASK.md/design/fixtures не предоставлены: использованы приложенное полное ТЗ, renders/1.png, renders/2.png и четыре фактические пары examples/.

## Слои

| Слой | Ответственность | Зависимости |
|---|---|---|
| domain | строгий Export, Row, provenance, переносы, state transitions, сравнение baseline | Pydantic как осознанное упрощение DTO |
| application | upload/start/process/retry/cancel/session/export | domain, маленькие порты, общие Settings/timing |
| infrastructure | SQLAlchemy, PyMuPDF subprocess, httpx VLM stream, aio-pika, local files, openpyxl | реализует application ports |
| presentation/http | REST/SSE, auth/CSRF, response framing | application через composition root |
| web | state, API helper, viewer/prompt/results/analysis modules, BEM CSS | только публичный HTTP API |

`app/bootstrap.py` — единственная точка сборки конкретных зависимостей. AST tests проверяют направление импортов. Все source/config/script/template файлы собственных компонентов документированы на русском; upstream Swagger assets сохраняют оригинальную лицензию.

## Транзакции и восстановление

Собственная PostgreSQL хранит job snapshots, page checkpoints/provenance, серверные сессии и монотонный журнал. `repository.edit` выполняет чистую mutation под `SELECT FOR UPDATE`, сохраняет snapshot и события в одной транзакции. Вызов VLM не держит SQL-транзакцию. Parent job flush предшествует event insert, commit остаётся единым.

Таблица jobs служит durable outbox: dispatcher повторно публикует queued и stale-running задания в существующую app-очередь RabbitMQ. Сообщение содержит только UUID. Bounded prefetch/concurrency и fencing lease предотвращают двойную обработку при повторной доставке. Worker ack следует за сохранением состояния; после остановки текущая delivery возвращается broker, lease истекает, следующий worker пропускает committed страницы. In-memory dispatcher throttle ограничен и не является источником истины.

Raw model stream сохраняется до валидации отдельно от items. Snapshot содержит raw текущего листа; предыдущие chunks остаются в SSE journal. Перенос содержит pending row, группы и source_pages; только соседние физические страницы могут продолжить строку. Разные реальные одинаковые строки сохраняются, коллизия model row_id отклоняется с повтором страницы, без произвольного удаления строк. Backend составляет Caption из отдельных граф наименования/марки/изготовителя, задаёт UserComment1 и Pages. Незавершённые/непроверенные строки остаются warnings.

Retry повторяет не только выбранную страницу, но и зависимый суффикс, начиная с предыдущей физически соседней страницы. Это намеренный консервативный компромисс ради корректности групп и переносов. Экспорт не смешивает разные ревизии.

## Ограничения ресурсов и безопасности

MIME + сигнатура + структура PDF, запрет зашифрованных и repaired документов. UUID paths, приватный volume, отдельный убиваемый PDF subprocess: Linux RLIMIT CPU/RAM, timeout, ограничение геометрии/DPI/pixels. Большой лист получает общий render и две детальные половины в одном VLM-запросе; дубли не удаляются по равным названиям. Image-only PDF передаётся модели как изображение. Отдельный OCR-движок не требуется и не установлен.

Ограничены multipart body до spool, число страниц, длина prompt/ответа, events/items, очередь и частота запросов. UI session и API owners разделены. Cookie/CSRF/origin, безопасный DOM через textContent, формулы Excel сохраняются текстом. Секреты типизированы SecretStr и не выводятся. Приватный PostgreSQL не публикует порт и не подключается к ai-shared. API/worker работают без root с read-only rootfs и ограничением памяти. Логи JSON в stdout, local driver 5×10 MB. Timing public use-case основан на perf_counter.

Входящие документы не отправляются во внешние модели. Application использует `http://shared-vlm:8000/v1`, alias `shared-vlm`; модели/GPU/layout принадлежат shared stack. `n8n-db`, Redis, новая модель и новый broker не создаются.
