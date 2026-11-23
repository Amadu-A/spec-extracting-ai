# app/application/invoices/ports.py
"""Дополнительные узкие порты загрузки изображений и финансовой книги."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from app.application.ports import FileStorage, PdfReader
from app.domain.invoices.models import InvoiceExport


@dataclass
class Upload:
    """Один файл транспорта без зависимости от FastAPI UploadFile."""

    filename: str
    media_type: str
    chunks: AsyncIterator[bytes]


class InvoiceStorage(FileStorage, Protocol):
    """Общий UUID storage с явным разрешением изображений только новому профилю."""

    async def write_source(self, document_id: str, chunks: AsyncIterator[bytes], limit: int) -> str:
        """Сохраняет оригинал PDF/JPEG/PNG и возвращает хэш исходных байт."""
        ...


class SourcePages(PdfReader, Protocol):
    """Расширяет preflight проверкой MIME и нормализацией одностраничного изображения."""

    async def inspect(self, path: str, media_type: str = "application/octet-stream") -> dict:
        """Не доверяет расширению и ограничивает геометрию до декодирования."""
        ...


class InvoiceSpreadsheet(Protocol):
    """Экспортирует подтверждённый финансовый DTO независимо от HTTP."""

    async def excel(self, result: InvoiceExport, warnings: list[dict]) -> bytes:
        """Создаёт три листа с безопасным текстом и Decimal форматированием."""
        ...
