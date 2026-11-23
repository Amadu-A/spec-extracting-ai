# app/domain/invoices/reconcile.py
"""Согласует границы документов/переносы, сохраняя каждую физическую строку."""

import re
from decimal import Decimal

from app.domain.errors import ServiceError
from app.domain.invoices.evidence import apply_native, mixed_code, native_rows
from app.domain.invoices.models import (
    InvoiceDocument,
    InvoiceExport,
    InvoiceFile,
    InvoiceItem,
    InvoiceJob,
    Party,
    Summary,
)
from app.domain.invoices.validation import validate_document


def merge_fields(target, source, warnings: list[str]) -> None:
    """Дополняет подтверждённые поля; противоречие не скрывается перезаписью."""
    if isinstance(target, Party) and target.inn and source.inn and target.inn != source.inn:
        warnings.append("Противоречивый ИНН; реквизиты разных сторон не объединены")
        return
    for key in type(source).model_fields:
        if key in {
            "items",
            "rows",
            "starts_document",
            "validation",
            "source_pages",
            "attached_pages",
            "notes",
            "article_header",
            "document_vat_rate",
            "line_columns",
        }:
            continue
        value, old = getattr(source, key), getattr(target, key)
        if key in {"supplier", "buyer", "totals"}:
            merge_fields(old, value, warnings)
        elif value is not None and value != [] and value != {} and value != "unknown":
            if old is None or old in ([], {}, "unknown"):
                setattr(target, key, value)
            elif old != value:
                warnings.append(
                    f"Противоречивые значения поля {key}; сохранено первое подтверждённое"
                )


