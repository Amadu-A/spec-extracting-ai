# app/application/invoices/processor.py
"""Invoice-профиль общего worker: leases/отмена/stream/checkpoints без новой очереди."""

import asyncio

from pydantic import ValidationError

from app.application.processor import ExtractionProcessor
from app.domain.errors import ServiceError
from app.domain.invoices.evidence import (
    attachment_heading,
    document_heading,
    included_discounts,
    native_rows,
    needs_financial_review,
)
from app.domain.invoices.models import FinancialReview, InvoiceExport, InvoicePage
from app.domain.invoices.reconcile import explicit_vat_rates, reconcile_job, validate_file
from app.domain.models import JobState


class InvoiceProcessor(ExtractionProcessor):
    """Наследует проверенный lifecycle, переопределяя только обработку invoice-страниц."""

    async def _process(self, job_id: str, lease: str) -> None:
        """Дополняет ошибки финализации диагностикой, не меняя общий fencing/cancel lifecycle."""
        try:
            await self._run_pages(job_id, lease)
        except ServiceError as exc:

            def record(current, code=exc.code, message=exc.message):
                """Ошибку страницы запишет общий lifecycle; общая ошибка пакета получает отдельную причину."""
                self._guard(current, lease, check_cancel=False)
                if not current.cancel_requested and not any(
                    page.state == "running" for page in current.pages
                ):
                    current.diagnostics.append(
                        dict(
                            file_index=None,
                            pdf_page=None,
                            document_index=None,
                            row_number=None,
                            code=code,
                            message=message,
                        )
                    )

            await self.repository.edit(job_id, record)
            raise

    async def _run_pages(self, job_id: str, lease: str) -> None:
        """Возобновляет committed листы и атомарно публикует итог после всех файлов."""
        job = await self.repository.get(job_id)
        for index, checkpoint in enumerate(job.checkpoints):
            if checkpoint.extraction is not None:
                continue
            await self._page(job_id, lease, index)
        job = await self.repository.get(job_id)

        async def reconcile():
            """Сводит подтверждённые fragments детерминированно; второго VLM-вызова нет."""
            return reconcile_job(job, numeric=False)

        result, diagnostics = await self._stage(
            job_id, lease, len(job.pages) - 1, "document_reconcile", reconcile
        )
        if result.summary.item_count > self.settings.max_items:
            raise ServiceError("item_limit", "Превышен лимит позиций пакета")

        async def numbers():
            """Выполняет чистую Decimal-арифметику, не изменяя опубликованные значения."""
            return [warning for file in result.files for warning in validate_file(file)]

        diagnostics.extend(
            await self._stage(job_id, lease, len(job.pages) - 1, "numeric_validation", numbers)
        )

        async def export():
            """Проверяет сериализацию полного DTO до публикации успешных links."""

            def validate():
                """Повторно проверяет типы/ограничения после детерминированных изменений полей."""
                try:
                    return InvoiceExport.model_validate_json(result.model_dump_json())
                except ValueError as exc:
                    raise ServiceError(
                        "invoice_invalid_result",
                        "Исходные поля не соответствуют ограничениям результата; необходима проверка документа",
                        422,
                    ) from exc

            return await asyncio.to_thread(validate)

        result = await self._stage(job_id, lease, len(job.pages) - 1, "export", export)

        def commit(current):
            """Результат batch, проверки и события появляются в одной DB-транзакции."""
            current.result, current.diagnostics = result, diagnostics
            for file in result.files:
                for doc_index, document in enumerate(file.documents):
                    current.emit(
                        "document_validated",
                        file_index=file.file_index,
                        document_index=doc_index,
                        pdf_page=document.source_pages[0] if document.source_pages else None,
                        validation=document.validation.model_dump(mode="json"),
                        item_count=len(document.items),
                    )

        await self._edit(job_id, lease, commit)
        await self._finish(job_id, lease, JobState.WARNINGS if diagnostics else JobState.COMPLETED)

    async def _page(self, job_id: str, lease: str, index: int) -> None:
        """Рендерит один физический лист и подтверждает целиком валидный ответ модели."""
        job = await self.repository.get(job_id)
        checkpoint = job.checkpoints[index]
        source = job.sources[checkpoint.file_index]

        def begin(current):
            """Отмечает настоящий текущий файл и очищает незавершённую попытку."""
            current.current_file_index = checkpoint.file_index
            current.current_pdf_page = checkpoint.pdf_page
            current.pages[index].state = "running"
            current.pages[index].raw_text = ""
            for previous in current.pages:
                previous.raw_text = ""

        await self._edit(job_id, lease, begin)
        prepared = await self._stage(
            job_id,
            lease,
            index,
            "text_or_ocr",
            lambda: self.pdf.prepare(self.storage.path(source.document_id), checkpoint.pdf_page),
        )

        def timings(current):
            """Сохраняет реальные измерения из изолированного PDF-процесса."""
            current.pages[index].stages.extend(prepared.stages)

        await self._edit(job_id, lease, timings)
        previous = next(
            (
                c.extraction
                for c in reversed(job.checkpoints[:index])
                if c.file_index == checkpoint.file_index and c.pdf_page == checkpoint.pdf_page - 1
            ),
            None,
        )
        context = dict(
            file_index=checkpoint.file_index,
            pdf_page=checkpoint.pdf_page,
            page_count=source.page_count,
            previous_adjacent_page=previous.model_dump(mode="json") if previous else None,
            reconcile_instructions=job.reconcile_prompt,
            native_tables=prepared.tables,
            page_role_hint="attachment" if attachment_heading(prepared.text) else None,
            document_heading_hint=document_heading(prepared.text),
        )
        for attempt in range(self.settings.vlm_attempts):
            review = None
            try:

                def started(current, attempt=attempt):
                    """Автопереход viewer соответствует началу запроса модели, а не polling."""
                    current.current_pdf_page = checkpoint.pdf_page
                    current.pages[index].attempts += 1
                    current.pages[index].raw_text = ""
                    current.emit("page_started", attempt=attempt + 1)
                    current.emit("stream_reset")

                await self._edit(job_id, lease, started)
                raw = await self._stage(
                    job_id,
                    lease,
                    index,
                    "page_classification",
                    lambda: self._stream(job_id, lease, index, prepared, job.prompt, context),
                )
                try:
                    extraction = InvoicePage.model_validate_json(raw)
                except (ValidationError, ValueError) as exc:
                    raise ServiceError(
                        "invoice_invalid_json",
                        "Модель вернула неверную схему invoice-страницы",
                        502,
                    ) from exc
                if extraction.completeness != "complete":
                    raise ServiceError(
                        "invoice_uncertain_page",
                        "Модель не подтвердила полноту исходной страницы",
                        502,
                    )
                if attachment_heading(prepared.text):
                    # Подписанное соглашение может повторять товары счёта; это приложение.
                    extraction.role = "attachment"
                    for fragment in extraction.documents:
                        fragment.rows = []
                        fragment.document_type = "unknown"
                else:
                    heading = document_heading(prepared.text)
                    if heading and len(extraction.documents) == 1:
                        extraction.role = heading["document_type"]
                        for key, value in heading.items():
                            setattr(extraction.documents[0], key, value)
                    native = native_rows(prepared.tables)
                    count = sum(len(values) for values in native.values())
                    model_rows = [row for fragment in extraction.documents for row in fragment.rows]
                    if (
                        count
                        and count != len(model_rows)
                        and not any(row.continuation or row.continues_next for row in model_rows)
                    ):
                        raise ServiceError(
                            "invoice_row_count_mismatch",
                            "Число строк модели не совпало с исходной товарной таблицей PDF",
                            502,
                        )
                # Координаты источника назначает сервер, даже если модель указала иные.
                for fragment in extraction.documents:
                    fragment.source_pages, fragment.attached_pages = [], []
                    for row in fragment.rows:
                        row.item.source_pages = [checkpoint.pdf_page]
                if not prepared.tables and needs_financial_review(extraction):
                    review = await self._review(
                        job_id, lease, index, prepared, job.reconcile_prompt, context, extraction
                    )
                break
            except ServiceError as exc:
                if (
                    attempt + 1 == self.settings.vlm_attempts
                    or exc.code == "vlm_contract_unsupported"
                ):
                    raise
                context["previous_error"] = exc.code
                await asyncio.sleep(min(2 ** (attempt + 1), 15))

        def commit(current):
            """Checkpoint и диагностические строки сохраняются вместе, без premature экспорта."""
            count = sum(
                len(d.rows)
                for c in current.checkpoints
                if c.extraction
                for d in c.extraction.documents
            )
            count += sum(len(d.rows) for d in extraction.documents)
            if count > self.settings.max_items:
                raise ServiceError("item_limit", "Превышен лимит позиций пакета")
            if current.event_seq >= self.settings.max_events - 100:
                raise ServiceError("event_limit", "Превышен лимит журнала задания")
            current.checkpoints[index].extraction = extraction
            current.checkpoints[index].document_vat_rates = explicit_vat_rates(prepared.text)
            current.checkpoints[index].included_discount_amounts = included_discounts(prepared.text)
            current.checkpoints[index].native_tables = prepared.tables
            current.checkpoints[index].financial_review = review
            current.pages[index].state = "completed"
            current.emit(
                "page_completed",
                stage="row_extraction",
                item_count=sum(len(d.rows) for d in extraction.documents),
            )

        await self._edit(job_id, lease, commit)

    async def _review(self, job_id, lease, index, prepared, prompt, context, extraction):
        """При необходимости перечитывает только графы платёжного скана по компактной схеме."""

        def reset(current):
            """Отделяет второй реальный поток модели от первоначального JSON страницы."""
            current.pages[index].raw_text = ""
            current.emit("stream_reset", operation="financial_review")

        await self._edit(job_id, lease, reset)
        raw = await self._stage(
            job_id,
            lease,
            index,
            "financial_review",
            lambda: self._stream(
                job_id, lease, index, prepared, prompt, {**context, "operation": "financial_review"}
            ),
        )
        try:
            review = FinancialReview.model_validate_json(raw)
        except ValueError as exc:
            raise ServiceError(
                "invoice_review_invalid",
                "Не удалось подтвердить числовые графы платёжной строки",
                502,
            ) from exc
        document = extraction.documents[0]
        if (
            review.document_number is not None
            and document.number is not None
            and review.document_number != document.number
        ):
            raise ServiceError(
                "invoice_review_conflict", "Уточнение относится к другому документу", 502
            )
        rows = {row.row_number: row for row in review.rows}
        if len(rows) != len(review.rows) or set(rows) != {
            row.item.row_number for row in document.rows
        }:
            raise ServiceError(
                "invoice_review_conflict", "Уточнение не подтвердило все исходные строки", 502
            )
        document.line_columns = review.line_columns
        document.document_vat_rate = review.document_vat_rate or document.document_vat_rate
        for row in document.rows:
            for key, value in rows[row.item.row_number].model_dump(exclude={"row_number"}).items():
                setattr(row.item, key, value)
            row.item.vat_rate_source = "row" if row.item.vat_rate is not None else None
        return review


class ProfileProcessor:
    """Направляет UUID из существующей очереди в нужный профиль по durable discriminator."""

    def __init__(self, repository, specifications, invoices):
        """Собирается только composition root; API не выбирает worker реализацию."""
        self.repository, self.specifications, self.invoices = repository, specifications, invoices

    async def execute(self, job_id: str, delivery_revision: int | None = None) -> bool:
        """Повторная доставка использует тот же fencing lifecycle обоих профилей."""
        job = await self.repository.get(job_id)
        if job is not None:
            processor = (
                self.invoices
                if getattr(job, "job_type", None) == "invoice"
                else self.specifications
            )
            return await processor.execute(job_id, delivery_revision)
        return False
