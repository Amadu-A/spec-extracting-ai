# app/infrastructure/invoices/pages.py
"""Приводит изображения к безопасному PDF в существующем изолированном парсере."""

import asyncio
from pathlib import Path

from app.domain.errors import ServiceError


class InvoicePages:
    """Переиспользует общий PDF reader и его ограничения вместо второго движка OCR."""

    def __init__(self, pdf):
        """Получает один общий адаптер из composition root."""
        self.pdf = pdf

    async def inspect(self, path: str, media_type: str = "application/octet-stream") -> dict:
        """Проверяет MIME по содержимому до конвертации; оригинал не перезаписывает."""
        signature = await asyncio.to_thread(self._signature, path)
        actual = (
            "application/pdf"
            if signature.startswith(b"%PDF-")
            else (
                "image/png"
                if signature.startswith(b"\x89PNG\r\n\x1a\n")
                else "image/jpeg"
                if signature.startswith(b"\xff\xd8\xff")
                else None
            )
        )
        if actual is None or media_type not in (actual, "application/octet-stream"):
            raise ServiceError(
                "unsupported_media_type", "MIME не соответствует содержимому PDF/JPEG/PNG", 415
            )
        if actual != "application/pdf":
            await self.pdf._call(path, "normalize_image")
        return await self.pdf.inspect(await self._path(path))

    @staticmethod
    def _signature(path: str) -> bytes:
        """Читает только заголовок локального UUID-файла."""
        with open(path, "rb") as stream:
            return stream.read(8)

    async def _path(self, path: str) -> str:
        """Находит безопасную нормализованную копию только внутри того же UUID-каталога."""
        normalized = Path(path).with_name("normalized.pdf")
        return str(normalized) if await asyncio.to_thread(normalized.is_file) else path

    async def prepare(self, path: str, pdf_page: int):
        """Общий renderer сохраняет физическую страницу и видимые повороты."""
        return await self.pdf.prepare(await self._path(path), pdf_page, tables=True)

    async def preview(self, path: str, pdf_page: int, width: int):
        """Viewer использует ту же копию, которая была передана VLM."""
        return await self.pdf.preview(await self._path(path), pdf_page, width)
