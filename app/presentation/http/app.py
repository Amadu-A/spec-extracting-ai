# app/presentation/http/app.py
"""FastAPI factory, безопасные ошибки и ограничение HTTP-периметра до multipart parser."""

import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.bootstrap import build_container
from app.core.observability import configure_logging
from app.domain.errors import ServiceError
from app.presentation.http.routes import router, session_info


class PerimeterMiddleware:
    """Ограничивает request body/частоту, добавляет CSP и correlation id без буферизации SSE."""

    def __init__(self, app, max_body: int, rate: int):
        """Лимиты рассчитаны на один Uvicorn worker; журнал ограничен 10000 IP buckets."""
        self.app, self.max_body, self.rate = app, max_body, rate
        self.buckets = OrderedDict()

    async def __call__(self, scope, receive, send):
        """Останавливает oversized поток до записи большого multipart spool на диск."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        reference = str(uuid4())
        scope.setdefault("state", {})["reference_id"] = reference
        headers = dict(scope.get("headers", []))
        ip = scope.get("client", ("unknown",))[0]
        key = ip
        now = monotonic()
        count, started = self.buckets.get(key, (0, now))
        if now - started >= 60:
            count, started = 0, now
        self.buckets[key] = (count + 1, started)
        self.buckets.move_to_end(key)
        while len(self.buckets) > 10000:
            self.buckets.popitem(last=False)
        if count >= self.rate:
            await self._error(
                scope, receive, send, 429, "rate_limit", "Слишком много запросов", reference
            )
            return
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = self.max_body + 1
        if content_length > self.max_body:
            await self._error(
                scope, receive, send, 413, "upload_limit", "Превышен лимит HTTP-загрузки", reference
            )
            return
        received = 0

        async def limited_receive():
            """Считает фактические bytes, в том числе без Content-Length."""
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_body:
                # FastAPI сохраняет HTTPException при multipart parsing, а произвольные
                # исключения преобразует в 400 и теряет исходный machine-readable код.
                raise StarletteHTTPException(413, "upload_limit")
            return message

        async def safe_send(message):
            """Не накапливает response и добавляет единые security headers."""
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"x-request-id", reference.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"same-origin"),
                        (b"x-frame-options", b"DENY"),
                        (b"cache-control", b"no-store"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; img-src 'self' blob: data:; script-src 'self'; style-src 'self'; frame-ancestors 'none'; object-src 'none'",
                        ),
                    ]
                )
            await send(message)

        await self.app(scope, limited_receive, safe_send)

    async def _error(
        self, scope, receive, send, status: int, code: str, message: str, reference: str
    ):
        """Возвращает тот же машинный error contract до входа в router."""
        response = JSONResponse(
            {"error": {"code": code, "message": message, "reference_id": reference}},
            status_code=status,
        )
        await response(scope, receive, send)


def create_app(dependencies=None) -> FastAPI:
    """Создаёт приложение; тесты передают свой composition root явно."""

    @asynccontextmanager
    async def lifespan(application):
        """Подключает production только при запуске, не при импорте тестовых модулей."""
        root = dependencies or build_container()
        application.state.container = root
        configure_logging(root.settings.log_level)
        yield
        await root.broker.close()
        await root.model.close()

    application = FastAPI(
        title="Извлечение спецификаций",
        version="1.0.0",
        lifespan=lifespan,
        description="Асинхронный API для Web UI, 1С и ботов. API-key: X-API-Key либо Bearer. Экспорт сохраняет пять строковых полей.",
        docs_url=None,
        redoc_url=None,
        swagger_ui_parameters={"persistAuthorization": False},
    )
    if dependencies:
        application.state.container = dependencies
    # Settings без секретов нужны middleware до lifespan; env остаётся типизированным.
    from app.core.settings import Settings

    settings = dependencies.settings if dependencies else Settings()
    application.add_middleware(
        PerimeterMiddleware,
        max_body=(settings.max_upload_mb + 1) * 1024 * 1024,
        rate=settings.rate_limit_per_minute,
    )
    application.include_router(router)
    web = Path(__file__).resolve().parents[2] / "web"
    application.mount("/static", StaticFiles(directory=web / "static"), name="static")
    templates = Jinja2Templates(directory=web / "templates")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        """Отдаёт оболочку; документы/прогресс загружаются тем же защищённым API."""
        response = templates.TemplateResponse(
            request=request, name="analysis.html", context={"max_upload_mb": settings.max_upload_mb}
        )
        await session_info(request, response)
        return response

    @application.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    async def api_docs(request: Request):
        """Отдаёт локальные Swagger assets, совместимые с CSP без внешних CDN."""
        return templates.TemplateResponse(request=request, name="docs.html")

    @application.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        """Преобразует предметную ошибку без утечки внутреннего состояния."""
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "reference_id": getattr(request.state, "reference_id", str(uuid4())),
                }
            },
            status_code=exc.status,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """Не возвращает body/пароли из Pydantic validation errors."""
        fields = [".".join(map(str, e["loc"])) for e in exc.errors()][:8]
        return JSONResponse(
            {
                "error": {
                    "code": "request_validation",
                    "message": "Проверьте поля: " + ", ".join(fields),
                    "reference_id": getattr(request.state, "reference_id", str(uuid4())),
                }
            },
            status_code=422,
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """Сохраняет единый контракт для chunked upload limit и framework 404/405."""
        limited = exc.status_code == 413
        return JSONResponse(
            {
                "error": {
                    "code": "upload_limit" if limited else "http_error",
                    "message": "Превышен лимит HTTP-загрузки"
                    if limited
                    else "Запрос не поддерживается",
                    "reference_id": getattr(request.state, "reference_id", str(uuid4())),
                }
            },
            status_code=exc.status_code,
        )

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        """Скрывает детали исключения; один correlation id связывает HTTP и журнал."""
        reference = getattr(request.state, "reference_id", str(uuid4()))
        logging.getLogger(__name__).exception(
            "http_unexpected_error", extra={"request_id": reference}
        )
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "Внутренняя ошибка сервиса",
                    "reference_id": reference,
                }
            },
            status_code=500,
        )

    return application
