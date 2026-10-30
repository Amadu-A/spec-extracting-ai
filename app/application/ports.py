# app/application/ports.py
"""Минимальные заменяемые интерфейсы хранения, PDF, очереди и VLM."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.domain.models import Document, Event, Export, Job, Session, Stage, Warning


@dataclass
class PreparedPage:
    """Локально извлечённый текст, геометрия и ограниченные растры одного листа."""

    text: str
    blocks: list[dict]
    images: list[bytes]
    warnings: list[str]
    stages: list[Stage] = field(default_factory=list)


class JobRepository(Protocol):
    """Каждый edit атомарно сохраняет снимок и события под блокировкой job."""

    async def create(self, job: Job, capacity: int) -> Job:
        """Атомарно проверяет ёмкость и owner-scoped идемпотентность."""
        ...

    async def get(self, job_id: str) -> Job | None:
        """Читает приватный снимок; owner проверяется use-case."""
        ...

    async def edit(self, job_id: str, change: Callable[[Job], None]) -> Job:
        """Выполняет чистое изменение под блокировкой строки и общей транзакцией."""
        ...

    async def events(self, job_id: str, after: int, limit: int) -> list[Event]:
        """Возвращает ограниченный replay монотонных событий."""
        ...

    async def dispatchable(self, limit: int) -> list[str]:
        """Находит queued и просроченные running для повторной доставки."""
        ...

    async def save_document(self, document: Document) -> None:
        """Сохраняет owner и срок доступности загруженного PDF."""
        ...

    async def document(self, document_id: str) -> Document | None:
        """Читает метаданные документа без доступа к файловому пути."""
        ...

    async def session(self, token_hash: str) -> Session | None:
        """Читает серверную сессию по необратимому хэшу cookie."""
        ...

    async def overdue_jobs(self, deadline: datetime) -> list[str]:
        """Возвращает активные задания с истёкшим бюджетом выполнения для финализации."""
        ...

    async def save_session(self, session: Session) -> None:
        """Атомарно сохраняет prompt и фиксированный срок одной сессии."""
        ...

    async def delete_session(self, token_hash: str) -> None:
        """Немедленно отзывает cookie на сервере."""
        ...

    async def cleanup(self) -> list[str]:
        """Удаляет истёкшие метаданные и возвращает UUID файлов для удаления."""
        ...

    async def ping(self) -> bool:
        """Проверяет реальное соединение с БД."""
        ...


class FileStorage(Protocol):
    """Приватное хранилище принимает исключительно UUID, не имя пользователя."""

    async def write(self, document_id: str, chunks: AsyncIterator[bytes], limit: int) -> str:
        """Потоково сохраняет PDF с лимитом; возвращает SHA256 содержимого."""
        ...

    def path(self, document_id: str) -> str:
        """Разрешает безопасный абсолютный путь для локального PDF-адаптера."""
        ...

    async def delete(self, document_id: str) -> None:
        """Удаляет только файлы указанного UUID внутри корня приложения."""
        ...

    async def cleanup_orphans(
        self, exists: Callable[[str], Awaitable[Document | None]], older_than: float
    ) -> None:
        """Удаляет старые файлы без метаданных после сбоя загрузки или очистки."""
        ...


class PdfReader(Protocol):
    """Порт формата документа; будущий Word-адаптер не меняет extraction rules."""

    async def inspect(self, path: str) -> dict:
        """Проверяет структуру, пароль и лимиты, предлагает код проекта."""
        ...

    async def prepare(self, path: str, pdf_page: int) -> PreparedPage:
        """Извлекает текст и изображения с геометрией для VLM."""
        ...

    async def preview(self, path: str, pdf_page: int, width: int) -> bytes:
        """Рендерит ограниченный PNG для авторизованного просмотра."""
        ...


class ModelClient(Protocol):
    """Поток модели не является финальным экспортом до полной валидации."""

    def stream(self, page: PreparedPage, prompt: str, context: dict) -> AsyncIterator[str]:
        """Передаёт локальные изображения и контекст только в shared-vlm."""
        ...

    async def ping(self) -> bool:
        """Проверяет доступность логического alias модели."""
        ...


class TaskPublisher(Protocol):
    """Durable доставка UUID; payload задания и prompt остаются в БД."""

    async def publish(self, job_id: str) -> None:
        """Подтверждает публикацию в существующий app-vhost RabbitMQ."""
        ...

    async def ping(self) -> bool:
        """Проверяет доступ к очереди существующим app-пользователем."""
        ...


class SpreadsheetExporter(Protocol):
    """Порт бинарного экспорта; application не зависит от openpyxl."""

    async def excel(self, result: Export, warnings: list[Warning]) -> bytes:
        """Создаёт Excel, эквивалентный одному подтверждённому Export DTO."""
        ...
