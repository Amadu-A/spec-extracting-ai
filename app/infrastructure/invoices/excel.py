# app/infrastructure/invoices/excel.py
"""Отдельная финансовая книга: документы, позиции, предупреждения без шаблона legacy."""

import asyncio
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.domain.invoices.models import InvoiceExport

ITEM_COLUMNS = (
    ("row_number", "Строка"),
    ("caption", "Наименование"),
    ("brand", "Бренд"),
    ("manufacturer", "Производитель"),
    ("article", "Артикул"),
    ("article_candidate", "Артикул (кандидат)"),
    ("supplier_code", "Код поставщика"),
    ("product_reference_raw", "Исходная ссылка"),
    ("row_kind", "Вид строки"),
    ("units", "Единица"),
    ("quantity", "Количество"),
    ("unit_price_without_vat", "Цена без НДС"),
    ("unit_price_with_vat", "Цена с НДС"),
    ("line_total_without_vat", "Сумма без НДС"),
    ("vat_rate", "НДС, %"),
    ("vat_amount", "Сумма НДС"),
    ("line_total_with_vat", "Сумма с НДС"),
    ("vat_rate_source", "Источник ставки НДС"),
)
NUMBERS = {
    "quantity",
    "unit_price_without_vat",
    "unit_price_with_vat",
    "line_total_without_vat",
    "vat_rate",
    "vat_amount",
    "line_total_with_vat",
}


def append(sheet, values, numeric=()):
    """Любая строка принудительно текстовая; числа остаются Decimal до XML-сериализации."""
    index = sheet.max_row + 1
    for column, value in enumerate(values, 1):
        cell = sheet.cell(index, column)
        if column in numeric and value is not None:
            number = Decimal(value)
            if len(number.as_tuple().digits) > 15:
                # Excel ограничивает точность числа; длинное исходное значение сохраняется текстом.
                cell.value, cell.data_type = value, "s"
            else:
                cell.value = number
                cell.number_format = "#,##0.00############"
        else:
            cell.value = value
            if isinstance(value, str):
                cell.data_type = "s"


def build(result: InvoiceExport, warnings: list[dict]) -> bytes:
    """Строит книгу из того же DTO, что JSON, без записи служебных файлов в repository."""
    book = Workbook()
    documents = book.active
    documents.title = "Документы"
    documents.append(
        [
            "Файл",
            "Документ",
            "Вид",
            "Номер",
            "Дата",
            "Поставщик",
            "ИНН поставщика",
            "КПП поставщика",
            "Покупатель",
            "ИНН покупателя",
            "КПП покупателя",
            "Валюта",
            "Позиций",
            "Без НДС",
            "НДС",
            "С НДС",
            "Проверка",
            "Страницы",
        ]
    )
    items = book.create_sheet("Позиции")
    items.append(["Файл", "Документ", *[label for _, label in ITEM_COLUMNS], "Страницы"])
    problems = book.create_sheet("Предупреждения")
    problems.append(["Файл", "Документ", "Страница", "Строка", "Код", "Сообщение"])
    for file in result.files:
        for index, doc in enumerate(file.documents):
            append(
                documents,
                [
                    file.source_file,
                    index + 1,
                    doc.document_type,
                    doc.number,
                    doc.date,
                    doc.supplier.name,
                    doc.supplier.inn,
                    doc.supplier.kpp,
                    doc.buyer.name,
                    doc.buyer.inn,
                    doc.buyer.kpp,
                    doc.currency,
                    len(doc.items),
                    doc.totals.without_vat,
                    doc.totals.vat,
                    doc.totals.with_vat,
                    doc.validation.status,
                    ", ".join(map(str, doc.source_pages)),
                ],
                numeric={14, 15, 16},
            )
            for item in doc.items:
                append(
                    items,
                    [
                        file.source_file,
                        index + 1,
                        *[getattr(item, key) for key, _ in ITEM_COLUMNS],
                        ", ".join(map(str, item.source_pages)),
                    ],
                    numeric={i + 3 for i, (key, _) in enumerate(ITEM_COLUMNS) if key in NUMBERS},
                )
    for warning in warnings:
        index = warning.get("file_index")
        append(
            problems,
            [
                result.files[index].source_file if index is not None else None,
                warning["document_index"] + 1
                if warning.get("document_index") is not None
                else None,
                warning.get("pdf_page"),
                warning.get("row_number"),
                warning.get("code"),
                warning.get("message"),
            ],
        )
    for sheet in book:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.fill, cell.font = PatternFill("solid", fgColor="E3EBF5"), Font(bold=True)
        for index in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(index)].width = 26 if index != 2 else 45
    output = BytesIO()
    book.save(output)
    return output.getvalue()


class InvoiceExcelExporter:
    """CPU/XML-сериализация вынесена из event loop, общий шаблон не затрагивается."""

    async def excel(self, result: InvoiceExport, warnings: list[dict]) -> bytes:
        """Возвращает новую независимую книгу на каждый вызов."""
        return await asyncio.to_thread(build, result, warnings)
