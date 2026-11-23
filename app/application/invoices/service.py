# app/application/invoices/service.py
"""Пакетная загрузка/управление счетами с owner, TTL и immutable prompt snapshots."""

import hashlib
import json
import re
from datetime import timedelta
from uuid import uuid4

from app.application.invoices.ports import InvoiceSpreadsheet, InvoiceStorage, SourcePages, Upload
from app.application.ports import JobRepository
from app.application.service import ExtractionService
from app.core.observability import timed
from app.core.settings import Settings
from app.domain.errors import ServiceError
from app.domain.invoices.models import Checkpoint, InvoiceJob, Source
from app.domain.models import SUCCESS, TERMINAL, Document, JobState, PageAnalysis, utcnow
from app.domain.pages import parse_pages


class InvoiceService(ExtractionService):
    """Использует общий cancel/TTL, сохраняя раздельные status/result/retry контрактов."""

    def __init__(
        self,
        repository: JobRepository,
        storage: InvoiceStorage,
        pages: SourcePages,
        settings: Settings,
        prompt: str,
        reconcile_prompt: str,
        spreadsheet: InvoiceSpreadsheet,
    ):
        """Все адаптеры и ресурсы получает только из composition root."""
        super().__init__(repository, storage, pages, settings, prompt)
        self.reconcile_prompt, self.spreadsheet = reconcile_prompt, spreadsheet

    @timed("create_invoice_extraction")
    async def create(
        self,
        owner: str,
        uploads: list[Upload],
        selection: dict[str, str],
        key: str | None,
        session_prompt: str | None = None,
    ) -> InvoiceJob:
        """Сохраняет все файлы до admission; при отказе не оставляет неучтённые uploads."""
        if not 1 <= len(uploads) <= self.settings.invoice_max_files:
            raise ServiceError(
                "invoice_file_limit", "Превышен допустимый размер пакета файлов", 413
            )
        if set(selection) - {str(i) for i in range(len(uploads))}:
            raise ServiceError(
                "page_selection", "Индексы page_selection должны соответствовать файлам"
            )
        if key and (len(key) > 190 or any(ord(c) < 32 for c in key)):
            raise ServiceError("invalid_idempotency_key", "Некорректный Idempotency-Key")
        saved, total_bytes = [], 0
        active_expiry = utcnow() + timedelta(
            seconds=self.settings.job_timeout_seconds + self.settings.retention_seconds
        )
        try:
            for upload in uploads:
                document_id = str(uuid4())
                saved.append(document_id)

                async def chunks(upload=upload):
                    """Считает суммарный размер потоково, не складывая весь batch в память."""
                    nonlocal total_bytes
                    async for chunk in upload.chunks:
                        total_bytes += len(chunk)
                        if total_bytes > self.settings.invoice_max_batch_mb * 1024 * 1024:
                            raise ServiceError(
                                "invoice_batch_limit", "Превышен лимит размера пакета", 413
                            )
                        yield chunk

                digest = await self.storage.write_source(
                    document_id, chunks(), self.settings.max_upload_mb * 1024 * 1024
                )
                info = await self.pdf.inspect(self.storage.path(document_id), upload.media_type)
                filename = (
                    re.sub(
                        r"[\x00-\x1f\x7f]", "", upload.filename.replace("\\", "/").split("/")[-1]
                    )[:180]
                    or "document"
                )
                document = Document(
                    id=document_id,
                    owner=owner,
                    filename=filename,
                    page_count=info["page_count"],
                    sha256=digest,
                    expires_at=active_expiry,
                )
                await self.repository.save_document(document)
                if len(saved) == 1:
                    sources = []
                chosen = parse_pages(
                    selection.get(str(len(saved) - 1), "") or f"1-{document.page_count}",
                    document.page_count,
                    self.settings.max_selected_pages,
                )
                sources.append(
                    Source(
                        document_id=document_id,
                        filename=filename,
                        sha256=digest,
                        page_count=document.page_count,
                        selected_pages=chosen,
                        media_type=upload.media_type,
                    )
                )
            if sum(len(s.selected_pages) for s in sources) > self.settings.max_selected_pages:
                raise ServiceError("page_limit", "Суммарный лимит страниц пакета превышен", 413)
            prompt = session_prompt if session_prompt is not None else self.default_prompt
            version = hashlib.sha256((prompt + "\0" + self.reconcile_prompt).encode()).hexdigest()
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        "invoice",
                        [(s.sha256, s.filename, s.selected_pages) for s in sources],
                        version,
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            checkpoints = [
                Checkpoint(file_index=i, pdf_page=p)
                for i, source in enumerate(sources)
                for p in source.selected_pages
            ]
            job = InvoiceJob(
                id=str(uuid4()),
                document_id=sources[0].document_id,
                owner=owner,
                filename=sources[0].filename,
                document_pages=sum(s.page_count for s in sources),
                selected_pdf_pages=[c.pdf_page for c in checkpoints],
                project_code="",
                prompt=prompt,
                prompt_version=version,
                fingerprint=fingerprint,
                idempotency_key="invoice:" + key if key else None,
                pages=[PageAnalysis(pdf_page=c.pdf_page) for c in checkpoints],
                expires_at=active_expiry,
                sources=sources,
                checkpoints=checkpoints,
                reconcile_prompt=self.reconcile_prompt,
            )
            job.emit("job_queued")
            result = await self.repository.create(job, self.settings.max_queued_jobs)
            if result.expires_at <= utcnow():
                raise ServiceError("job_expired", "Истёк срок хранения идемпотентного задания", 410)
            if result.id != job.id:
                for document_id in saved:
                    await self.storage.delete(document_id)
            return result
        except BaseException:
            for document_id in saved:
                await self.storage.delete(document_id)
            raise

    async def owned(self, job_id: str, owner: str) -> InvoiceJob:
        """Чужой owner или другой профиль скрывается одинаковым 404."""
        job = await self.repository.get(job_id)
        if not isinstance(job, InvoiceJob) or job.owner != owner:
            raise ServiceError("job_not_found", "Задание не найдено", 404)
        if job.expires_at <= utcnow():
            raise ServiceError("job_expired", "Срок хранения задания истёк", 410)
        return job

    def status(self, job: InvoiceJob) -> dict:
        """Безопасный durable прогресс без prompt/lease и незавершённого результата."""
        processed = sum(c.extraction is not None for c in job.checkpoints)
        base = f"/api/v1/invoice-extractions/{job.id}"
        return dict(
            job_id=job.id,
            status=job.state.value,
            revision=job.revision,
            prompt_version=job.prompt_version,
            current_file_index=job.current_file_index,
            current_pdf_page=job.current_pdf_page,
            processed_pages=processed,
            total_pages=len(job.pages),
            progress=round(100 * processed / len(job.pages), 2),
            file_count=len(job.sources),
            document_count=job.result.summary.document_count if job.result else 0,
            item_count=job.result.summary.item_count
            if job.result
            else sum(
                len(d.rows) for c in job.checkpoints if c.extraction for d in c.extraction.documents
            ),
            warning_count=len(self.warnings(job)),
            error_code=job.error_code,
            last_event_id=job.event_seq,
            expires_at=job.expires_at.isoformat(),
            created_at=job.created_at.isoformat(),
            updated_at=job.updated_at.isoformat(),
            files=[
                dict(
                    file_index=i,
                    source_file=s.filename,
                    page_count=s.page_count,
                    selected_pages=s.selected_pages,
                    pages=[
                        dict(
                            pdf_page=c.pdf_page,
                            state=job.pages[n].state,
                            attempts=job.pages[n].attempts,
                            stages=[stage.model_dump(mode="json") for stage in job.pages[n].stages],
                        )
                        for n, c in enumerate(job.checkpoints)
                        if c.file_index == i
                    ],
                )
                for i, s in enumerate(job.sources)
            ],
            links=dict(
                status=base,
                events=base + "/events",
                warnings=base + "/warnings",
                snapshot=base + "/snapshot",
                json=base + "/result.json" if job.state in SUCCESS else None,
                xlsx=base + "/result.xlsx" if job.state in SUCCESS else None,
            ),
        )

    def warnings(self, job: InvoiceJob) -> list[dict]:
        """Привязывает ошибки lifecycle и финансов к файлу/листу/документу."""
        return [
            *job.diagnostics,
            *(
                dict(
                    file_index=c.file_index,
                    document_index=None,
                    row_number=None,
                    **w.model_dump(mode="json"),
                )
                for c, p in zip(job.checkpoints, job.pages, strict=True)
                for w in p.warnings
            ),
        ]

    async def preview_file(
        self, job_id: str, owner: str, file_index: int, page: int, width: int
    ) -> bytes:
        """Защищает предпросмотр любого исходного листа общими owner/TTL правилами."""
        job = await self.owned(job_id, owner)
        if not 0 <= file_index < len(job.sources):
            raise ServiceError("file_not_found", "Файл не найден", 404)
        source = job.sources[file_index]
        return await self.preview(source.document_id, owner, page, width)

    @timed("retry_invoice_page")
    async def retry_file(self, job_id: str, owner: str, file_index: int, page: int) -> InvoiceJob:
        """Повторяет зависимый суффикс только внутри выбранного файла, меняет ревизию."""
        await self.owned(job_id, owner)

        def change(job):
            """Инвалидирует финальный batch в одной транзакции с отзывом lease."""
            if job.state not in TERMINAL:
                raise ServiceError("job_busy", "Дождитесь завершения или отмены задания", 409)
            indices = [i for i, c in enumerate(job.checkpoints) if c.file_index == file_index]
            index = next((i for i in indices if job.checkpoints[i].pdf_page == page), None)
            if index is None:
                raise ServiceError("page_not_selected", "Лист не выбран для анализа")
            previous = index - 1
            if previous in indices and job.checkpoints[previous].pdf_page == page - 1:
                index = previous
            requested = next(i for i in indices if job.checkpoints[i].pdf_page == page)
            if job.checkpoints[requested].retry_count >= self.settings.invoice_max_page_retries:
                raise ServiceError(
                    "retry_limit", "Достигнут лимит повторных обработок страницы", 409
                )
            for i in indices:
                if i >= index:
                    old = job.pages[i]
                    job.pages[i] = PageAnalysis(pdf_page=old.pdf_page, attempts=old.attempts)
                    job.checkpoints[i].extraction = None
                    job.checkpoints[i].retry_count += 1
            job.result, job.diagnostics = None, []
            job.revision += 1
            job.state, job.cancel_requested, job.error_code = JobState.QUEUED, False, None
            job.current_file_index, job.current_pdf_page = file_index, None
            job.lease_token, job.lease_until = None, None
            job.expires_at = utcnow() + timedelta(
                seconds=self.settings.job_timeout_seconds + self.settings.retention_seconds
            )
            job.emit(
                "result_invalidated",
                file_index=file_index,
                from_pdf_page=job.checkpoints[index].pdf_page,
            )
            job.emit("job_queued")

        return await self.repository.edit(job_id, change)

    async def result(self, job_id: str, owner: str):
        """Partial доступен только в диагностике; успешный экспорт атомарен для всего batch."""
        job = await self.owned(job_id, owner)
        if job.state not in SUCCESS or job.result is None:
            raise ServiceError("result_not_ready", "Полный результат ещё не готов", 409)
        return job

    @timed("export_invoice_excel")
    async def excel(self, job_id: str, owner: str) -> bytes:
        """Передаёт единственный подтверждённый DTO заменяемому адаптеру Excel."""
        job = await self.result(job_id, owner)
        return await self.spreadsheet.excel(job.result, self.warnings(job))

    @timed("manage_invoice_prompt")
    async def prompt(self, session, value: str | None = None, restore=False) -> dict:
        """Сессионный invoice prompt изолирован от legacy prompt; running snapshot неизменен."""
        if value is not None and (not value.strip() or len(value) > 20000):
            raise ServiceError("invalid_prompt", "Промпт должен содержать 1–20000 символов")
        if value is not None or restore:

            def update(current):
                """Меняет только invoice_prompt, сохраняя параллельные правки спецификаций."""
                current.invoice_prompt = None if restore else value

            session = await self.repository.edit_session(session.token_hash, update)
        effective = (
            session.invoice_prompt if session.invoice_prompt is not None else self.default_prompt
        )
        return dict(
            prompt=effective,
            is_default=session.invoice_prompt is None,
            version=hashlib.sha256((effective + "\0" + self.reconcile_prompt).encode()).hexdigest(),
            expires_at=session.expires_at.isoformat(),
        )
