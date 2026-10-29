# app/infrastructure/storage.py
"""Файловое хранилище с UUID-адресацией, лимитом загрузки и атомарной записью."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

from app.domain.errors import ServiceError


class LocalStorage:
    """Не использует пользовательское имя как путь и не раздаёт каталог через static."""

    def __init__(self, root: Path):
        """Закрепляет разрешённый корень приложения до обработки запросов."""
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, document_id: str) -> Path:
        """Проверяет canonical UUID и отсутствие symlink-выхода за корень."""
        try:
            if str(UUID(document_id)) != document_id:
                raise ValueError
        except ValueError as exc:
            raise ServiceError("invalid_id", "Некорректный идентификатор", 422) from exc
        path = (self.root / document_id).resolve()
        if path.parent != self.root or (self.root / document_id).is_symlink():
            raise ServiceError("invalid_path", "Недопустимый путь", 400)
        return path

    def path(self, document_id: str) -> str:
        """Возвращает только original.pdf внутри проверенного UUID-каталога."""
        path = self._directory(document_id) / "original.pdf"
        if path.is_symlink():
            raise ServiceError("invalid_path", "Недопустимый путь", 400)
        return str(path)

    async def write(self, document_id: str, chunks: AsyncIterator[bytes], limit: int) -> str:
        """Записывает поток без накопления PDF в памяти; при ошибке удаляет partial."""
        directory = self._directory(document_id)
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
        temporary = directory / "upload.part"
        digest, size, signature = hashlib.sha256(), 0, b""
        stream = await asyncio.to_thread(temporary.open, "xb")
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise ServiceError("upload_limit", "Превышен лимит загрузки PDF", 413)
                signature = (signature + chunk)[:5]
                digest.update(chunk)
                await asyncio.to_thread(stream.write, chunk)
            if signature != b"%PDF-":
                raise ServiceError("unsupported_file", "Ожидается файл с сигнатурой PDF", 415)
            await asyncio.to_thread(stream.close)
            await asyncio.to_thread(temporary.replace, directory / "original.pdf")
            return digest.hexdigest()
        except BaseException:
            await asyncio.to_thread(stream.close)
            await self.delete(document_id)
            raise

    async def delete(self, document_id: str) -> None:
        """Удаляет только известные файлы, не выполняя рекурсивного удаления."""
        directory = self._directory(document_id)

        def remove():
            """Не следует ссылкам и не удаляет неожиданные дочерние каталоги."""
            for name in ("upload.part", "original.pdf"):
                (directory / name).unlink(missing_ok=True)
            if directory.exists():
                directory.rmdir()

        await asyncio.to_thread(remove)

    async def cleanup_orphans(self, find_document, cutoff: float) -> None:
        """Удаляет забытые upload-каталоги после TTL, проверяя отсутствие метаданных."""
        for directory in await asyncio.to_thread(lambda: list(self.root.iterdir())):
            try:
                UUID(directory.name)
            except ValueError:
                continue
            if directory.is_symlink():
                continue
            stamp = await asyncio.to_thread(lambda path=directory: path.stat().st_mtime)
            if stamp < cutoff and await find_document(directory.name) is None:
                await self.delete(directory.name)
