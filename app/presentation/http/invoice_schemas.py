# app/presentation/http/invoice_schemas.py
"""OpenAPI-схемы прогресса, диагностик и восстановления нового профиля."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.domain.invoices.models import InvoiceExport
from app.domain.models import Contract, JobState, Stage


class InvoicePageStatus(Contract):
    """Durable фактический этап одного физического листа."""

    pdf_page: int
    state: str
    attempts: int
    stages: list[Stage]


class InvoiceFileStatus(Contract):
    """Отдельный источник с собственным выбором страниц."""

    file_index: int
    source_file: str
    page_count: int
    selected_pages: list[int]
    pages: list[InvoicePageStatus]


class InvoiceStatus(Contract):
    """Прогресс пакета; финальные ссылки доступны только после полной обработки."""

    job_id: str
    status: JobState
    revision: int
    prompt_version: str
    current_file_index: int | None
    current_pdf_page: int | None
    processed_pages: int
    total_pages: int
    progress: float
    file_count: int
    document_count: int
    item_count: int
    warning_count: int
    error_code: str | None
    cancel_requested: bool = False
    started_at: datetime | None = None
    last_event_id: int
    expires_at: str
    created_at: str
    updated_at: str
    files: list[InvoiceFileStatus]
    links: dict[str, str | None]


class InvoiceDiagnostic(Contract):
    """Ошибка/предупреждение с координатами; без чувствительных трассировок."""

    file_index: int | None
    pdf_page: int | None
    document_index: int | None
    row_number: int | None
    code: str
    message: str
    row: dict | None = None
    check: dict | None = None


class InvoiceWarnings(Contract):
    """Пагинированный список диагностик; проверки сумм находятся и в результате."""

    total: int
    offset: int
    warnings: list[InvoiceDiagnostic]


class InvoiceSnapshot(Contract):
    """Диагностика не является результатом для импорта в ERP."""

    status: InvoiceStatus
    diagnostic_only: Literal[True] = True
    raw_text: str
    partial_pages: list[dict] = Field(default_factory=list)
    result: InvoiceExport | None
    last_event_id: int
