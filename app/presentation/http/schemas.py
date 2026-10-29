# app/presentation/http/schemas.py
"""Транспортные формы и ответы OpenAPI; пятиколоночный Export находится в domain."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.domain.models import Export, JobState, Stage, Warning


class ErrorDetail(BaseModel):
    """Стабильная ошибка без внутренних путей, prompts или секретов."""

    code: str
    message: str
    reference_id: str


class ErrorResponse(BaseModel):
    """Общая структура ожидаемых и системных ошибок REST API."""

    error: ErrorDetail


class PromptInput(BaseModel):
    """Редактируемая безопасная часть инструкций, ограниченная web-сессией."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=20000)


class LoginInput(BaseModel):
    """Пароль UI используется только для текущего сеанса и не возвращается клиенту."""

    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(description="Пароль UI_PASSWORD из серверного .env")


class PageStatus(BaseModel):
    """Публичная checkpoint-метаинформация без внутренних prompt/lease/строк результата."""

    pdf_page: int = Field(description="Физический номер PDF-страницы, начиная с 1")
    state: Literal["pending", "running", "completed", "completed_with_warnings", "failed"]
    attempts: int
    item_count: int
    warning_count: int
    stages: list[Stage]


class OutputLinks(BaseModel):
    """Относительные авторизованные URL; json/xlsx равны null до готовности."""

    status: str
    events: str
    warnings: str
    json_url: str | None = Field(alias="json")
    xlsx: str | None


class ExtractionStatus(BaseModel):
    """Статус долговечного задания; количества и прогресс берутся из committed pages."""

    job_id: UUID
    status: JobState
    filename: str
    document_id: UUID
    document_pages: int = Field(description="Все физические страницы оригинального PDF")
    project_code: str = Field(description="Подтверждённое клиентом значение UserComment1")
    selected_pdf_pages: list[int]
    current_pdf_page: int | None = Field(description="Последний лист, реально переданный VLM")
    processed_pages: int
    total_pages: int = Field(description="Количество выбранных для обработки страниц")
    progress: float = Field(ge=0, le=100, description="Доля завершённых выбранных страниц, %")
    item_count: int
    warning_count: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime = Field(description="После TTL original/result/events недоступны")
    last_event_id: int = Field(description="Монотонный курсор SSE внутри задания")
    revision: int = Field(description="Номер ревизии результата, увеличивается при page retry")
    prompt_version: str = Field(description="SHA256 неизменяемого prompt snapshot")
    error_code: str | None
    cancel_requested: bool
    pages: list[PageStatus]
    links: OutputLinks


class DocumentStatus(BaseModel):
    """Ответ preflight upload: предложение шифра требует подтверждения вызывающим клиентом."""

    id: UUID
    filename: str
    page_count: int
    suggested_project_code: str | None
    expires_at: datetime


class Snapshot(BaseModel):
    """Восстанавливаемый UI snapshot; partial не является успешным полным экспортом."""

    status: ExtractionStatus
    partial: Export
    raw_text: str = Field(description="Непроверенный JSON-stream текущей VLM-страницы")
    last_event_id: int


class WarningPage(BaseModel):
    """Ограниченная страница diagnostics с причинами и исходными непроверенными полями."""

    total: int
    offset: int
    warnings: list[Warning]


class SessionStatus(BaseModel):
    """Метаданные browser session; cookie и prompt других клиентов не возвращаются."""

    authenticated: bool
    csrf: str = Field(description="Токен X-CSRF-Token для cookie write actions")
    expires_at: datetime


class PromptStatus(BaseModel):
    """Эффективные инструкции одной web-сессии и их версия."""

    prompt: str
    is_default: bool
    version: str
    expires_at: datetime


class DependencyStatus(BaseModel):
    """Готовность реальных соединений, не факт прохождения vision smoke."""

    database: bool
    rabbitmq: bool
    shared_vlm: bool


class ReadyStatus(BaseModel):
    """HTTP 503 сохраняет тот же health-контракт, если зависимость не готова."""

    ready: bool
    dependencies: DependencyStatus