def reconcile_file(
    job: InvoiceJob, file_index: int, numeric: bool = True
) -> tuple[InvoiceFile, list[dict]]:
    """Группирует только страницы одного файла; арифметика выполняется после всех листов."""
    source = job.sources[file_index]
    output = InvoiceFile(
        file_index=file_index,
        source_file=source.filename,
        source_sha256=source.sha256,
        page_count=source.page_count,
    )
    diagnostics = []
    current = None
    pending = None
    pending_page = None
    leading_attachments = []
    rates: dict[int, set[str]] = {}
    for checkpoint in (c for c in job.checkpoints if c.file_index == file_index):
        page, extraction = checkpoint.pdf_page, checkpoint.extraction
        if extraction is None or extraction.completeness != "complete":
            raise ServiceError("invoice_incomplete", "Не все исходные страницы подтверждены", 409)
        for message in extraction.warnings:
            diagnostics.append(
                dict(
                    file_index=file_index,
                    pdf_page=page,
                    document_index=None,
                    row_number=None,
                    code="model_warning",
                    message=message,
                )
            )
        if extraction.role in ("attachment", "irrelevant"):
            if any(fragment.rows for fragment in extraction.documents):
                raise ServiceError(
                    "invoice_attachment_rows", "Модель поместила товары в приложение", 502
                )
            if current:
                current.attached_pages.append(page)
                for fragment in extraction.documents if extraction.role == "attachment" else []:
                    # Финансы и номер договора/техприложения не относятся к итогам самого счёта.
                    merge_fields(current.supplier, fragment.supplier, current.notes)
                    merge_fields(current.buyer, fragment.buyer, current.notes)
                    for key in (
                        "contract_reference",
                        "specification_reference",
                        "related_commercial_offer_number",
                        "payment_terms",
                    ):
                        if getattr(current, key) is None and getattr(fragment, key) is not None:
                            setattr(current, key, getattr(fragment, key))
            else:
                leading_attachments.append(page)
            continue
        for fragment in extraction.documents:
            adopted_header = False
            if (
                current is not None
                and not current.items
                and pending is None
                and fragment.rows
                and current.document_type == "commercial_offer"
                and fragment.document_type == "invoice"
            ):
                # Вводное ТКП перед счётом описывает тот же прибор, но сущность задаёт товарная таблица.
                header = current
                current = InvoiceDocument.model_validate(
                    fragment.model_dump(
                        exclude={
                            "rows",
                            "starts_document",
                            "article_header",
                            "document_vat_rate",
                            "line_columns",
                        }
                    )
                )
                current.source_pages = []
                current.attached_pages = sorted(set([*header.attached_pages, *header.source_pages]))
                if (
                    header.document_type == "commercial_offer"
                    and current.document_type == "invoice"
                ):
                    current.related_commercial_offer_number = header.number
                merge_fields(current.supplier, header.supplier, current.notes)
                merge_fields(current.buyer, header.buyer, current.notes)
                current.payment_terms = current.payment_terms or header.payment_terms
                output.documents[-1] = current
                rates[len(output.documents) - 1] = set()
                adopted_header = True
            different_number = (
                current and fragment.number and current.number and fragment.number != current.number
            )
            new = not adopted_header and (
                current is None or fragment.starts_document or different_number
            )
            if new:
                if pending:
                    raise ServiceError(
                        "invoice_unclosed_row", "Перенос строки пересёк границу документов", 502
                    )
                current = InvoiceDocument.model_validate(
                    fragment.model_dump(
                        exclude={
                            "rows",
                            "starts_document",
                            "article_header",
                            "document_vat_rate",
                            "line_columns",
                        }
                    )
                )
                current.items = []
                current.source_pages = []
                current.attached_pages = leading_attachments
                leading_attachments = []
                output.documents.append(current)
            merge_fields(current, fragment, current.notes)
            # Пометка «Вам предоставлена скидка ... на ... руб.» описывает уже применённую
            # скидку, а не новый компонент сверх опубликованных строк. Итоги не участвуют
            # в этом решении; сопоставляется только буквально напечатанная сумма скидки.
            included = {Decimal(value) for value in checkpoint.included_discount_amounts}
            for adjustment in current.totals.adjustments:
                if (
                    adjustment.kind == "discount"
                    and not adjustment.included_in_line_totals
                    and any(
                        value is not None and abs(Decimal(value)) in included
                        for value in (adjustment.without_vat, adjustment.with_vat)
                    )
                ):
                    adjustment.included_in_line_totals = True
                    message = "Скидка из пометки о предоставленной скидке уже учтена в ценах; повторно не вычитается"
                    if message not in current.notes:
                        current.notes.append(message)
            doc_rates = rates.setdefault(len(output.documents) - 1, set())
            doc_rates.update(checkpoint.document_vat_rates)
            if fragment.document_vat_rate is not None:
                doc_rates.add(fragment.document_vat_rate)
            current.source_pages = sorted(set([*current.source_pages, page]))
            for row in fragment.rows:
                item = InvoiceItem.model_validate(row.item.model_dump(exclude={"article_source"}))
                if fragment.line_columns is not None:
                    for key in ("line_total_without_vat", "vat_amount", "line_total_with_vat"):
                        if key not in fragment.line_columns:
                            setattr(item, key, None)
                confirmed = (
                    bool(re.search(r"артикул", fragment.article_header, re.I))
                    or row.item.article_source == "explicit_marker"
                )
                if (
                    item.product_reference_raw
                    and item.article == item.product_reference_raw
                    and any(c.isdigit() for c in item.article or "")
                ):
                    confirmed = True
                if item.article and not confirmed:
                    item.article_candidate = item.article_candidate or item.article
                    item.article_candidate_source = "caption_suffix"
                    item.article = None
                elif item.article is None and item.product_reference_raw:
                    item.article = mixed_code(item)
                apply_native(
                    item,
                    native_rows(checkpoint.native_tables).get(item.row_number, []),
                    current.notes,
                )
                if item.article_candidate and (
                    not re.fullmatch(r"[\w./-]+", item.article_candidate)
                    or not item.caption.rstrip().endswith(item.article_candidate)
                ):
                    # Техническая конфигурация с диапазонами/скобками не является артикулом в конце имени.
                    item.article_candidate = None
                    item.article_candidate_source = None
                item.source_pages = [page]
                item.technical_parameters_pages = []
                if row.continuation:
                    if pending is None or pending_page != page - 1:
                        raise ServiceError(
                            "invoice_orphan_continuation",
                            "Продолжение не имеет соседней исходной строки",
                            502,
                        )
                    if item.row_number != pending.row_number:
                        raise ServiceError(
                            "invoice_row_conflict",
                            "Номера частей строки противоречат друг другу",
                            502,
                        )
                    if item.caption and item.caption != pending.caption:
                        pending.caption += " " + item.caption
                    item.caption = pending.caption
                    merge_fields(pending, item, current.notes)
                    pending.source_pages = sorted(set([*pending.source_pages, page]))
                    item = pending
                    pending = None
                elif pending:
                    raise ServiceError(
                        "invoice_unclosed_row", "Следующий лист не подтвердил перенос позиции", 502
                    )
                if row.continues_next:
                    pending, pending_page = item, page
                else:
                    # Номера строк, а не коды/описания, определяют исходное положение.
                    if any(old.row_number == item.row_number for old in current.items):
                        raise ServiceError(
                            "invoice_duplicate_row_id",
                            "Модель повторила номер физической строки",
                            502,
                        )
                    current.items.append(item)
    if pending:
        raise ServiceError("invoice_unclosed_row", "Не подтверждён конец переносимой позиции", 502)
    if not output.documents or not any(doc.items for doc in output.documents):
        raise ServiceError(
            "invoice_no_items", "Не найдены подтверждённые товарные/платёжные строки", 422
        )
    if any(not doc.items for doc in output.documents):
        raise ServiceError(
            "invoice_incomplete_document",
            "В пакете обнаружен документ без подтверждённых строк",
            502,
        )
    for index, doc in enumerate(output.documents):
        doc_rates = rates.get(index, set())
        if len(doc_rates) == 1:
            for item in doc.items:
                if item.vat_rate is None:
                    item.vat_rate = next(iter(doc_rates))
                    item.vat_rate_source = "document_total"
                    item.field_origin["vat_rate"] = "document_total"
        if source.selected_pages != list(range(1, source.page_count + 1)):
            doc.notes.append(
                "Выбраны не все страницы файла: реквизиты или итоговая страница могли быть исключены"
            )
    if numeric:
        diagnostics.extend(validate_file(output))
    return output, diagnostics


