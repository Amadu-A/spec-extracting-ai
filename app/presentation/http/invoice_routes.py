# app/presentation/http/invoice_routes.py
"""Тонкий transport счетов; UI и 1С используют одни use-case и authentication."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.application.invoices.ports import Upload
from app.domain.errors import ServiceError
from app.domain.invoices.models import InvoiceExport
from app.domain.models import SUCCESS
from app.presentation.http.auth import container
from app.presentation.http.invoice_schemas import InvoiceSnapshot, InvoiceStatus, InvoiceWarnings
from app.presentation.http.routes import PNG_RESPONSE, Actor, event_response
from app.presentation.http.routes import router as legacy_router
from app.presentation.http.schemas import ErrorResponse, PromptInput, PromptStatus

router = APIRouter(
    prefix="/api/v1/invoice-extractions",
    tags=["Извлечение счетов"],
    responses={
        **legacy_router.responses,
        415: {
            "model": ErrorResponse,
            "description": "Неподдерживаемый PDF/JPEG/PNG или MIME не соответствует содержимому",
        },
        422: {
            "model": ErrorResponse,
            "description": "Повреждённый/защищённый PDF, pixel limit, неверные страницы/структура запроса",
        },
        502: {"model": ErrorResponse, "description": "VLM не подтвердила полный structured output"},
        504: {"model": ErrorResponse, "description": "Превышен срок ожидания VLM/распознавания"},
    },
)


@router.post("", status_code=202, response_model=InvoiceStatus)
async def create(
    request: Request,
    actor: Actor,
    files: Annotated[
        list[UploadFile],
        File(description="PDF/JPEG/PNG; 1–10 файлов, 50 МБ каждый, 100 МБ на batch по умолчанию"),
    ],
    page_selection: Annotated[
        str,
        Form(
            description='JSON: {"0":"1-3", "1":"2"}; без фильтра — все страницы; общий page budget MAX_SELECTED_PAGES'
        ),
    ] = "{}",
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """202 без ожидания VLM. Повтор ключа с другим payload → 409; пароль/ключи в JSON не попадают."""
    try:
        try:
            selection = json.loads(page_selection)
        except ValueError as exc:
            raise ServiceError("page_selection", "Ожидается JSON-словарь выбора страниц") from exc
        if not isinstance(selection, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in selection.items()
        ):
            raise ServiceError(
                "page_selection", "page_selection: индексы файлов и диапазоны должны быть строками"
            )
        uploads = []
        for file in files:

            async def chunks(file=file):
                """Передаёт spool в application ограниченными блоками."""
                while chunk := await file.read(1024 * 1024):
                    yield chunk

            uploads.append(
                Upload(
                    file.filename or "document",
                    file.content_type or "application/octet-stream",
                    chunks(),
                )
            )
        service = container(request).invoices
        job = await service.create(
            actor.owner,
            uploads,
            selection,
            idempotency_key,
            actor.session.invoice_prompt if actor.session else None,
        )
        return service.status(job)
    finally:
        for file in files:
            await file.close()


@router.get("/prompt", response_model=PromptStatus)
async def get_prompt(request: Request, actor: Actor):
    """Читает invoice prompt текущей web-сессии; API использует стандартный snapshot."""
    if not actor.session:
        raise ServiceError("web_session_required", "Требуется веб-сессия", 403)
    return await container(request).invoices.prompt(actor.session)


@router.put("/prompt", response_model=PromptStatus)
async def save_prompt(body: PromptInput, request: Request, actor: Actor):
    """Меняет только будущие invoice-задания этой сессии."""
    if not actor.session:
        raise ServiceError("web_session_required", "Требуется веб-сессия", 403)
    return await container(request).invoices.prompt(actor.session, body.prompt)


@router.post("/prompt/restore", response_model=PromptStatus)
async def restore_prompt(request: Request, actor: Actor):
    """Восстанавливает ресурс без изменения prompt спецификаций и работающих jobs."""
    if not actor.session:
        raise ServiceError("web_session_required", "Требуется веб-сессия", 403)
    return await container(request).invoices.prompt(actor.session, restore=True)


@router.get("/{job_id}", response_model=InvoiceStatus)
async def status(job_id: UUID, request: Request, actor: Actor):
    """Возвращает прогресс, expiry, counts и итоговые links без приватного prompt."""
    service = container(request).invoices
    return service.status(await service.owned(str(job_id), actor.owner))


@router.get("/{job_id}/snapshot", response_model=InvoiceSnapshot)
async def snapshot(job_id: UUID, request: Request, actor: Actor):
    """Восстанавливает UI/reconnect из одной ревизии durable snapshot."""
    service = container(request).invoices
    job = await service.owned(str(job_id), actor.owner)
    return dict(
        status=service.status(job),
        diagnostic_only=True,
        raw_text=next(
            (
                p.raw_text
                for c, p in zip(job.checkpoints, job.pages, strict=True)
                if c.file_index == job.current_file_index and c.pdf_page == job.current_pdf_page
            ),
            "",
        ),
        partial_pages=[
            dict(
                file_index=c.file_index,
                pdf_page=c.pdf_page,
                extraction=c.extraction.model_dump(mode="json"),
            )
            for c in job.checkpoints
            if c.extraction
        ],
        result=job.result if job.state in SUCCESS else None,
        last_event_id=job.event_seq,
    )


@router.get("/{job_id}/result.json", response_model=InvoiceExport)
async def result_json(job_id: UUID, request: Request, actor: Actor):
    """Только полный финальный invoice_extraction.v1; result_not_ready → 409, TTL → 410."""
    job = await container(request).invoices.result(str(job_id), actor.owner)
    return Response(
        job.result.model_dump_json(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="invoices-{job_id}.json"'},
    )


@router.get(
    "/{job_id}/result.xlsx",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            }
        }
    },
)
async def result_excel(job_id: UUID, request: Request, actor: Actor):
    """Новая финансовая книга с листами Документы/Позиции/Предупреждения."""
    return Response(
        await container(request).invoices.excel(str(job_id), actor.owner),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="invoices-{job_id}.xlsx"'},
    )


@router.get("/{job_id}/warnings", response_model=InvoiceWarnings)
async def warnings(
    job_id: UUID,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    """Неопределённые поля, ошибки и доказанные расхождения с координатами источника."""
    service = container(request).invoices
    values = service.warnings(await service.owned(str(job_id), actor.owner))
    return dict(total=len(values), offset=offset, warnings=values[offset : offset + limit])


@router.get(
    "/{job_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def events(
    job_id: UUID,
    request: Request,
    actor: Actor,
    after: int = Query(0, ge=0),
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
):
    """Replay включает file_index/pdf_page/document_index/revision; disconnect не отменяет job."""
    return await event_response(
        str(job_id), request, actor, container(request).invoices, after, last_event_id
    )


@router.get(
    "/{job_id}/files/{file_index}/pages/{page}/preview",
    response_class=Response,
    responses=PNG_RESPONSE,
)
async def preview(
    job_id: UUID,
    file_index: int,
    page: int,
    request: Request,
    actor: Actor,
    width: int = Query(1200, ge=120, le=2400),
):
    """Оригинал PDF/JPEG/PNG через защищённый viewer, без публичного каталога файлов."""
    return Response(
        await container(request).invoices.preview_file(
            str(job_id), actor.owner, file_index, page, width
        ),
        media_type="image/png",
    )


@router.post("/{job_id}/cancel", response_model=InvoiceStatus)
async def cancel(job_id: UUID, request: Request, actor: Actor):
    """Идемпотентная отмена общего worker lifecycle."""
    service = container(request).invoices
    return service.status(await service.cancel(str(job_id), actor.owner))


@router.post(
    "/{job_id}/files/{file_index}/pages/{page}/retry", status_code=202, response_model=InvoiceStatus
)
async def retry(job_id: UUID, file_index: int, page: int, request: Request, actor: Actor):
    """Инвалидирует результат и зависимые страницы только выбранного файла."""
    service = container(request).invoices
    return service.status(await service.retry_file(str(job_id), actor.owner, file_index, page))
