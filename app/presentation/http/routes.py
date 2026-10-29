# app/presentation/http/routes.py
"""Тонкий REST transport: валидация, owner, SSE framing и сериализация экспорта."""

import asyncio
import hashlib
import json
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.domain.errors import ServiceError
from app.domain.models import TERMINAL, Export
from app.presentation.http.auth import COOKIE, Principal, check_csrf, container, principal
from app.presentation.http.schemas import (
    DocumentStatus,
    ErrorResponse,
    ExtractionStatus,
    LoginInput,
    PromptInput,
    PromptStatus,
    ReadyStatus,
    SessionStatus,
    Snapshot,
    WarningPage,
)

router = APIRouter(
    prefix="/api/v1",
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Требуется cookie-сессия или отдельный API credential",
        },
        403: {"model": ErrorResponse, "description": "CSRF или origin не подтверждён"},
        404: {"model": ErrorResponse, "description": "Не найден ресурс текущего owner"},
        409: {
            "model": ErrorResponse,
            "description": "Конфликт idempotency/state или результат не готов",
        },
        410: {"model": ErrorResponse, "description": "Истёк срок хранения"},
        413: {"model": ErrorResponse, "description": "Превышен лимит загрузки"},
        415: {"model": ErrorResponse, "description": "Неподдерживаемый формат/сигнатура PDF"},
        422: {"model": ErrorResponse, "description": "Невалидный PDF, страницы или шифр проекта"},
        429: {"model": ErrorResponse, "description": "Очередь/частота запросов превышает лимит"},
        503: {"model": ErrorResponse, "description": "Зависимость не готова"},
    },
)
Actor = Annotated[Principal, Depends(principal)]
PNG_RESPONSE = {
    200: {
        "description": "Ограниченный PNG физической PDF-страницы",
        "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def set_cookie(response: Response, token: str, secure: bool) -> None:
    """Session-only cookie без persistent Max-Age, недоступный JavaScript."""
    response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite="strict", path="/")


@router.get("/session", tags=["Web-сессия"], response_model=SessionStatus)
async def session_info(request: Request, response: Response) -> dict:
    """Создаёт/возвращает browser session; prompt и документы требуют входа."""
    dependencies = container(request)
    try:
        session = await dependencies.sessions.resolve(
            request.cookies.get(COOKIE), authenticated=False
        )
    except ServiceError:
        token, session = await dependencies.sessions.create()
        set_cookie(response, token, dependencies.settings.cookie_secure)
    return {
        "authenticated": session.authenticated,
        "csrf": session.csrf,
        "expires_at": session.expires_at.isoformat(),
    }


@router.post("/session/login", tags=["Web-сессия"], response_model=SessionStatus)
async def login(body: LoginInput, request: Request, response: Response) -> dict:
    """Авторизует UI и ротирует cookie; интеграции используют отдельные API_KEYS."""
    dependencies = container(request)
    session = await dependencies.sessions.resolve(request.cookies.get(COOKIE), authenticated=False)
    check_csrf(request, session)
    token, authenticated = await dependencies.sessions.login(
        request.cookies[COOKIE], body.password.get_secret_value()
    )
    set_cookie(response, token, dependencies.settings.cookie_secure)
    return {
        "authenticated": True,
        "csrf": authenticated.csrf,
        "expires_at": authenticated.expires_at.isoformat(),
    }


@router.post("/session/logout", tags=["Web-сессия"])
async def logout(request: Request, response: Response, actor: Actor) -> dict:
    """Отзывает текущую web-сессию и её временный prompt немедленно."""
    if not actor.session:
        raise ServiceError("web_session_required", "Операция предназначена для UI", 403)
    await container(request).repository.delete_session(actor.session.token_hash)
    response.delete_cookie(COOKIE, path="/")
    return {"logged_out": True}


@router.get("/session/prompt", tags=["Web-сессия"], response_model=PromptStatus)
async def get_prompt(request: Request, actor: Actor) -> dict:
    """Возвращает действующие инструкции только текущей web-сессии."""
    if not actor.session:
        raise ServiceError("web_session_required", "API-клиенты используют стандартный prompt", 403)
    return await container(request).sessions.prompt(actor.session)


@router.put("/session/prompt", tags=["Web-сессия"], response_model=PromptStatus)
async def save_prompt(body: PromptInput, request: Request, actor: Actor) -> dict:
    """Сохраняет prompt только для будущих заданий текущей web-сессии."""
    if not actor.session:
        raise ServiceError("web_session_required", "Требуется web-сессия", 403)
    return await container(request).sessions.prompt(actor.session, value=body.prompt)


@router.post("/session/prompt/restore", tags=["Web-сессия"], response_model=PromptStatus)
async def restore_prompt(request: Request, actor: Actor) -> dict:
    """Немедленно восстанавливает ресурс prompts/extract_specification_v1.txt."""
    if not actor.session:
        raise ServiceError("web_session_required", "Требуется web-сессия", 403)
    return await container(request).sessions.prompt(actor.session, restore=True)


async def upload_document(request: Request, actor: Principal, upload: UploadFile):
    """Передаёт ограниченный поток use-case, не читая весь PDF в transport RAM."""

    async def chunks():
        """Читает spool файла блоками до application-level лимита."""
        while chunk := await upload.read(1024 * 1024):
            yield chunk

    try:
        return await container(request).extractions.upload(
            actor.owner,
            upload.filename or "document.pdf",
            upload.content_type or "application/octet-stream",
            chunks(),
        )
    finally:
        await upload.close()


@router.post("/documents", status_code=201, tags=["Документы"], response_model=DocumentStatus)
async def inspect_document(
    request: Request,
    actor: Actor,
    file: Annotated[UploadFile, File(description="Один PDF, включая скан")],
) -> dict:
    """Загружает PDF для настоящего preview и предлагает шифр для подтверждения."""
    document = await upload_document(request, actor, file)
    return document.model_dump(mode="json", exclude={"owner", "sha256"})


@router.get(
    "/documents/{document_id}/pages/{pdf_page}/preview",
    tags=["Документы"],
    response_class=Response,
    responses=PNG_RESPONSE,
)
async def document_preview(
    document_id: UUID,
    pdf_page: int,
    request: Request,
    actor: Actor,
    width: int = Query(1200, ge=120, le=2400),
) -> Response:
    """Отдаёт PNG физического PDF-листа; width ограничен до безопасного рендеринга."""
    data = await container(request).extractions.preview(
        str(document_id), actor.owner, pdf_page, width
    )
    return Response(data, media_type="image/png")


@router.post("/extractions", status_code=202, tags=["Извлечение"], response_model=ExtractionStatus)
async def create_extraction(
    request: Request,
    actor: Actor,
    pages: Annotated[str, Form(description="Физические PDF-страницы с 1: 1-5, 8, 12-16")],
    file: Annotated[
        UploadFile | None, File(description="Один PDF; альтернатива document_id")
    ] = None,
    document_id: Annotated[
        UUID | None, Form(description="Ранее загруженный PDF текущего owner")
    ] = None,
    project_code: Annotated[
        str, Form(description="Подтверждённый шифр проекта, записываемый в UserComment1")
    ] = "",
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """Принимает задание асинхронно; не ждёт VLM и не зависит от открытого UI."""
    if (file is None) == (document_id is None):
        raise ServiceError("document_source_required", "Передайте ровно один file или document_id")
    if file is not None:
        document_id = (await upload_document(request, actor, file)).id
    dependencies = container(request)
    job = await dependencies.extractions.start(
        actor.owner,
        str(document_id),
        pages,
        project_code,
        idempotency_key,
        actor.session.prompt if actor.session else None,
    )
    return dependencies.extractions.status(job)


@router.get("/extractions/{job_id}", tags=["Извлечение"], response_model=ExtractionStatus)
async def get_status(job_id: UUID, request: Request, actor: Actor) -> dict:
    """Возвращает checkpoints, истинный прогресс, времена этапов и доступные outputs."""
    service = container(request).extractions
    return service.status(await service.owned(str(job_id), actor.owner))


@router.get("/extractions/{job_id}/snapshot", tags=["Диагностика"], response_model=Snapshot)
async def get_snapshot(job_id: UUID, request: Request, actor: Actor) -> dict:
    """Восстанавливает UI из одного атомарного snapshot; partial не является полным экспортом."""
    service = container(request).extractions
    job = await service.owned(str(job_id), actor.owner)
    page = next((p for p in job.pages if p.pdf_page == job.current_pdf_page), None)
    return {
        "status": service.status(job),
        "partial": job.export().model_dump(),
        "raw_text": page.raw_text if page else "",
        "last_event_id": job.event_seq,
    }


@router.get("/extractions/{job_id}/warnings", tags=["Диагностика"], response_model=WarningPage)
async def get_warnings(
    job_id: UUID,
    request: Request,
    actor: Actor,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
) -> dict:
    """Пагинирует непроверенные строки с причинами и физическими страницами."""
    job = await container(request).extractions.owned(str(job_id), actor.owner)
    warnings = [w for p in job.pages for w in p.warnings]
    return {
        "total": len(warnings),
        "offset": offset,
        "warnings": [w.model_dump(mode="json") for w in warnings[offset : offset + limit]],
    }


@router.post("/extractions/{job_id}/cancel", tags=["Управление"], response_model=ExtractionStatus)
async def cancel(job_id: UUID, request: Request, actor: Actor) -> dict:
    """Идемпотентно отменяет job, сохраняя подтверждённые позиции для диагностики."""
    service = container(request).extractions
    return service.status(await service.cancel(str(job_id), actor.owner))


@router.post(
    "/extractions/{job_id}/pages/{pdf_page}/retry",
    status_code=202,
    tags=["Управление"],
    response_model=ExtractionStatus,
)
async def retry(job_id: UUID, pdf_page: int, request: Request, actor: Actor) -> dict:
    """Повторяет выбранную страницу и зависимый суффикс после terminal state."""
    service = container(request).extractions
    return service.status(await service.retry(str(job_id), actor.owner, pdf_page))


@router.get(
    "/extractions/{job_id}/pages/{pdf_page}/preview",
    tags=["Документы"],
    response_class=Response,
    responses=PNG_RESPONSE,
)
async def job_preview(
    job_id: UUID,
    pdf_page: int,
    request: Request,
    actor: Actor,
    width: int = Query(1200, ge=120, le=2400),
) -> Response:
    """Просматривает любой лист оригинала независимо от выбранных страниц анализа."""
    service = container(request).extractions
    job = await service.owned(str(job_id), actor.owner)
    return Response(
        await service.preview(job.document_id, actor.owner, pdf_page, width), media_type="image/png"
    )


@router.get(
    "/extractions/{job_id}/events",
    tags=["Поток"],
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE с id, event, JSON data; reconnect через Last-Event-ID или after",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def events(
    job_id: UUID,
    request: Request,
    actor: Actor,
    after: int = Query(0, ge=0),
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
):
    """SSE replay: id и Last-Event-ID дают доставку без потери committed событий."""
    dependencies = container(request)
    job = await dependencies.extractions.owned(str(job_id), actor.owner)
    cursor = last_event_id if last_event_id is not None else after
    if cursor < 0 or cursor > job.event_seq:
        raise ServiceError("invalid_event_cursor", "Курсор не принадлежит журналу задания", 409)

    async def stream():
        """Отправляет ограниченные batches; disconnect не отменяет durable worker."""
        nonlocal cursor
        heartbeat = 0
        next_auth_check = monotonic() + 10
        while not await request.is_disconnected():
            if actor.session and monotonic() >= next_auth_check:
                next_auth_check = monotonic() + 10
                if await dependencies.repository.session(actor.session.token_hash) is None:
                    yield "event: access_expired\ndata: {}\n\n"
                    return
            try:
                current = await dependencies.extractions.owned(str(job_id), actor.owner)
            except ServiceError:
                yield "event: access_expired\ndata: {}\n\n"
                return
            batch = await dependencies.repository.events(str(job_id), cursor, 200)
            for event in batch:
                cursor = event.id
                payload = {**event.data, "timestamp": event.timestamp.isoformat()}
                yield f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if current.state in TERMINAL and cursor >= current.event_seq:
                return
            if not batch:
                heartbeat += 1
                if heartbeat % 10 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/extractions/{job_id}/result.json", tags=["Экспорт"], response_model=Export)
async def result_json(job_id: UUID, request: Request, actor: Actor) -> Response:
    """Возвращает ровно items и пять строковых полей, только после полного завершения."""
    data = await container(request).exports.json_bytes(str(job_id), actor.owner)
    return Response(
        data,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="specification-{job_id}.json"',
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        },
    )


@router.get(
    "/extractions/{job_id}/result.xlsx",
    tags=["Экспорт"],
    response_class=Response,
    responses={
        200: {
            "description": "Excel, эквивалентный пятиколоночному JSON",
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def result_excel(job_id: UUID, request: Request, actor: Actor) -> Response:
    """Экспортирует те же значения в Excel; формулы из PDF остаются обычным текстом."""
    data = await container(request).exports.excel(str(job_id), actor.owner)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="specification-{job_id}.xlsx"'},
    )


@router.get("/health/live", tags=["Здоровье"])
async def live() -> dict:
    """Живость HTTP-процесса не зависит от состояния общей модели."""
    return {"status": "alive"}


@router.get(
    "/health/ready",
    tags=["Здоровье"],
    response_model=ReadyStatus,
    responses={503: {"model": ReadyStatus}},
)
async def ready(request: Request, response: Response) -> dict:
    """Проверяет реальные DB/broker/model; не утверждает vision до smoke-теста."""
    dependencies = container(request)
    checks = await asyncio.gather(
        dependencies.repository.ping(),
        dependencies.broker.ping(),
        dependencies.model.ping(),
        return_exceptions=True,
    )
    status = {
        name: result is True
        for name, result in zip(["database", "rabbitmq", "shared_vlm"], checks, strict=True)
    }
    if not all(status.values()):
        response.status_code = 503
    return {"ready": all(status.values()), "dependencies": status}
