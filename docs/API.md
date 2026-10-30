<!-- docs/API.md -->
# Публичный API v1

Интерактивная локальная OpenAPI-документация: `/docs`, схема: `/openapi.json`. В Swagger кнопка Authorize принимает отдельный `X-API-Key`. Также поддерживается `Authorization: Bearer …`. Ключи разных интеграций имеют разных owners; чужие job/PDF/события выглядят как 404. Cookie UI не заменяет credential интеграции.

## Headless-клиент

```bash
curl -X POST http://127.0.0.1:8083/api/v1/extractions \
  -H "X-API-Key: $SPEC_CLIENT_KEY" \
  -H 'Idempotency-Key: purchase-2026-001' \
  -F 'file=@document.pdf;type=application/pdf' \
  -F 'pages=1-5, 8, 12-16' \
  -F 'project_code=267-РД-ИТП.ТМ'
```

`SPEC_CLIENT_KEY` здесь — временная переменная клиентского примера; сервер читает credentials исключительно через `Settings.API_KEYS`. `202` возвращает `job_id`, состояние и ссылки. `project_code` подтверждает вызывающий клиент: предложения PDF не принимаются автоматически. Идемпотентный ключ действует внутри одного owner, включает PDF content hash, выбранные страницы, шифр и prompt snapshot; другой payload даёт 409.

```bash
curl -H "X-API-Key: $SPEC_CLIENT_KEY" http://127.0.0.1:8083/api/v1/extractions/JOB_ID
curl -N -H "X-API-Key: $SPEC_CLIENT_KEY" -H 'Last-Event-ID: 25' http://127.0.0.1:8083/api/v1/extractions/JOB_ID/events
curl -OJ -H "X-API-Key: $SPEC_CLIENT_KEY" http://127.0.0.1:8083/api/v1/extractions/JOB_ID/result.json
curl -OJ -H "X-API-Key: $SPEC_CLIENT_KEY" http://127.0.0.1:8083/api/v1/extractions/JOB_ID/result.xlsx
```

Минимальный Python-клиент с переданным приложением credential:

```python
import time
import httpx

def extract(base_url: str, api_key: str, pdf_path: str) -> dict:
    """Отправляет PDF и ожидает durable результат без веб-интерфейса."""
    with httpx.Client(base_url=base_url, headers={"X-API-Key": api_key}, timeout=120) as client:
        with open(pdf_path, "rb") as source:
            response = client.post("/api/v1/extractions", files={"file": ("document.pdf", source, "application/pdf")},
                                   data={"pages": "1-3", "project_code": "ПОДТВЕРЖДЁННЫЙ-ШИФР"})
        response.raise_for_status()
        job = response.json()
        while job["status"] in {"queued", "running"}:
            time.sleep(2)
            response = client.get(f"/api/v1/extractions/{job['job_id']}")
            response.raise_for_status()
            job = response.json()
        if job["status"] not in {"completed", "completed_with_warnings"}:
            raise RuntimeError(f"Задание {job['job_id']}: {job['status']}, {job['error_code']}")
        response = client.get(job["links"]["json"])
        response.raise_for_status()
        return response.json()
```

## Состояния и события

Job: `queued`, `running`, `completed`, `completed_with_warnings`, `failed`, `cancelled`. Успешный JSON доступен только в двух `completed*` состояниях. Отмена/ошибка сохраняют partial для диагностики, но не выдают его за полный результат.

Основные SSE: `job_started`, `page_started`, `text_delta`, `item_validated`, `page_completed`, `warning`, `job_completed`, `job_failed`, `job_cancelled`. Дополнительные: `job_queued`, `stream_reset`, `stage_started`, `stage_completed`, `retry_scheduled`, `cancellation_requested`, `result_invalidated`. `id` монотонен внутри задания, `revision` изменяется при retry. Доставка at least once: клиент пропускает уже обработанные id. `text_delta` — raw output модели, не готовые записи.

`GET /extractions/{id}/snapshot` возвращает атомарный status/partial/raw/cursor для восстановления UI. Подписка с `?after=cursor` или `Last-Event-ID` продолжает журнал. Snapshot не является экспортом. Автопереход PDF происходит только на новый `page_started.pdf_page`; изменение current в status/reconnect не сбрасывает ручной просмотр.

## Управление и диагностика

Все пути ниже имеют префикс `/api/v1`:

| Endpoint | Поведение |
|---|---|
| `POST /documents` | Проверенный PDF для preview; возвращает document_id/page_count/предложенный шифр |
| `POST /extractions` | file либо document_id, pages, project_code; immutable prompt snapshot |
| `GET /extractions/{id}` | Прогресс завершённых страниц, counts, checkpoints/stages, timestamps/TTL, links |
| `GET /extractions/{id}/warnings?offset=0&limit=100` | Реальные причины и непроверенные строки; limit ≤ 200 |
| `POST /extractions/{id}/cancel` | Идемпотентная отмена, текущий запрос прерывается heartbeat-монитором |
| `POST /extractions/{id}/pages/{page}/retry` | Только terminal job и выбранный лист; инвалидируется зависимый суффикс с предыдущего соседнего листа |
| `GET /extractions/{id}/pages/{page}/preview?width=1200` | PNG любого листа оригинала; width 120–2400, собственный pixel limit |
| `GET /health/live`, `/health/ready` | Живость процесса / реальные DB, app broker и alias VLM |

UI автоматически получает анонимную сессию при GET `/` или `/api/v1/session`; пароль и `/session/login` удалены. `/session` возвращает csrf и expires_at, без authenticated. `/session/logout` остаётся для явного отзыва capability через API; `/session/prompt` (GET/PUT) и `/session/prompt/restore` (POST) ограничены текущей сессией. Write actions с cookie требуют `X-CSRF-Token` и допустимый Origin. Cookie HttpOnly/SameSite=Strict с фиксированным Max-Age: JOB_TIMEOUT_SECONDS + RETENTION_SECONDS + 3600 (по умолчанию 10800 секунд). Активность не продлевает TTL. После истечения GET `/session` выдаёт новый capability; прежние документы недоступны новой сессии. API_KEYS интеграций сохраняют прежний контракт.

RETENTION_SECONDS=3600 отсчитывается после completed/completed_with_warnings/failed/cancelled. До завершения expires_at резервирует бюджет обработки и хранения, после завершения возвращает фактический срок результата. Preview, SSE, status и exports проверяют TTL до ответа (410); фоновое физическое удаление не требуется для закрытия доступа.

Ошибки имеют форму `{"error":{"code":"…","message":"…","reference_id":"…"}}`. Различаются `unsupported_file`, `password_pdf`, `corrupted_pdf`, `empty_pdf`, `pdf_timeout`, `pages_out_of_bounds`, `page_limit`, `project_code_unconfirmed`, `upload_limit`, `queue_full`, `invalid_model_json`, `vlm_timeout`, `vlm_unavailable`, `vlm_contract_unsupported`, `vlm_truncated`, `result_not_ready`, `job_expired`, `csrf_failed` и другие документированные коды. Полные документы, prompts, cookies, credentials и внутренние traceback наружу не возвращаются.
