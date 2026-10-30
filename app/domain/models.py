# app/domain/models.py
"""Типы задания и строгий экспорт; Pydantic используется как единая граница данных."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


def utcnow() -> datetime:
    """Возвращает timezone-aware UTC для хранения и TTL."""
    return datetime.now(UTC)


class Contract(BaseModel):
    """Запрещает незаявленные поля на внешних и сохраняемых границах."""

    model_config = ConfigDict(extra="forbid")


class SpecificationItem(Contract):
    """Машинный контракт 1С: ровно пять строк, без технических метаданных."""

    Caption: StrictStr = Field(min_length=1, max_length=20000)
    Units: StrictStr = Field(min_length=1, max_length=100)
    Quantity: StrictStr = Field(min_length=1, max_length=100)
    UserComment1: StrictStr = Field(min_length=1, max_length=200)
    Pages: StrictStr = Field(pattern=r"^[1-9]\d*$")


class Export(Contract):
    """Единственная допустимая верхняя структура JSON-экспорта."""

    items: list[SpecificationItem]


class Row(Contract):
    """Промежуточная строка VLM с физическим идентификатором табличного фрагмента."""

    row_id: StrictStr = Field(min_length=1, max_length=200)
    caption: StrictStr = Field(max_length=20000)
    designation: StrictStr = Field(max_length=4000)
    manufacturer: StrictStr = Field(max_length=2000)
    units: StrictStr = Field(max_length=100)
    quantity: StrictStr = Field(max_length=100)
    continuation: bool
    continues_next: bool
    verified: bool


class PageExtraction(Contract):
    """Внутренний ответ модели; эти diagnostics не попадают в экспорт 1С."""

    rows: list[Row] = Field(max_length=2000)
    group_context: list[StrictStr] = Field(max_length=30)
    warnings: list[StrictStr] = Field(max_length=200)
    completeness: Literal["complete", "uncertain", "not_specification"]

    @model_validator(mode="after")
    def unique_physical_ids(self) -> Self:
        """Коллизия model row_id требует повторения страницы, а не удаления реального товара."""
        ids = [row.row_id for row in self.rows]
        if len(ids) != len(set(ids)):
            raise ValueError(
                "Модель повторила row_id: нельзя доказать идентичность физических фрагментов"
            )
        return self


class Warning(Contract):
    """Явная причина, по которой полнота или строка требуют проверки человеком."""

    pdf_page: int
    code: str
    message: str
    row: Row | None = None


class Evidence(Contract):
    """Происхождение экспортной записи, включая перенос через границу листов."""

    item: SpecificationItem
    source_pages: list[int]
    row_id: str


class Carryover(Contract):
    """Только незавершённая строка и группы непосредственно соседнего листа."""

    pdf_page: int
    pending: Row | None = None
    source_pages: list[int] = Field(default_factory=list)
    group_context: list[str] = Field(default_factory=list)


class JobState(StrEnum):
    """Терминальные и исполняемые состояния durable задания."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {JobState.COMPLETED, JobState.WARNINGS, JobState.FAILED, JobState.CANCELLED}
SUCCESS = {JobState.COMPLETED, JobState.WARNINGS}


class Stage(Contract):
    """Фактический этап с измеренной длительностью и безопасной причиной ошибки."""

    name: str
    state: str = "running"
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: float | None = None


class PageAnalysis(Contract):
    """Контрольная точка страницы; сохраняется атомарно с событиями и строками."""

    pdf_page: int
    state: Literal["pending", "running", "completed", "completed_with_warnings", "failed"] = (
        "pending"
    )
    items: list[Evidence] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)
    carryover: Carryover | None = None
    raw_text: str = ""
    attempts: int = 0


class Event(Contract):
    """Неизменяемое событие SSE с монотонным номером внутри задания."""

    id: int
    type: str
    data: dict
    timestamp: datetime = Field(default_factory=utcnow)


class Job(Contract):
    """Снимок задания; секретный prompt доступен worker, но исключён из status."""

    id: str
    document_id: str
    owner: str
    filename: str
    document_pages: int
    selected_pdf_pages: list[int]
    project_code: str
    prompt: str
    prompt_version: str
    fingerprint: str
    idempotency_key: str | None = None
    state: JobState = JobState.QUEUED
    pages: list[PageAnalysis]
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    current_pdf_page: int | None = None
    cancel_requested: bool = False
    lease_token: str | None = None
    lease_until: datetime | None = None
    event_seq: int = 0
    error_code: str | None = None
    revision: int = 0
    pending_events: list[Event] = Field(default_factory=list, exclude=True)

    def emit(self, event_type: str, **data) -> None:
        """Добавляет событие в ту же транзакцию, что и изменение состояния."""
        self.event_seq += 1
        self.pending_events.append(
            Event(
                id=self.event_seq,
                type=event_type,
                data={
                    "revision": self.revision,
                    **data,
                },
            )
        )

    def transition(self, target: JobState) -> None:
        """Проверяет переход состояния; терминальный job запускается только через retry."""
        allowed = {
            JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED, JobState.FAILED},
            JobState.RUNNING: TERMINAL,
        }
        if target != self.state and target not in allowed.get(self.state, set()):
            raise ValueError(f"Недопустимый переход {self.state} → {target}")
        self.state = target
        self.updated_at = utcnow()

    def export(self) -> Export:
        """Собирает результат в физическом порядке без удаления одинаковых товаров."""
        return Export(items=[e.item for page in self.pages for e in page.items])


class Session(Contract):
    """Анонимный владелец документов по хэшу cookie с фиксированным TTL и CSRF token."""

    token_hash: str
    csrf: str
    prompt: str | None = None
    expires_at: datetime


class Document(Contract):
    """Приватный PDF для предпросмотра и нескольких независимых запусков."""

    id: str
    owner: str
    filename: str
    page_count: int
    sha256: str
    suggested_project_code: str | None = None
    expires_at: datetime