def explicit_vat_rates(text: str) -> list[str]:
    """Читает только явно напечатанный процент рядом с НДС, без вывода ставки из сумм/даты."""
    return sorted(
        set(
            value.replace(",", ".")
            for value in re.findall(r"НДС\s*[,:(]*\s*(\d{1,2}(?:[.,]\d+)?)\s*%", text, re.I)
        )
    )


def validate_file(output: InvoiceFile) -> list[dict]:
    """Отдельный измеряемый этап числовых проверок уже собранного документа."""
    diagnostics = []
    file_index = output.file_index
    for index, doc in enumerate(output.documents):
        doc.validation = validate_document(doc)
        for message in doc.validation.warnings:
            diagnostics.append(
                dict(
                    file_index=file_index,
                    pdf_page=doc.source_pages[0] if doc.source_pages else None,
                    document_index=index,
                    row_number=None,
                    code="document_validation",
                    message=message,
                )
            )
        for check in doc.validation.checks:
            if check.status == "failed":
                diagnostics.append(
                    dict(
                        file_index=file_index,
                        pdf_page=doc.source_pages[0],
                        document_index=index,
                        row_number=check.row_number,
                        code="arithmetic_mismatch",
                        message=check.check,
                        check=check.model_dump(),
                    )
                )
    return diagnostics


def reconcile_job(job: InvoiceJob, numeric: bool = True) -> tuple[InvoiceExport, list[dict]]:
    """Атомарный результат создаётся только после согласования каждого файла пакета."""
    files, warnings = [], []
    for index in range(len(job.sources)):
        result, diagnostics = reconcile_file(job, index, numeric)
        files.append(result)
        warnings.extend(diagnostics)
    return InvoiceExport(
        files=files,
        summary=Summary(
            file_count=len(files),
            document_count=sum(len(f.documents) for f in files),
            item_count=sum(len(d.items) for f in files for d in f.documents),
        ),
    ), warnings
