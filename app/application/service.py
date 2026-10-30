# app/application/service.py
"""Use-case загрузки, запуска и управления заданиями; едины для Web и интеграций.

Операции проверяют owner/TTL до обращения к файлам. Durable очередь основана на
сохранённом queued-состоянии, поэтому сбой API после commit не теряет задание.
"""

import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

from app.application.ports import FileStorage, JobRepository, PdfReader
from app.core.observability import timed
from app.core.settings import Settings
from app.domain.errors import ServiceError
from app.domain.models import SUCCESS, TERMINAL, Document, Job, JobState, PageAnalysis, utcnow
from app.domain.pages import parse_pages


class ExtractionService:
    """Оркестрирует публичные действия через порты, не создавая инфраструктуру."""

    def __init__(
        self,
        repository: JobRepository,
        storage: FileStorage,
        pdf: PdfReader,
        settings: Settings,
        default_prompt: str,
    ):
        """Получает зависимости в composition root; prompt ресурса неизменяем."""
        self.repository, self.storage, self.pdf = repository, storage, pdf
        self.settings, self.default_prompt = settings, default_prompt

    @timed("upload_document")
    async def upload(
        self, owner: str, filename: str, content_type: str, chunks: AsyncIterator[bytes]
    ) -> Document:
        """Потоково сохраняет и проверяет PDF, удаляет файл при отказе preflight."""
        if content_type not in ("application/pdf", "application/octet-stream"):
            raise ServiceError("unsupported_media_type", "Принимается только PDF", 415)
        document_id = str(uuid4())
        digest = await self.storage.write(
            document_id, chunks, self.settings.max_upload_mb * 1024 * 1024
        )
        try:
            info = await self.pdf.inspect(self.storage.path(document_id))
            safe_name = filename.replace("\\", "/").split("/")[-1]
            safe_name = re.sub(r"[\x00-\x1f\x7f]", "", safe_name)[:180] or "document.pdf"
            document = Document(
                id=document_id,
                owner=owner,
                filename=safe_name,
                page_count=info["page_count"],
                sha256=digest,
                suggested_project_code=info.get("suggested_project_code"),
                expires_at=utcnow() + timedelta(seconds=self.settings.retention_seconds),
            )
            await self.repository.save_document(document)
            return document
        except BaseException:
            await self.storage.delete(document_id)
            raise

    async def document(self, document_id: str, owner: str) -> Document:
        """Скрывает существование чужих PDF и проверяет срок хранения."""
        document = await self.repository.document(document_id)
        if not document or document.owner != owner:
            raise ServiceError("document_not_found", "Документ не найден", 404)
        if document.expires_at <= utcnow():
            raise ServiceError("document_expired", "Срок хранения PDF истёк", 410)
        return document

    @timed("start_extraction")
    async def start(
        self,
        owner: str,
        document_id: str,
        pages: str,
        project_code: str,
        idempotency_key: str | None = None,
        session_prompt: str | None = None,
    ) -> Job:
        """Создаёт immutable snapshot и возвращает queued без ожидания модели.

        project_code должен быть явно подтверждён клиентом; автоопределение в
        документе является предложением, особенно для сканов с ошибочным OCR.
        """
        document = await self.document(document_id, owner)
        selected = parse_pages(pages, document.page_count, self.settings.max_selected_pages)
        project_code = project_code.strip()
        if not project_code or len(project_code) > 200 or any(ord(c) < 32 for c in project_code):
            raise ServiceError(
                "project_code_unconfirmed", "Подтвердите шифр проекта (до 200 символов)"
            )
        if idempotency_key and (
            len(idempotency_key) > 200 or any(ord(c) < 32 for c in idempotency_key)
        ):
            raise ServiceError("invalid_idempotency_key", "Некорректный Idempotency-Key")
        prompt = session_prompt if session_prompt is not None else self.default_prompt
        version = hashlib.sha256(prompt.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                [document.sha256, selected, project_code, version], ensure_ascii=False
            ).encode()
        ).hexdigest()
        job = Job(
            id=str(uuid4()),
            document_id=document.id,
            owner=owner,
            filename=document.filename,
            document_pages=document.page_count,
            selected_pdf_pages=selected,
            project_code=project_code,
            prompt=prompt,
            prompt_version=version,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            pages=[PageAnalysis(pdf_page=p) for p in selected],
            expires_at=utcnow()
            + timedelta(
                seconds=self.settings.job_timeout_seconds + self.settings.retention_seconds
            ),
        )
        job.emit("job_queued", selected_pdf_pages=selected)
        return await self.repository.create(job, self.settings.max_queued_jobs)

    async def owned(self, job_id: str, owner: str) -> Job:
        """Единая проверка owner и TTL для status/SSE/preview/exports."""
        job = await self.repository.get(job_id)
        if not job or job.owner != owner:
            raise ServiceError("job_not_found", "Задание не найдено", 404)
        if job.expires_at <= utcnow():
            raise ServiceError("job_expired", "Срок хранения задания истёк", 410)
        return job

    def status(self, job: Job) -> dict:
        """Формирует безопасные метаданные; prompt и lease наружу не выдаются."""
        processed = sum(p.state in ("completed", "completed_with_warnings") for p in job.pages)
        base = f"/api/v1/extractions/{job.id}"
        return {
            "job_id": job.id,
            "status": job.state.value,
            "filename": job.filename,
            "document_id": job.document_id,
            "document_pages": job.document_pages,
            "project_code": job.project_code,
            "selected_pdf_pages": job.selected_pdf_pages,
            "current_pdf_page": job.current_pdf_page,
            "processed_pages": processed,
            "total_pages": len(job.pages),
            "progress": round(100 * processed / len(job.pages), 2),
            "item_count": sum(len(p.items) for p in job.pages),
            "warning_count": sum(len(p.warnings) for p in job.pages),
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "expires_at": job.expires_at.isoformat(),
            "last_event_id": job.event_seq,
            "revision": job.revision,
            "prompt_version": job.prompt_version,
            "error_code": job.error_code,
            "cancel_requested": job.cancel_requested,
            "pages": [
                {
                    "pdf_page": p.pdf_page,
                    "state": p.state,
                    "attempts": p.attempts,
                    "item_count": len(p.items),
                    "warning_count": len(p.warnings),
                    "stages": [stage.model_dump(mode="json") for stage in p.stages],
                }
                for p in job.pages
            ],
            "links": {
                "status": base,
                "events": base + "/events",
                "warnings": base + "/warnings",
                "json": base + "/result.json" if job.state in SUCCESS else None,
                "xlsx": base + "/result.xlsx" if job.state in SUCCESS else None,
            },
        }

    @timed("cancel_extraction")
    async def cancel(self, job_id: str, owner: str) -> Job:
        """Отмена идемпотентна; подтверждённые позиции остаются в diagnostics."""
        await self.owned(job_id, owner)

        def change(job: Job) -> None:
            """Ставит флаг под row lock; queued отменяется до вызова worker."""
            if job.state in TERMINAL:
                return
            job.cancel_requested = True
            if job.state == JobState.QUEUED:
                job.transition(JobState.CANCELLED)
                job.expires_at = utcnow() + timedelta(seconds=self.settings.retention_seconds)
                job.emit("job_cancelled")
            else:
                job.emit("cancellation_requested")

        return await self.repository.edit(job_id, change)

    @timed("retry_page")
    async def retry(self, job_id: str, owner: str, pdf_page: int) -> Job:
        """Инвалидирует страницу и зависимый суффикс; результат не смешивает ревизии.

        Восстановление начинается с предыдущего соседнего листа: это сохраняет
        строку, которая могла начаться на нём. Следующие страницы повторяются,
        потому что их группы/переносы могли зависеть от изменённого ответа.
        """
        await self.owned(job_id, owner)

        def change(job: Job) -> None:
            """Проверяет актуальное состояние внутри транзакции, исключая гонку retry."""
            if job.state not in TERMINAL:
                raise ServiceError("job_busy", "Дождитесь завершения или отмены задания", 409)
            if pdf_page not in job.selected_pdf_pages:
                raise ServiceError("page_not_selected", "Страница не выбрана для анализа")
            index = job.selected_pdf_pages.index(pdf_page)
            if index and job.selected_pdf_pages[index - 1] == pdf_page - 1:
                index -= 1
            for i in range(index, len(job.pages)):
                job.pages[i] = PageAnalysis(pdf_page=job.pages[i].pdf_page)
            job.revision += 1
            job.state, job.cancel_requested, job.current_pdf_page = JobState.QUEUED, False, None
            job.lease_token, job.lease_until, job.error_code = None, None, None
            job.expires_at = utcnow() + timedelta(
                seconds=self.settings.job_timeout_seconds + self.settings.retention_seconds
            )
            job.emit("result_invalidated", from_pdf_page=job.pages[index].pdf_page)
            job.emit("job_queued", selected_pdf_pages=job.selected_pdf_pages)

        return await self.repository.edit(job_id, change)

    async def purge_expired(self) -> None:
        """Завершает просроченные задания и удаляет истёкшие данные независимо от браузера."""
        now = utcnow()
        retention = timedelta(seconds=self.settings.retention_seconds)
        for job_id in await self.repository.overdue_jobs(now + retention):

            def fail(job: Job) -> None:
                """Повторно проверяет deadline под lock и отзывает lease старого worker."""
                if job.state in TERMINAL or job.expires_at - retention > now:
                    return
                job.transition(JobState.FAILED)
                job.error_code = "job_timeout"
                job.lease_token, job.lease_until = None, None
                job.expires_at = now + retention
                for page in job.pages:
                    if page.state == "running":
                        page.state = "failed"
                    for stage in page.stages:
                        if stage.state == "running":
                            stage.state = "interrupted"
                job.emit("job_failed", status="failed", error_code="job_timeout")

            await self.repository.edit(job_id, fail)
        for document_id in await self.repository.cleanup():
            await self.storage.delete(document_id)
        await self.storage.cleanup_orphans(
            self.repository.document, now.timestamp() - self.settings.retention_seconds
        )

    async def preview(self, document_id: str, owner: str, page: int, width: int) -> bytes:
        """Проверяет owner/TTL даже при попадании изображения в кэш адаптера."""
        document = await self.document(document_id, owner)
        if not 1 <= page <= document.page_count:
            raise ServiceError("pages_out_of_bounds", "Страница вне PDF")
        return await self.pdf.preview(self.storage.path(document_id), page, width)
