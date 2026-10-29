# app/infrastructure/pdf.py
"""Async-порт PDF запускает парсер в убиваемом subprocess с конечным timeout."""

import asyncio
import base64
import json
import sys
from collections import OrderedDict

from app.application.ports import PreparedPage
from app.core.settings import Settings
from app.domain.errors import ServiceError
from app.domain.models import Stage


class IsolatedPdfReader:
    """Ограничивает concurrency native parsing, память preview-cache и размеры PNG."""

    def __init__(self, settings: Settings):
        """Применяет общие Settings без чтения os.environ в адаптере."""
        self.settings = settings
        self._slots = asyncio.Semaphore(2)
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()

    async def _call(self, path: str, operation: str, **extra) -> dict:
        """Убивает parser при timeout; лимиты действуют до создания больших bitmap."""
        payload = {
            "path": path,
            "operation": operation,
            "max_pages": self.settings.max_pdf_pages,
            "max_pixels": self.settings.max_image_pixels,
            "dpi": self.settings.render_dpi,
            "max_page_points": self.settings.max_page_points,
            **extra,
        }
        async with self._slots:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.infrastructure.pdf_task",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(json.dumps(payload).encode()), 45
                )
            except BaseException as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(exc, TimeoutError):
                    raise ServiceError(
                        "pdf_timeout", "Превышено время безопасного разбора PDF"
                    ) from exc
                raise
        if process.returncode or len(output) > 80_000_000:
            raise ServiceError(
                "pdf_processing_failed", "Парсер PDF превысил безопасные ограничения"
            )
        try:
            data = json.loads(output)
        except ValueError as exc:
            raise ServiceError("pdf_processing_failed", "Не получен результат парсера PDF") from exc
        if "error" in data:
            raise ServiceError(data["error"], data["message"])
        return data

    async def inspect(self, path: str) -> dict:
        """Выполняет безопасный preflight PDF до постановки задания в очередь."""
        return await self._call(path, "inspect")

    async def prepare(self, path: str, pdf_page: int) -> PreparedPage:
        """Готовит весь физический лист, текст и при необходимости детальные crops."""
        data = await self._call(path, "prepare", pdf_page=pdf_page)
        return PreparedPage(
            text=data["text"],
            blocks=data["blocks"],
            warnings=data["warnings"],
            images=[base64.b64decode(image) for image in data["images"]],
            stages=[Stage.model_validate(stage) for stage in data.get("stages", [])],
        )

    async def preview(self, path: str, pdf_page: int, width: int) -> bytes:
        """Ограничивает preview-cache 24 изображениями и очищает его при превышении 32MB."""
        key = (path, pdf_page, width)
        if key not in self._cache:
            data = await self._call(path, "preview", pdf_page=pdf_page, width=width)
            self._cache[key] = base64.b64decode(data["image"])
            while len(self._cache) > 24 or sum(map(len, self._cache.values())) > 32_000_000:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return self._cache[key]
