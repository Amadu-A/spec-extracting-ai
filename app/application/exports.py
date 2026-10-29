# app/application/exports.py
"""Use-case экспорта не допускает выдачу partial/cancelled за полный результат."""

import asyncio
import json
from time import perf_counter

from app.application.ports import SpreadsheetExporter
from app.application.service import ExtractionService
from app.core.observability import timed
from app.domain.errors import ServiceError
from app.domain.models import SUCCESS, Job, Stage, utcnow


class ExportService:
    """Использует тот же набор записей для JSON и Excel, без повторного inference."""

    def __init__(self, extractions: ExtractionService, exporter: SpreadsheetExporter):
        """Получает application-проверки owner и инфраструктурный exporter через порт."""
        self.extractions, self.exporter = extractions, exporter

    async def ready(self, job_id: str, owner: str) -> Job:
        """Проверяет доступ и успешное терминальное состояние до сериализации."""
        job = await self.extractions.owned(job_id, owner)
        if job.state not in SUCCESS:
            raise ServiceError("result_not_ready", "Полный результат ещё не готов", 409)
        return job

    @timed("export_excel")
    async def excel(self, job_id: str, owner: str) -> bytes:
        """Возвращает строковые ячейки и отдельный лист реальных предупреждений."""
        job = await self.ready(job_id, owner)
        started_at, started = utcnow(), perf_counter()
        data = await self.exporter.excel(job.export(), [w for p in job.pages for w in p.warnings])
        await self._record(job, "Экспорт Excel", started_at, (perf_counter() - started) * 1000)
        return data

    @timed("export_json")
    async def json_bytes(self, job_id: str, owner: str) -> bytes:
        """Сериализует тот же Export DTO и измеряет фактическую генерацию файла."""
        job = await self.ready(job_id, owner)
        started_at, started = utcnow(), perf_counter()

        def serialize() -> bytes:
            """Выносит большую JSON-сериализацию из HTTP event loop."""
            return json.dumps(job.export().model_dump(), ensure_ascii=False, indent=2).encode()

        data = await asyncio.to_thread(serialize)
        await self._record(job, "Экспорт JSON", started_at, (perf_counter() - started) * 1000)
        return data

    async def _record(self, snapshot: Job, name: str, started_at, duration_ms: float) -> None:
        """Не отдаёт устаревший результат при параллельном retry; журнал ограничен десятью экспортами."""

        def change(current: Job) -> None:
            """Добавляет измеренный этап, сохраняя содержимое и срок жизни экспортного результата."""
            if current.revision != snapshot.revision or current.state not in SUCCESS:
                raise ServiceError(
                    "result_changed", "Результат изменился во время экспорта, повторите запрос", 409
                )
            if current.expires_at <= utcnow():
                raise ServiceError("job_expired", "Срок хранения задания истёк", 410)
            stages = current.pages[-1].stages
            export_indices = [
                i for i, stage in enumerate(stages) if stage.name.startswith("Экспорт ")
            ]
            if len(export_indices) >= 10:
                del stages[export_indices[0]]
            stages.append(
                Stage(
                    name=name,
                    state="completed",
                    started_at=started_at,
                    duration_ms=round(duration_ms, 2),
                )
            )

        await self.extractions.repository.edit(snapshot.id, change)
